#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "__all__")

import plan_longmemeval_run
import run_longmemeval_waggle_phase as waggle_runner


def _case_by_id(dataset: Path) -> dict[str, dict[str, Any]]:
    cases = plan_longmemeval_run._load_cases(dataset)
    return {plan_longmemeval_run._case_id(case, index): case for index, case in enumerate(cases, start=1)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate real LongMemEval Waggle context assembly without reader calls.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--case-id", action="append", dest="case_ids", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--retrieval-limit", type=int, default=5)
    parser.add_argument("--agent-id", default="context-validation")
    args = parser.parse_args()

    print("loading cases", flush=True)
    cases = _case_by_id(args.dataset)
    print("loading embedding classes", flush=True)
    original_entry_points = importlib.metadata.entry_points

    def no_plugin_entry_points(*entry_args: Any, **entry_kwargs: Any) -> Any:
        return ()

    importlib.metadata.entry_points = no_plugin_entry_points
    try:
        EmbeddingModel, _ = waggle_runner._load_waggle_classes()
    finally:
        importlib.metadata.entry_points = original_entry_points
    print("constructing embedding model", flush=True)
    embedding_model = EmbeddingModel(args.embedding_model)
    print("starting case loop", flush=True)
    rows: list[dict[str, Any]] = []

    for index, case_id in enumerate(args.case_ids, start=1):
        if case_id not in cases:
            raise ValueError(f"case_id {case_id!r} not found")
        case = cases[case_id]
        print(f"[{index}/{len(args.case_ids)}] {case_id} {waggle_runner._task(case)}", flush=True)
        case_graph = waggle_runner._build_case_graph(case, embedding_model=embedding_model, agent_id=args.agent_id)
        try:
            context, retrieved_ids, context_mode = waggle_runner._context_from_waggle(
                case,
                condition="waggle_full",
                case_graph=case_graph,
                retrieval_limit=args.retrieval_limit,
            )
            gold_ids = waggle_runner._gold_support_ids(case)
            rows.append(
                {
                    "case_id": case_id,
                    "category": waggle_runner._task(case),
                    "question": waggle_runner._question(case),
                    "gold_support_ids": gold_ids,
                    "retrieved_support_ids": retrieved_ids,
                    "gold_support_hit": bool(set(gold_ids) & set(retrieved_ids)),
                    "first_gold_rank": next(
                        (rank for rank, session_id in enumerate(retrieved_ids, start=1) if session_id in set(gold_ids)),
                        None,
                    ),
                    "context_mode": context_mode,
                    "context_tokens": waggle_runner._token_estimate(context),
                    "context_excerpt": context[:1200],
                }
            )
        finally:
            case_graph["graph"].close()
            case_graph["tmpdir"].cleanup()

    payload = {
        "dataset": str(args.dataset),
        "embedding_model": args.embedding_model,
        "retrieval_limit": args.retrieval_limit,
        "cases": rows,
        "summary": {
            "cases": len(rows),
            "gold_support_hits": sum(1 for row in rows if row["gold_support_hit"]),
            "context_modes": {
                mode: sum(1 for row in rows if row["context_mode"] == mode)
                for mode in sorted({row["context_mode"] for row in rows})
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2), flush=True)
    print(f"Wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
