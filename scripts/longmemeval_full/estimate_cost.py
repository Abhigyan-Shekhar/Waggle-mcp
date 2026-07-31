from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate paid reader/judge cost for longmemeval_full.")
    parser.add_argument("--cases", type=int, required=True)
    parser.add_argument("--conditions", required=True)
    parser.add_argument("--context-budget", type=int, required=True)
    parser.add_argument("--reader-output-tokens", type=int, default=128)
    parser.add_argument("--judge-input-tokens", type=int, default=512)
    parser.add_argument("--judge-output-tokens", type=int, default=8)
    parser.add_argument("--second-judge-policy", choices=["none", "all_disagreements", "all_rows"], default="all_disagreements")
    parser.add_argument("--max-disagreement-rate", type=float, default=0.25)
    parser.add_argument("--retry-allowance", type=float, default=0.10)
    parser.add_argument("--pricing-config", type=Path, required=True)
    args = parser.parse_args(argv)

    pricing = json.loads(args.pricing_config.read_text(encoding="utf-8"))
    conditions = [item.strip() for item in args.conditions.split(",") if item.strip()]
    rows = args.cases * len(conditions)
    reader_calls = rows
    primary_judge_calls = rows
    if args.second_judge_policy == "all_rows":
        secondary_judge_calls = rows
    elif args.second_judge_policy == "all_disagreements":
        secondary_judge_calls = int(rows * args.max_disagreement_rate)
    else:
        secondary_judge_calls = 0

    reader_input = reader_calls * args.context_budget
    reader_output = reader_calls * args.reader_output_tokens
    primary_judge_input = primary_judge_calls * args.judge_input_tokens
    primary_judge_output = primary_judge_calls * args.judge_output_tokens
    secondary_judge_input = secondary_judge_calls * args.judge_input_tokens
    secondary_judge_output = secondary_judge_calls * args.judge_output_tokens

    reader_cost = _cost(pricing["reader"], reader_input, reader_output)
    primary_judge_cost = _cost(pricing["primary_judge"], primary_judge_input, primary_judge_output)
    secondary_judge_cost = _cost(pricing["secondary_judge"], secondary_judge_input, secondary_judge_output)
    total = reader_cost + primary_judge_cost + secondary_judge_cost
    payload: dict[str, Any] = {
        "pricing_date": pricing.get("pricing_date", "unknown"),
        "pricing_source": pricing.get("pricing_source", "user_supplied"),
        "cases": args.cases,
        "conditions": conditions,
        "rows": rows,
        "reader_calls": reader_calls,
        "primary_judge_calls": primary_judge_calls,
        "maximum_second_judge_calls": secondary_judge_calls,
        "estimated_input_tokens": reader_input + primary_judge_input + secondary_judge_input,
        "estimated_output_tokens": reader_output + primary_judge_output + secondary_judge_output,
        "estimated_cost_usd": round(total, 6),
        "retry_allowance": args.retry_allowance,
        "pessimistic_retry_adjusted_cost_usd": round(total * (1.0 + args.retry_allowance), 6),
        "by_component_usd": {
            "reader": round(reader_cost, 6),
            "primary_judge": round(primary_judge_cost, 6),
            "secondary_judge": round(secondary_judge_cost, 6),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cost(model_pricing: dict[str, Any], input_tokens: int, output_tokens: int) -> float:
    input_per_m = float(model_pricing.get("input_per_million_usd", 0.0))
    output_per_m = float(model_pricing.get("output_per_million_usd", 0.0))
    return (input_tokens / 1_000_000.0) * input_per_m + (output_tokens / 1_000_000.0) * output_per_m


if __name__ == "__main__":
    raise SystemExit(main())
