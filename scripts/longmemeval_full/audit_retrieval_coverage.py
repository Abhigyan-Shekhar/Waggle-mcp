from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def retrieved_sessions(row: dict[str, Any], db_path: Path) -> set[str]:
    transcript_ids = {str(value) for value in row.get("retrieved_transcript_ids") or []}
    node_ids = {str(value) for value in row.get("retrieved_node_ids") or []}
    sessions: set[str] = set()
    with sqlite3.connect(db_path) as connection:
        if transcript_ids:
            marks = ",".join("?" * len(transcript_ids))
            query = (
                f"SELECT DISTINCT session_id FROM transcript_records "
                f"WHERE id IN ({marks}) OR turn_pair_id IN ({marks})"
            )
            sessions.update(str(value) for value, in connection.execute(query, (*transcript_ids, *transcript_ids)))
        if node_ids:
            marks = ",".join("?" * len(node_ids))
            query = f"SELECT source_turn_pair_id FROM nodes WHERE id IN ({marks})"
            for source_turn_pair_id, in connection.execute(query, tuple(node_ids)):
                if not source_turn_pair_id:
                    continue
                sessions.update(
                    str(value)
                    for value, in connection.execute(
                        "SELECT DISTINCT session_id FROM transcript_records WHERE turn_pair_id = ?",
                        (source_turn_pair_id,),
                    )
                )
    return sessions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score retrieved session coverage without inspecting case content.")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--graph-cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--condition", default="")
    parser.add_argument("--include-case-details", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = {
        str(row["question_id"]): row
        for row in json.loads(args.dataset.read_text(encoding="utf-8"))
    }
    rows = load_jsonl(args.results)
    if args.condition:
        rows = [row for row in rows if str(row.get("condition") or "") == args.condition]
        if not rows:
            raise SystemExit(f"No result rows found for condition: {args.condition}")
    totals: Counter[str] = Counter()
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    details: list[dict[str, Any]] = []
    for row in rows:
        case_identifier = str(row["case_id"])
        case = cases[case_identifier]
        gold = {str(value) for value in case.get("answer_session_ids") or []}
        retrieved = retrieved_sessions(row, args.graph_cache_dir / f"{case_identifier}.db")
        matched = len(gold & retrieved)
        label = "full" if gold and matched == len(gold) else "partial" if matched else "none"
        category = str(row.get("category") or case.get("question_type") or "unknown")
        totals[label] += 1
        by_category[category][label] += 1
        if args.include_case_details:
            details.append(
                {
                    "case_id": case_identifier,
                    "category": category,
                    "coverage": label,
                    "gold_session_count": len(gold),
                    "matched_gold_session_count": matched,
                }
            )

    payload: dict[str, Any] = {
        "dataset": str(args.dataset),
        "dataset_sha256": sha256(args.dataset),
        "results": str(args.results),
        "results_sha256": sha256(args.results),
        "case_count": len(rows),
        "condition": args.condition or "all_conditions",
        "coverage": dict(sorted(totals.items())),
        "coverage_by_category": {
            category: dict(sorted(counts.items()))
            for category, counts in sorted(by_category.items())
        },
        "uses_gold_only_for_post_retrieval_scoring": True,
        "case_details_included": args.include_case_details,
    }
    if args.include_case_details:
        payload["cases"] = details
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
