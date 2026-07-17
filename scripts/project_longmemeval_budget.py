#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

import validate_longmemeval_artifacts as artifact_validator


def _parse_target(value: str) -> tuple[str, str, int, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 5:
        raise argparse.ArgumentTypeError(
            "targets must be condition,reader_model,cases,input_price_per_mtok,output_price_per_mtok"
        )
    condition, reader_model, raw_cases, raw_input_price, raw_output_price = parts
    try:
        cases = int(raw_cases)
        input_price = float(raw_input_price)
        output_price = float(raw_output_price)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid numeric target field in {value!r}") from exc
    if cases < 1 or input_price < 0 or output_price < 0:
        raise argparse.ArgumentTypeError("target cases must be positive and prices must be non-negative")
    return condition, reader_model, cases, input_price, output_price


def _load_valid_rows(path: Path, *, allow_heldout: bool) -> list[dict[str, Any]]:
    rows, load_errors = artifact_validator.load_jsonl(path)
    errors = list(load_errors)
    for line_number, row in enumerate(rows, start=1):
        errors.extend(artifact_validator.validate_row(row, line_number=line_number, allow_heldout=allow_heldout))
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    return rows


def _group_rows(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["condition"]), str(row["reader_model"]))].append(row)
    return dict(groups)


def project_budget(
    rows: list[dict[str, Any]],
    targets: list[tuple[str, str, int, float, float]],
    *,
    fixed_cost: float,
    cap: float,
) -> dict[str, Any]:
    groups = _group_rows(rows)
    projections: list[dict[str, Any]] = []
    total_projected = fixed_cost

    for condition, reader_model, case_count, input_price, output_price in targets:
        sample = groups.get((condition, reader_model), [])
        if not sample:
            available = ", ".join(f"{key[0]}:{key[1]}" for key in sorted(groups)) or "none"
            raise ValueError(f"no mock rows found for {condition}:{reader_model}; available groups: {available}")

        avg_input_tokens = mean(int(row["input_tokens"]) for row in sample)
        avg_output_tokens = mean(int(row["output_tokens"]) for row in sample)
        avg_context_tokens = mean(int(row["context_tokens"]) for row in sample)
        projected_input_tokens = avg_input_tokens * case_count
        projected_output_tokens = avg_output_tokens * case_count
        projected_cost = (projected_input_tokens / 1_000_000 * input_price) + (
            projected_output_tokens / 1_000_000 * output_price
        )
        total_projected += projected_cost
        projections.append(
            {
                "condition": condition,
                "reader_model": reader_model,
                "sample_rows": len(sample),
                "target_cases": case_count,
                "avg_context_tokens": avg_context_tokens,
                "avg_input_tokens": avg_input_tokens,
                "avg_output_tokens": avg_output_tokens,
                "projected_input_tokens": projected_input_tokens,
                "projected_output_tokens": projected_output_tokens,
                "input_price_per_mtok": input_price,
                "output_price_per_mtok": output_price,
                "projected_cost_usd": projected_cost,
            }
        )

    return {
        "cap_usd": cap,
        "fixed_cost_usd": fixed_cost,
        "projected_total_cost_usd": total_projected,
        "fits_cap": total_projected <= cap,
        "targets": projections,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project LongMemEval paid-run budget from mock token measurements.",
        epilog=(
            "Example: scripts/project_longmemeval_budget.py runs/mock.jsonl "
            "--target full_context,Qwen/Qwen3.7-Plus,500,0.32,1.28"
        ),
    )
    parser.add_argument("mock_results", type=Path, help="Validated mock result JSONL.")
    parser.add_argument(
        "--target",
        action="append",
        type=_parse_target,
        required=True,
        help="condition,reader_model,cases,input_price_per_mtok,output_price_per_mtok",
    )
    parser.add_argument("--fixed-cost", type=float, default=0.0, help="Already committed paid cost to include.")
    parser.add_argument("--cap", type=float, default=180.0)
    parser.add_argument("--allow-heldout", action="store_true")
    parser.add_argument("--output", type=Path, help="Optional JSON file for the projection.")
    args = parser.parse_args(argv)

    if args.fixed_cost < 0 or args.cap < 0:
        parser.error("--fixed-cost and --cap must be non-negative")

    rows = _load_valid_rows(args.mock_results, allow_heldout=args.allow_heldout)
    try:
        projection = project_budget(rows, args.target, fixed_cost=args.fixed_cost, cap=args.cap)
    except ValueError as exc:
        print(f"Budget projection failed: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(projection, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(payload, end="")

    return 0 if projection["fits_cap"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
