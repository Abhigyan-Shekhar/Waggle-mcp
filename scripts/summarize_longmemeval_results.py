#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

import validate_longmemeval_artifacts


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _score(row: dict[str, Any]) -> float | None:
    judge_result = row.get("judge_result")
    if not isinstance(judge_result, dict):
        return None
    return _number(judge_result.get("score"))


def _hit_at(row: dict[str, Any], k: int) -> bool | None:
    gold = row.get("gold_support_ids")
    retrieved = row.get("retrieved_support_ids")
    if not gold:
        return None
    gold_set = set(gold)
    return bool(gold_set & set(retrieved[:k]))


def _exact_at(row: dict[str, Any], k: int) -> bool | None:
    gold = row.get("gold_support_ids")
    retrieved = row.get("retrieved_support_ids")
    if not gold:
        return None
    return set(gold).issubset(set(retrieved[:k]))


def _rate(values: list[bool | None]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return sum(1 for value in usable if value) / len(usable)


def _avg(values: list[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return mean(usable)


def _summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [_score(row) for row in rows]
    return {
        "rows": len(rows),
        "retrieval": {
            "support_hit_at_5": _rate([_hit_at(row, 5) for row in rows]),
            "support_hit_at_10": _rate([_hit_at(row, 10) for row in rows]),
            "support_hit_at_15": _rate([_hit_at(row, 15) for row in rows]),
            "exact_support_at_5": _rate([_exact_at(row, 5) for row in rows]),
            "exact_support_at_10": _rate([_exact_at(row, 10) for row in rows]),
            "exact_support_at_15": _rate([_exact_at(row, 15) for row in rows]),
        },
        "qa": {
            "mean_judge_score": _avg(scores),
            "scored_rows": sum(1 for score in scores if score is not None),
        },
        "efficiency": {
            "mean_context_tokens": _avg([row["context_tokens"] for row in rows]),
            "mean_input_tokens": _avg([row["input_tokens"] for row in rows]),
            "mean_output_tokens": _avg([row["output_tokens"] for row in rows]),
            "mean_latency_seconds": _avg([row["latency_seconds"] for row in rows]),
            "total_cost_usd": sum(float(row["cost_usd"]) for row in rows),
        },
    }


def _grouped_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_condition_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_condition[row["condition"]].append(row)
        by_category[row["category"]].append(row)
        by_split[row["split"]].append(row)
        by_condition_category[f"{row['condition']}|{row['category']}"].append(row)

    return {
        "overall": _summarize_group(rows) if rows else {"rows": 0},
        "by_condition": {key: _summarize_group(value) for key, value in sorted(by_condition.items())},
        "by_category": {key: _summarize_group(value) for key, value in sorted(by_category.items())},
        "by_split": {key: _summarize_group(value) for key, value in sorted(by_split.items())},
        "by_condition_category": {
            key: _summarize_group(value) for key, value in sorted(by_condition_category.items())
        },
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    official = [
        row
        for row in rows
        if row["suite"] == "longmemeval_s" and row["official_table_eligible"] is True
    ]
    non_official = [
        row
        for row in rows
        if row["suite"] == "longmemeval_s" and row["official_table_eligible"] is not True
    ]
    stress = [row for row in rows if row["suite"] == "supplementary_stress"]

    return {
        "row_count": len(rows),
        "total_cost_usd": sum(float(row["cost_usd"]) for row in rows),
        "official_longmemeval": _grouped_summary(official),
        "non_official_longmemeval": _grouped_summary(non_official),
        "supplementary_stress": _grouped_summary(stress),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _markdown_section(title: str, summary: dict[str, Any]) -> list[str]:
    lines = [f"## {title}", ""]
    by_condition = summary.get("by_condition", {})
    if not by_condition:
        lines.extend(["No rows.", ""])
        return lines

    lines.extend(
        [
            "| condition | rows | hit@5 | hit@10 | hit@15 | exact@5 | exact@10 | exact@15 | judge | ctx tok | in tok | out tok | cost |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for condition, payload in by_condition.items():
        retrieval = payload["retrieval"]
        qa = payload["qa"]
        efficiency = payload["efficiency"]
        lines.append(
            "| "
            + " | ".join(
                [
                    condition,
                    str(payload["rows"]),
                    _fmt(retrieval["support_hit_at_5"]),
                    _fmt(retrieval["support_hit_at_10"]),
                    _fmt(retrieval["support_hit_at_15"]),
                    _fmt(retrieval["exact_support_at_5"]),
                    _fmt(retrieval["exact_support_at_10"]),
                    _fmt(retrieval["exact_support_at_15"]),
                    _fmt(qa["mean_judge_score"]),
                    _fmt(efficiency["mean_context_tokens"]),
                    _fmt(efficiency["mean_input_tokens"]),
                    _fmt(efficiency["mean_output_tokens"]),
                    _fmt(efficiency["total_cost_usd"]),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def to_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# LongMemEval Result Summary",
        "",
        f"Rows: {summary['row_count']}",
        f"Total cost: ${summary['total_cost_usd']:.4f}",
        "",
    ]
    lines.extend(_markdown_section("Official LongMemEval-S", summary["official_longmemeval"]))
    lines.extend(_markdown_section("Non-Official LongMemEval-S", summary["non_official_longmemeval"]))
    lines.extend(_markdown_section("Supplementary Stress", summary["supplementary_stress"]))
    return "\n".join(lines)


def _load_valid_rows(path: Path, *, allow_heldout: bool, max_paid_cost: float) -> list[dict[str, Any]]:
    rows, load_errors = validate_longmemeval_artifacts.load_jsonl(path)
    errors = list(load_errors)
    for line_number, row in enumerate(rows, start=1):
        errors.extend(
            validate_longmemeval_artifacts.validate_row(row, line_number=line_number, allow_heldout=allow_heldout)
        )
    total_cost = sum(float(row.get("cost_usd", 0.0)) for row in rows if _number(row.get("cost_usd")) is not None)
    if total_cost > max_paid_cost:
        errors.append(f"total row cost ${total_cost:.2f} exceeds ${max_paid_cost:.2f}")
    if errors:
        print("LongMemEval summary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize validated LongMemEval result JSONL artifacts.")
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--max-paid-cost", type=float, default=180.0)
    parser.add_argument("--allow-heldout", action="store_true")
    args = parser.parse_args(argv)

    rows = _load_valid_rows(args.jsonl, allow_heldout=args.allow_heldout, max_paid_cost=args.max_paid_cost)
    summary = summarize(rows)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {args.output_json}")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(to_markdown(summary) + "\n", encoding="utf-8")
        print(f"Wrote {args.output_md}")
    if not args.output_json and not args.output_md:
        print(to_markdown(summary))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
