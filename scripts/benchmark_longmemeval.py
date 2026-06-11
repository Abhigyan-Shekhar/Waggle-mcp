"""
Exploratory LongMemEval adapter for Waggle.

Examples:
    .venv/bin/python scripts/benchmark_longmemeval.py /path/to/longmemeval_s_cleaned.json --mode graph_raw --limit 50
    .venv/bin/python scripts/benchmark_longmemeval.py /path/to/longmemeval_s_cleaned.json --mode graph_hybrid
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from waggle.longmemeval_benchmark import main

if __name__ == "__main__":
    raise SystemExit(main())
