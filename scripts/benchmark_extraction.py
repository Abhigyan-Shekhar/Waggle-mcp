"""
Reproducible benchmark entrypoint for waggle-mcp.

Examples:
    PYTHONPATH=src .venv/bin/python scripts/benchmark_extraction.py
    PYTHONPATH=src .venv/bin/python scripts/benchmark_extraction.py --extraction-backend regex
    PYTHONPATH=src .venv/bin/python scripts/benchmark_extraction.py --output benchmarks/output/latest.json
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from waggle.benchmark_harness import main

if __name__ == "__main__":
    raise SystemExit(main())
