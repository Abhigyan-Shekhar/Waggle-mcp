"""
OOLONG benchmark entrypoint for Waggle.

Examples:
    PYTHONPATH=src .venv/bin/python scripts/benchmark_oolong.py /path/to/oolong.jsonl --eval-mode retrieval_only
    PYTHONPATH=src .venv/bin/python scripts/benchmark_oolong.py /path/to/oolong.jsonl --eval-mode waggle_llm --llm-command "python my_llm_runner.py {prompt_file}"
    PYTHONPATH=src GROQ_API_KEY=gsk_... .venv/bin/python scripts/benchmark_oolong.py /path/to/oolong.jsonl --eval-mode waggle_rlm --llm-backend groq
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from waggle.oolong_benchmark import main

if __name__ == "__main__":
    raise SystemExit(main())
