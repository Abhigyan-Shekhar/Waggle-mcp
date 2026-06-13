from __future__ import annotations

import os
import sys

# Add src to path if running locally
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from waggle.embeddings import EmbeddingModel


def verify():
    print("Checking model usage...")
    # Explicitly set model to use transformer
    model = EmbeddingModel("all-MiniLM-L6-v2")

    print(f"Target Model: {model.model_name}")
    print(f"Uses Deterministic Mode: {model.uses_deterministic_mode}")
    print(f"Model Version: {model.model_version}")

    if model.uses_deterministic_mode:
        print("FAIL: Model is using deterministic fallback!")
        sys.exit(1)
    else:
        print("SUCCESS: Model is using real transformer (baked-in)!")
        sys.exit(0)

if __name__ == "__main__":
    verify()
