from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waggle.token_efficiency_benchmark import (
    build_markdown_report,
    build_v2_comparison_report,
    generate_default_dataset,
    run_benchmark,
    run_comparison_benchmark,
    write_comparison_report,
    write_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark Waggle graph-memory retrieval against vanilla chunked-vector RAG on token efficiency."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("baseline", "comparison_v2"),
        default="comparison_v2",
        help="Benchmark mode to run.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/generated/token_efficiency_dataset.json"),
        help="JSON dataset path. If missing and --generate-default-dataset is set, a deterministic fixture is generated.",
    )
    parser.add_argument(
        "--generate-default-dataset",
        action="store_true",
        help="Generate the deterministic 50-conversation / 30-query benchmark dataset before running.",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=Path("benchmarks/generated/v2_comparison.md"),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("benchmarks/generated/v2_comparison.json"),
    )
    parser.add_argument(
        "--rag-embedding-model",
        default="text-embedding-3-small",
        help="Requested embedding model for the vanilla RAG baseline.",
    )
    parser.add_argument(
        "--allow-local-baseline-fallback",
        action="store_true",
        help="Allow the RAG baseline to fall back to the local embedding model if OpenAI embeddings are unavailable.",
    )
    parser.add_argument(
        "--local-fallback-embedding-model",
        default="all-MiniLM-L6-v2",
        help="Local embedding model name to use for Waggle and optional RAG fallback.",
    )
    parser.add_argument("--waggle-top-k", type=int, default=5)
    parser.add_argument("--waggle-max-depth", type=int, default=2)
    parser.add_argument("--waggle-expand-depth", type=int, default=0)
    parser.add_argument("--rag-chunk-size", type=int, default=512)
    parser.add_argument("--rag-chunk-overlap", type=int, default=64)
    parser.add_argument("--rag-top-k", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.generate_default_dataset or not args.dataset.exists():
        generate_default_dataset(args.dataset)

    if args.mode == "comparison_v2":
        report = run_comparison_benchmark(
            dataset_path=args.dataset,
            allow_local_baseline_fallback=True,
            local_fallback_embedding_model_name=args.local_fallback_embedding_model,
        )
        write_comparison_report(args.output_markdown, args.output_json, report)
        print(build_v2_comparison_report(report))
    else:
        report = run_benchmark(
            dataset_path=args.dataset,
            rag_embedding_model_name=args.rag_embedding_model,
            allow_local_baseline_fallback=args.allow_local_baseline_fallback,
            local_fallback_embedding_model_name=args.local_fallback_embedding_model,
            waggle_top_k=args.waggle_top_k,
            waggle_max_depth=args.waggle_max_depth,
            waggle_expand_depth=args.waggle_expand_depth,
            rag_chunk_size=args.rag_chunk_size,
            rag_chunk_overlap=args.rag_chunk_overlap,
            rag_top_k=args.rag_top_k,
        )
        write_report(args.output_markdown, args.output_json, report)
        print(build_markdown_report(report))
    print(f"\nSaved markdown report to {args.output_markdown}")
    print(f"Saved JSON report to {args.output_json}")


if __name__ == "__main__":
    main()
