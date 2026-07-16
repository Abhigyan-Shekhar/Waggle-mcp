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


def _candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    node_type = candidate.get("node_type", "")
    node_type_value = getattr(node_type, "value", str(node_type))
    return {
        "node_type": node_type_value,
        "label": " ".join(str(candidate.get("label", "")).split()),
        "content": " ".join(str(candidate.get("content", "")).split()),
        "tags": list(candidate.get("tags", []) or []),
    }


def _extract(message: dict[str, Any], *, narrative_enabled: bool) -> list[dict[str, Any]]:
    text = waggle_runner._message_text(message)
    role = waggle_runner._message_role(message)
    kwargs = {"user_message": "", "assistant_response": ""}
    if role == "assistant":
        kwargs["assistant_response"] = text
    else:
        kwargs["user_message"] = text

    original = intelligence._is_personal_narrative
    if not narrative_enabled:
        intelligence._is_personal_narrative = lambda sentence: False
    try:
        return [_candidate_payload(candidate) for candidate in intelligence.extract_conversation_candidates(**kwargs)]
    finally:
        intelligence._is_personal_narrative = original


def audit_dataset(dataset: Path) -> dict[str, Any]:
    cases = plan_longmemeval_run._load_cases(dataset)
    rows: list[dict[str, Any]] = []
    summary_by_category: dict[str, Counter[str]] = defaultdict(Counter)
    added_type_by_category: dict[str, Counter[str]] = defaultdict(Counter)

    for index, case in enumerate(cases, start=1):
        case_id = plan_longmemeval_run._case_id(case, index)
        category = waggle_runner._task(case)
        gold_sessions = set(waggle_runner._gold_support_ids(case))
        for session_id, messages in zip(case.get("haystack_session_ids") or [], case.get("haystack_sessions") or []):
            if session_id not in gold_sessions:
                continue
            for message_index, message in enumerate(messages):
                if not isinstance(message, dict) or not message.get("has_answer"):
                    continue
                before = _extract(message, narrative_enabled=False)
                after = _extract(message, narrative_enabled=True)
                before_set = {json.dumps(candidate, sort_keys=True) for candidate in before}
                after_set = {json.dumps(candidate, sort_keys=True) for candidate in after}
                added = [json.loads(item) for item in sorted(after_set - before_set)]
                removed = [json.loads(item) for item in sorted(before_set - after_set)]
                changed = bool(added or removed)

                summary_by_category[category]["turns"] += 1
                if changed:
                    summary_by_category[category]["changed_turns"] += 1
                summary_by_category[category]["before_candidates"] += len(before)
                summary_by_category[category]["after_candidates"] += len(after)
                for candidate in added:
                    added_type_by_category[category][str(candidate.get("node_type", ""))] += 1

                rows.append(
                    {
                        "case_id": case_id,
                        "category": category,
                        "question": waggle_runner._question(case),
                        "session_id": session_id,
                        "message_index": message_index,
                        "role": waggle_runner._message_role(message),
                        "changed": changed,
                        "before_count": len(before),
                        "after_count": len(after),
                        "before_type_counts": dict(Counter(candidate["node_type"] for candidate in before)),
                        "after_type_counts": dict(Counter(candidate["node_type"] for candidate in after)),
                        "added": added,
                        "removed": removed,
                        "source_text": " ".join(waggle_runner._message_text(message).split())[:900],
                    }
                )

    return {
        "dataset": str(dataset),
        "scope": "all LongMemEval-S has_answer turns in gold support sessions",
        "summary": {
            "total_turns": len(rows),
            "changed_turns": sum(1 for row in rows if row["changed"]),
            "by_category": {
                category: {
                    **dict(counts),
                    "added_type_counts": dict(added_type_by_category.get(category, Counter())),
                }
                for category, counts in sorted(summary_by_category.items())
            },
        },
        "changed_turns": [row for row in rows if row["changed"]],
        "turns": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare LongMemEval extraction with personal narrative rule on/off.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = audit_dataset(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    summary = payload["summary"]
    print(f"turns={summary['total_turns']} changed_turns={summary['changed_turns']}")
    for category, counts in summary["by_category"].items():
        print(
            f"{category}: turns={counts.get('turns', 0)} changed={counts.get('changed_turns', 0)} "
            f"before={counts.get('before_candidates', 0)} after={counts.get('after_candidates', 0)} "
            f"added_types={counts.get('added_type_counts', {})}"
        )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
