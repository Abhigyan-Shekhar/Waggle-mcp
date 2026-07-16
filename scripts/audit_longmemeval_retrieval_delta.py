#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import plan_longmemeval_run
import run_longmemeval_waggle_phase as waggle_runner
import waggle.intelligence as intelligence


def _case_by_id(dataset: Path) -> dict[str, dict[str, Any]]:
    cases = plan_longmemeval_run._load_cases(dataset)
    return {plan_longmemeval_run._case_id(case, index): case for index, case in enumerate(cases, start=1)}


def _selected_case_ids(delta_artifact: Path | None, explicit_case_ids: list[str]) -> list[str]:
    if explicit_case_ids:
        return explicit_case_ids
    if delta_artifact is None:
        raise ValueError("provide --case-id or --delta-artifact")
    payload = json.loads(delta_artifact.read_text(encoding="utf-8"))
    case_ids = {
        row["case_id"]
        for row in payload.get("changed_turns", [])
        if row.get("category") != "single-session-preference"
    }
    return sorted(case_ids)


def _first_gold_rank(retrieved_ids: list[str], gold_ids: list[str]) -> int | None:
    gold = set(gold_ids)
    return next((index for index, session_id in enumerate(retrieved_ids, start=1) if session_id in gold), None)


def _run_case(
    case_id: str,
    case: dict[str, Any],
    *,
    embedding_model: Any,
    retrieval_limit: int,
    narrative_enabled: bool,
) -> dict[str, Any]:
    original = intelligence._is_personal_narrative
    if not narrative_enabled:
        intelligence._is_personal_narrative = lambda sentence: False
    try:
        case_graph = waggle_runner._build_case_graph(
            case,
            embedding_model=embedding_model,
            agent_id=f"retrieval-delta-{'new' if narrative_enabled else 'legacy'}",
        )
    finally:
        intelligence._is_personal_narrative = original

    try:
        context, retrieved_ids, context_mode = waggle_runner._context_from_waggle(
            case,
            condition="waggle_full",
            case_graph=case_graph,
            retrieval_limit=retrieval_limit,
        )
        gold_ids = waggle_runner._gold_support_ids(case)
        return {
            "case_id": case_id,
            "category": waggle_runner._task(case),
            "question": waggle_runner._question(case),
            "gold_support_ids": gold_ids,
            "retrieved_support_ids": retrieved_ids,
            "first_gold_rank": _first_gold_rank(retrieved_ids, gold_ids),
            "gold_hit": bool(set(gold_ids) & set(retrieved_ids)),
            "context_mode": context_mode,
            "context_tokens": waggle_runner._token_estimate(context),
        }
    finally:
        case_graph["graph"].close()
        case_graph["tmpdir"].cleanup()


def audit_retrieval_delta(
    *,
    dataset: Path,
    case_ids: list[str],
    embedding_model_name: str,
    retrieval_limit: int,
) -> dict[str, Any]:
    cases = _case_by_id(dataset)
    EmbeddingModel, _ = waggle_runner._load_waggle_classes()
    embedding_model = EmbeddingModel(embedding_model_name)
    rows: list[dict[str, Any]] = []
    for index, case_id in enumerate(case_ids, start=1):
        if case_id not in cases:
            raise ValueError(f"case_id {case_id!r} not found")
        case = cases[case_id]
        print(f"[{index}/{len(case_ids)}] {case_id} {waggle_runner._task(case)}", flush=True)
        before = _run_case(
            case_id,
            case,
            embedding_model=embedding_model,
            retrieval_limit=retrieval_limit,
            narrative_enabled=False,
        )
        after = _run_case(
            case_id,
            case,
            embedding_model=embedding_model,
            retrieval_limit=retrieval_limit,
            narrative_enabled=True,
        )
        before_rank = before["first_gold_rank"]
        after_rank = after["first_gold_rank"]
        if before_rank is None and after_rank is None:
            rank_delta = None
        elif before_rank is None:
            rank_delta = "improved_from_miss"
        elif after_rank is None:
            rank_delta = "regressed_to_miss"
        elif after_rank < before_rank:
            rank_delta = "improved_rank"
        elif after_rank > before_rank:
            rank_delta = "regressed_rank"
        else:
            rank_delta = "unchanged_rank"
        rows.append(
            {
                "case_id": case_id,
                "category": before["category"],
                "question": before["question"],
                "gold_support_ids": before["gold_support_ids"],
                "before": before,
                "after": after,
                "retrieved_support_ids_changed": before["retrieved_support_ids"] != after["retrieved_support_ids"],
                "gold_hit_changed": before["gold_hit"] != after["gold_hit"],
                "rank_delta": rank_delta,
            }
        )

    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        bucket = by_category[row["category"]]
        bucket["cases"] += 1
        bucket[f"before_hit_{row['before']['gold_hit']}"] += 1
        bucket[f"after_hit_{row['after']['gold_hit']}"] += 1
        if row["retrieved_support_ids_changed"]:
            bucket["retrieved_support_ids_changed"] += 1
        if row["gold_hit_changed"]:
            bucket["gold_hit_changed"] += 1
        if row["rank_delta"]:
            bucket[str(row["rank_delta"])] += 1

    return {
        "dataset": str(dataset),
        "embedding_model": embedding_model_name,
        "retrieval_limit": retrieval_limit,
        "scope": "non-SSP cases whose has_answer extraction changed when personal narrative preservation is enabled",
        "summary": {
            "cases": len(rows),
            "retrieved_support_ids_changed": sum(1 for row in rows if row["retrieved_support_ids_changed"]),
            "gold_hit_changed": sum(1 for row in rows if row["gold_hit_changed"]),
            "regressed_to_miss": sum(1 for row in rows if row["rank_delta"] == "regressed_to_miss"),
            "improved_from_miss": sum(1 for row in rows if row["rank_delta"] == "improved_from_miss"),
            "by_category": {category: dict(counts) for category, counts in sorted(by_category.items())},
        },
        "cases": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare real LongMemEval harness retrieval with extraction rule on/off.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--delta-artifact", type=Path)
    parser.add_argument("--case-id", action="append", dest="case_ids", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--retrieval-limit", type=int, default=5)
    args = parser.parse_args()

    case_ids = _selected_case_ids(args.delta_artifact, args.case_ids)
    payload = audit_retrieval_delta(
        dataset=args.dataset,
        case_ids=case_ids,
        embedding_model_name=args.embedding_model,
        retrieval_limit=args.retrieval_limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2), flush=True)
    print(f"Wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
