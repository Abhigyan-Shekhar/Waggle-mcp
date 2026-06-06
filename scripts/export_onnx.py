"""
scripts/export_onnx.py

Part of ONNX Runtime migration (Issue #121).
Exports `all-MiniLM-L6-v2` to ONNX and validates numerical parity and timing against PyTorch.

Requirements:
    pip install onnx onnxruntime sentence-transformers>=3.2.0 optimum[onnxruntime]
"""

import argparse
import os
import shutil
import time
from pathlib import Path

from sentence_transformers import SentenceTransformer
from sentence_transformers.util import cos_sim


def get_dir_size_mb(path: str) -> float:
    total_size = sum(f.stat().st_size for f in Path(path).glob('**/*') if f.is_file())
    return total_size / (1024 * 1024)

def main(export_dir: str):
    model_name = "all-MiniLM-L6-v2"

    print(f"=== 1. Loading original PyTorch model: {model_name} ===")
    model_pt = SentenceTransformer(model_name)

    print("\n=== 2. Exporting to ONNX format ===")
    # Using ST-native ONNX backend.
    # model_kwargs={"export": True} forces optimum to build the ONNX graph locally.
    model_onnx = SentenceTransformer(
        model_name,
        backend="onnx",
        model_kwargs={"export": True}
    )

    # Save the artifact to fulfill the issue requirements
    if os.path.exists(export_dir):
        shutil.rmtree(export_dir)
    model_onnx.save(export_dir)
    print(f"ONNX artifact and tokenizer saved to ./{export_dir}/")

    print("\n=== 3. Validating Output Parity ===")
    sentences = [
    "We decided to use PostgreSQL as the primary database for the Acme web app. PostgreSQL offers ACID compliance, rich JSON support, and scales well for our expected load.",
    "PostgreSQL was chosen because the team has prior experience with it, it supports JSONB for flexible schema evolution, and the managed RDS offering fits our AWS deployment plan.",
    "For local development, we switched to SQLite to eliminate the Docker dependency and speed up onboarding. Production still targets PostgreSQL.",
    "New engineers were spending 30+ minutes setting up Postgres locally. SQLite requires zero setup and the ORM abstracts the difference.",
    "Final decision: use PostgreSQL in all environments (dev, staging, prod) via Docker Compose. The SQLite shortcut caused subtle migration drift. We added a one-command `make dev-up` to remove the setup friction.",
    "SQLite and PostgreSQL handle NULL semantics, JSON operators, and transaction isolation differently. Two bugs in staging traced back to SQLite-only dev. Docker Compose with a health-check solves onboarding without sacrificing parity.",
    "We will use Auth0 for authentication and SSO. Rolling our own OAuth is out of scope for v1. Auth0 supports SAML for enterprise customers.",
    "The team has no dedicated security engineer. Auth0 handles MFA, breach detection, and compliance certifications. Estimated 3-week saving vs. building in-house.",
    "We will deploy the web app on AWS ECS using Fargate. No EC2 instance management, auto-scaling, and integrates with our existing AWS account.",
    "The team is 4 engineers. Fargate means no patching, no AMI management, and cost scales to zero when idle. Kubernetes was considered but deemed over-engineered for v1.",
    "The team prefers TypeScript over plain JavaScript for all frontend work. Strict mode enabled. No `any` without a comment explaining why.",
    "The team prefers dark mode as the default UI theme. Light mode should be available as a toggle but dark is the out-of-box experience.",
    "The team strongly prefers small, focused PRs — ideally under 400 lines. Large PRs block review and increase merge conflict risk. Feature flags are the preferred mechanism for shipping incomplete features.",
    "The Acme web app team has 4 engineers: 2 full-stack, 1 backend, 1 frontend/design. No dedicated DevOps or security engineer.",
    "The target public launch date is end of Q3. The v1 scope is intentionally narrow: auth, core CRUD, and basic reporting. v2 will add integrations and advanced analytics."
    ]

    embeddings_pt = model_pt.encode(sentences, convert_to_numpy=True)
    embeddings_onnx = model_onnx.encode(sentences, convert_to_numpy=True)

    print("Checking cosine similarity for each sentence pair...")
    for i, _ in enumerate(sentences):
        # Calculate cosine similarity between the Torch and ONNX vectors
        sim = cos_sim(embeddings_pt[i], embeddings_onnx[i]).item()
        print(f"  Sentence {i+1} similarity: {sim:.6f}")

        # Assert parity >= 0.999 per acceptance criteria
        assert sim >= 0.999, f"Parity failed for sentence {i+1}: similarity = {sim}"

    print("Parity check passed! All similarities are >= 0.999.")

    print("\n=== 4. Performance and Size Comparison ===")

    onnx_size = get_dir_size_mb(export_dir)
    print(f"  Exported ONNX Directory Size: {onnx_size:.2f} MB")

    iterations = 50
    print(f"\nBenchmarking over {iterations} iterations (Batch size: {len(sentences)})...")

    # Warmup
    _ = model_pt.encode(sentences)
    _ = model_onnx.encode(sentences)

    # PyTorch timing
    start_pt = time.perf_counter()
    for _ in range(iterations):
        _ = model_pt.encode(sentences)
    time_pt = time.perf_counter() - start_pt

    # ONNX timing
    start_onnx = time.perf_counter()
    for _ in range(iterations):
        _ = model_onnx.encode(sentences)
    time_onnx = time.perf_counter() - start_onnx

    print(f"PyTorch Time : {time_pt:.4f}s")
    print(f"ONNX Time    : {time_onnx:.4f}s")
    print(f"Speedup      : {time_pt / time_onnx:.2f}x faster")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export all-MiniLM-L6-v2 to ONNX and validate output parity.")
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default="onnx_model_export",
        help="The directory where the ONNX model and tokenizer will be saved (default: onnx_model_export)"
    )

    args = parser.parse_args()
    main(args.output_dir)
