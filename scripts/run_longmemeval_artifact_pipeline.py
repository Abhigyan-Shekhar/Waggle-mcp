#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import export_longmemeval_cost_ledger
import plan_longmemeval_run
import preflight_longmemeval_run
import run_longmemeval_mock_phase
import summarize_longmemeval_results
import validate_longmemeval_artifacts


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_validation(results: Path, manifest: Path, *, max_paid_cost: float) -> None:
    status = validate_longmemeval_artifacts.main(
        [
            str(results),
            "--manifest",
            str(manifest),
            "--max-paid-cost",
            str(max_paid_cost),
        ]
    )
    if status != 0:
        raise RuntimeError("result validation failed")


def run_pipeline(
    *,
    dataset: Path,
    output_dir: Path,
    mock_size: int,
    heldout_size: int,
    seed: int,
    prompt_version: str,
    run_id: str | None,
    conditions: list[str],
    mode: str,
    reader_model: str,
    judge_model: str,
    input_price_per_mtok: float,
    output_price_per_mtok: float,
    max_paid_cost: float,
    skip_key_check: bool,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    split_plan = output_dir / "split-plan.json"
    manifest = output_dir / "run-manifest.json"
    results = output_dir / "results.jsonl"
    cost_ledger_json = output_dir / "cost-ledger.json"
    cost_ledger_md = output_dir / "cost-ledger.md"
    summary_json = output_dir / "summary.json"
    summary_md = output_dir / "summary.md"

    plan = plan_longmemeval_run.build_plan(dataset, mock_size=mock_size, heldout_size=heldout_size, seed=seed)
    _write_json(split_plan, plan)
    manifest_payload = {
        "run_id": run_id or output_dir.name,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_path": str(dataset),
        "dataset_sha256": plan["dataset_sha256"],
        "prompt_version": prompt_version,
        "answering_prompt_style": "supermemory-longmembench-appendix-v1",
        "judge_protocol": "longmemeval-paper-question-specific-prompts",
        "ingestion_protocol": "session-by-session",
        "conditions": conditions,
        "models": {"reader": reader_model if mode != "dry-run" else "dry-run-reader", "judge": judge_model},
        "retrieval_config": {
            condition: plan_longmemeval_run.DEFAULT_RETRIEVAL_CONFIG[condition]
            for condition in conditions
            if condition in plan_longmemeval_run.DEFAULT_RETRIEVAL_CONFIG
        },
        "result_jsonl": str(results),
        "projected_total_paid_cost_usd": 0.0,
        "max_total_paid_cost_usd": max_paid_cost,
        "heldout_policy": plan_longmemeval_run.HELDOUT_POLICY,
    }
    _write_json(manifest, manifest_payload)

    preflight_errors = preflight_longmemeval_run.check_preflight(
        manifest_path=manifest,
        split_plan=split_plan,
        budget_projection=None,
        env=os.environ,
        max_paid_cost=max_paid_cost,
        skip_key_check=skip_key_check,
    )
    if preflight_errors:
        raise RuntimeError("preflight failed: " + "; ".join(preflight_errors))

    status = run_longmemeval_mock_phase.run_mock_phase(
        dataset=dataset,
        split_plan=split_plan,
        output=results,
        conditions=conditions,
        mode=mode,
        reader_model=reader_model,
        judge_model=judge_model,
        prompt_version=prompt_version,
        input_price_per_mtok=input_price_per_mtok,
        output_price_per_mtok=output_price_per_mtok,
    )
    if status != 0:
        raise RuntimeError("mock phase failed")

    _run_validation(results, manifest, max_paid_cost=max_paid_cost)

    ledger_status = export_longmemeval_cost_ledger.main(
        [
            str(results),
            "--manifest",
            str(manifest),
            "--output-json",
            str(cost_ledger_json),
            "--output-md",
            str(cost_ledger_md),
            "--max-paid-cost",
            str(max_paid_cost),
        ]
    )
    if ledger_status != 0:
        raise RuntimeError("cost ledger export failed")

    summary_status = summarize_longmemeval_results.main(
        [
            str(results),
            "--output-json",
            str(summary_json),
            "--output-md",
            str(summary_md),
            "--max-paid-cost",
            str(max_paid_cost),
        ]
    )
    if summary_status != 0:
        raise RuntimeError("summary export failed")

    return {
        "split_plan": str(split_plan),
        "manifest": str(manifest),
        "results": str(results),
        "cost_ledger_json": str(cost_ledger_json),
        "cost_ledger_md": str(cost_ledger_md),
        "summary_json": str(summary_json),
        "summary_md": str(summary_md),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LongMemEval artifact pipeline end to end.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mock-size", type=int, default=30)
    parser.add_argument("--heldout-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260706)
    parser.add_argument("--prompt-version", default="longmemeval-systems-v1")
    parser.add_argument("--run-id")
    parser.add_argument("--condition", action="append", choices=sorted(validate_longmemeval_artifacts.CONDITIONS))
    parser.add_argument("--mode", choices=["dry-run", "gemini"], default="dry-run")
    parser.add_argument("--reader-model", default=run_longmemeval_mock_phase.DEFAULT_READER_MODEL)
    parser.add_argument("--judge-model", default="gpt-4o")
    parser.add_argument("--input-price-per-mtok", type=float, default=0.0)
    parser.add_argument("--output-price-per-mtok", type=float, default=0.0)
    parser.add_argument("--max-paid-cost", type=float, default=180.0)
    parser.add_argument("--skip-key-check", action="store_true")
    args = parser.parse_args(argv)

    if args.mode == "gemini" and not os.getenv("GEMINI_API_KEY"):
        parser.error("--mode gemini requires GEMINI_API_KEY")
    if args.mode == "dry-run" and not args.skip_key_check:
        args.skip_key_check = True
    if args.input_price_per_mtok < 0 or args.output_price_per_mtok < 0 or args.max_paid_cost < 0:
        parser.error("prices and max paid cost must be non-negative")

    conditions = args.condition or run_longmemeval_mock_phase.DEFAULT_MOCK_CONDITIONS
    try:
        artifacts = run_pipeline(
            dataset=args.dataset.resolve(),
            output_dir=args.output_dir.resolve(),
            mock_size=args.mock_size,
            heldout_size=args.heldout_size,
            seed=args.seed,
            prompt_version=args.prompt_version,
            run_id=args.run_id,
            conditions=conditions,
            mode=args.mode,
            reader_model=args.reader_model,
            judge_model=args.judge_model,
            input_price_per_mtok=args.input_price_per_mtok,
            output_price_per_mtok=args.output_price_per_mtok,
            max_paid_cost=args.max_paid_cost,
            skip_key_check=args.skip_key_check,
        )
    except RuntimeError as exc:
        print(f"LongMemEval artifact pipeline failed: {exc}")
        return 1

    print("LongMemEval artifact pipeline completed.")
    for name, path in artifacts.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
