#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import validate_longmemeval_artifacts


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_valid_rows(path: Path, *, allow_heldout: bool, max_paid_cost: float) -> list[dict[str, Any]]:
    rows, load_errors = validate_longmemeval_artifacts.load_jsonl(path)
    errors = list(load_errors)
    for line_number, row in enumerate(rows, start=1):
        errors.extend(
            validate_longmemeval_artifacts.validate_row(row, line_number=line_number, allow_heldout=allow_heldout)
        )

    total_cost = sum(float(row.get("cost_usd", 0.0)) for row in rows if _is_number(row.get("cost_usd")))
    if total_cost > max_paid_cost:
        errors.append(f"total row cost ${total_cost:.2f} exceeds ${max_paid_cost:.2f}")
    if errors:
        print("LongMemEval cost ledger validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    return rows


def _empty_bucket() -> dict[str, float | int]:
    return {
        "rows": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_tokens": 0,
        "latency_seconds": 0.0,
        "cost_usd": 0.0,
    }


def _add_to_bucket(bucket: dict[str, float | int], row: dict[str, Any]) -> None:
    bucket["rows"] = int(bucket["rows"]) + 1
    bucket["input_tokens"] = int(bucket["input_tokens"]) + int(row["input_tokens"])
    bucket["output_tokens"] = int(bucket["output_tokens"]) + int(row["output_tokens"])
    bucket["context_tokens"] = int(bucket["context_tokens"]) + int(row["context_tokens"])
    bucket["latency_seconds"] = float(bucket["latency_seconds"]) + float(row["latency_seconds"])
    bucket["cost_usd"] = float(bucket["cost_usd"]) + float(row["cost_usd"])


def _bucketize(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> dict[str, dict[str, float | int]]:
    buckets: dict[str, dict[str, float | int]] = defaultdict(_empty_bucket)
    for row in rows:
        key = "|".join(str(row[field]) for field in key_fields)
        _add_to_bucket(buckets[key], row)
    return {key: _finalize_bucket(value) for key, value in sorted(buckets.items())}


def _finalize_bucket(bucket: dict[str, float | int]) -> dict[str, float | int]:
    rows = int(bucket["rows"])
    cost = float(bucket["cost_usd"])
    latency = float(bucket["latency_seconds"])
    output = dict(bucket)
    output["cost_usd"] = round(cost, 8)
    output["mean_latency_seconds"] = round(latency / rows, 8) if rows else 0.0
    output["mean_cost_usd"] = round(cost / rows, 8) if rows else 0.0
    return output


def build_ledger(
    rows: list[dict[str, Any]],
    *,
    source_jsonl: Path,
    max_paid_cost: float,
    manifest: Path | None,
) -> dict[str, Any]:
    total = _empty_bucket()
    for row in rows:
        _add_to_bucket(total, row)
    total = _finalize_bucket(total)
    total_cost = float(total["cost_usd"])

    line_items = [
        {
            "case_id": row["case_id"],
            "suite": row["suite"],
            "split": row["split"],
            "category": row["category"],
            "condition": row["condition"],
            "reader_model": row["reader_model"],
            "judge_model": row["judge_model"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "context_tokens": row["context_tokens"],
            "latency_seconds": row["latency_seconds"],
            "cost_usd": row["cost_usd"],
            "run_artifact": row["run_artifact"],
            "official_table_eligible": row["official_table_eligible"],
        }
        for row in rows
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_jsonl": str(source_jsonl),
        "manifest": str(manifest) if manifest else None,
        "max_paid_cost_usd": max_paid_cost,
        "total_cost_usd": round(total_cost, 8),
        "remaining_budget_usd": round(max_paid_cost - total_cost, 8),
        "fits_cap": total_cost <= max_paid_cost,
        "totals": total,
        "by_suite": _bucketize(rows, ("suite",)),
        "by_split": _bucketize(rows, ("split",)),
        "by_condition": _bucketize(rows, ("condition",)),
        "by_reader_model": _bucketize(rows, ("reader_model",)),
        "by_judge_model": _bucketize(rows, ("judge_model",)),
        "by_reader_condition": _bucketize(rows, ("reader_model", "condition")),
        "line_items": line_items,
    }


def _fmt_usd(value: float | int) -> str:
    return f"${float(value):.4f}"


def _markdown_table(title: str, buckets: dict[str, dict[str, float | int]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not buckets:
        lines.extend(["No rows.", ""])
        return lines
    lines.extend(
        [
            "| key | rows | input tokens | output tokens | context tokens | cost | mean cost | mean latency |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for key, bucket in buckets.items():
        lines.append(
            "| "
            + " | ".join(
                [
                    key,
                    str(bucket["rows"]),
                    str(bucket["input_tokens"]),
                    str(bucket["output_tokens"]),
                    str(bucket["context_tokens"]),
                    _fmt_usd(bucket["cost_usd"]),
                    _fmt_usd(bucket["mean_cost_usd"]),
                    f"{float(bucket['mean_latency_seconds']):.4f}",
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def to_markdown(ledger: dict[str, Any]) -> str:
    lines = [
        "# LongMemEval Cost Ledger",
        "",
        f"Source JSONL: `{ledger['source_jsonl']}`",
        f"Manifest: `{ledger['manifest']}`",
        f"Total cost: {_fmt_usd(ledger['total_cost_usd'])}",
        f"Cap: {_fmt_usd(ledger['max_paid_cost_usd'])}",
        f"Remaining: {_fmt_usd(ledger['remaining_budget_usd'])}",
        f"Fits cap: {ledger['fits_cap']}",
        "",
    ]
    lines.extend(_markdown_table("By Reader Model", ledger["by_reader_model"]))
    lines.extend(_markdown_table("By Condition", ledger["by_condition"]))
    lines.extend(_markdown_table("By Reader And Condition", ledger["by_reader_condition"]))
    lines.extend(_markdown_table("By Suite", ledger["by_suite"]))
    lines.extend(_markdown_table("By Split", ledger["by_split"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a LongMemEval cost ledger from validated result JSONL.")
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--max-paid-cost", type=float, default=180.0)
    parser.add_argument("--allow-heldout", action="store_true")
    args = parser.parse_args(argv)

    if args.max_paid_cost < 0:
        parser.error("--max-paid-cost must be non-negative")
    if args.manifest:
        manifest_errors = validate_longmemeval_artifacts.validate_manifest(args.manifest, max_paid_cost=args.max_paid_cost)
        if manifest_errors:
            print("LongMemEval cost ledger validation failed:", file=sys.stderr)
            for error in manifest_errors:
                print(f"- {error}", file=sys.stderr)
            return 1

    try:
        rows = _load_valid_rows(args.jsonl, allow_heldout=args.allow_heldout, max_paid_cost=args.max_paid_cost)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    ledger = build_ledger(
        rows,
        source_jsonl=args.jsonl,
        max_paid_cost=args.max_paid_cost,
        manifest=args.manifest,
    )

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.output_json}")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(to_markdown(ledger) + "\n", encoding="utf-8")
        print(f"Wrote {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
