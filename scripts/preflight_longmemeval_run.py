#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import plan_longmemeval_run
import validate_longmemeval_artifacts

HELDOUT_POLICY = "heldout rows are not inspected until final evaluation"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def _models_from_manifest(manifest: dict[str, Any]) -> set[str]:
    raw_models = manifest.get("models")
    models: set[str] = set()
    if isinstance(raw_models, dict):
        for value in raw_models.values():
            if isinstance(value, str) and value.strip():
                models.add(value.strip())
            elif isinstance(value, list):
                models.update(str(item).strip() for item in value if str(item).strip())
    elif isinstance(raw_models, list):
        models.update(str(item).strip() for item in raw_models if str(item).strip())
    return models


def _required_key_for_model(model: str) -> str | None:
    normalized = model.lower()
    if normalized.startswith("dry-run") or normalized in {"stub", "none"}:
        return None
    if "gemini" in normalized or normalized.startswith("google/"):
        return "GEMINI_API_KEY"
    if "claude" in normalized or "anthropic" in normalized:
        return "ANTHROPIC_API_KEY"
    if "gpt-" in normalized or normalized.startswith("openai/") or normalized in {"o3", "o4-mini"}:
        return "OPENAI_API_KEY"
    if "qwen" in normalized or "llama" in normalized or normalized.startswith("meta-llama/"):
        return "TOGETHER_API_KEY"
    return None


def _validate_retrieval_parity(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    conditions = manifest.get("conditions")
    retrieval_config = manifest.get("retrieval_config")
    if not isinstance(conditions, list) or not isinstance(retrieval_config, dict):
        return errors

    retrieval_conditions = [
        condition for condition in conditions if condition in validate_longmemeval_artifacts.RETRIEVAL_CONDITIONS
    ]
    reference: tuple[str, str] | None = None
    for condition in retrieval_conditions:
        config = retrieval_config.get(condition)
        if not isinstance(config, dict):
            errors.append(f"retrieval_config missing object for {condition}")
            continue
        embedding_model = config.get("embedding_model")
        chunking_policy = config.get("chunking_policy")
        ingestion_granularity = config.get("ingestion_granularity")
        if not isinstance(embedding_model, str) or not embedding_model:
            errors.append(f"retrieval_config.{condition}.embedding_model must be a non-empty string")
        if not isinstance(chunking_policy, str) or not chunking_policy:
            errors.append(f"retrieval_config.{condition}.chunking_policy must be a non-empty string")
        if ingestion_granularity != "session":
            errors.append(f"retrieval_config.{condition}.ingestion_granularity must be session")
        if isinstance(embedding_model, str) and isinstance(chunking_policy, str):
            current = (embedding_model, chunking_policy)
            if reference is None:
                reference = current
            elif current != reference:
                errors.append("retrieval-assisted conditions must share embedding_model and chunking_policy")
    return errors


def _validate_split_plan(split_plan: Path, dataset_sha256: str) -> list[str]:
    errors: list[str] = []
    try:
        plan = _load_json(split_plan)
    except ValueError as exc:
        return [str(exc)]

    if plan.get("dataset_sha256") != dataset_sha256:
        errors.append("split plan dataset_sha256 does not match manifest")
    splits = plan.get("splits")
    if not isinstance(splits, dict):
        return errors + ["split plan must contain a splits object"]

    ids_by_split: dict[str, set[str]] = {}
    for split in ("mock", "heldout", "tune"):
        entries = splits.get(split)
        if not isinstance(entries, list):
            errors.append(f"split plan missing {split} list")
            continue
        ids: set[str] = set()
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
                errors.append(f"split plan {split} entries must be objects with string case_id")
                continue
            if set(item) - {"case_id", "category"}:
                errors.append(f"split plan {split} entries must contain only case_id and category")
            ids.add(item["case_id"])
        ids_by_split[split] = ids

    for left, right in (("mock", "heldout"), ("mock", "tune"), ("heldout", "tune")):
        overlap = ids_by_split.get(left, set()) & ids_by_split.get(right, set())
        if overlap:
            errors.append(f"split plan {left}/{right} overlap: {', '.join(sorted(overlap)[:5])}")

    if len(ids_by_split.get("heldout", set())) < 100:
        errors.append("split plan heldout split has fewer than 100 cases")
    return errors


def check_preflight(
    *,
    manifest_path: Path,
    split_plan: Path | None,
    budget_projection: Path | None,
    env: Mapping[str, str],
    max_paid_cost: float,
    skip_key_check: bool,
) -> list[str]:
    errors: list[str] = []
    try:
        manifest = _load_json(manifest_path)
    except ValueError as exc:
        return [str(exc)]

    errors.extend(validate_longmemeval_artifacts.validate_manifest(manifest_path, max_paid_cost=max_paid_cost))

    dataset_path = Path(str(manifest.get("dataset_path", ""))).expanduser()
    if not dataset_path.exists():
        errors.append(f"dataset_path does not exist: {dataset_path}")
    elif plan_longmemeval_run._sha256(dataset_path) != manifest.get("dataset_sha256"):
        errors.append("dataset_sha256 does not match dataset_path contents")

    result_path = Path(str(manifest.get("result_jsonl", ""))).expanduser()
    if not str(result_path):
        errors.append("manifest result_jsonl must be set")
    elif not result_path.parent.exists():
        errors.append(f"result_jsonl parent directory does not exist: {result_path.parent}")

    if manifest.get("heldout_policy") != HELDOUT_POLICY:
        errors.append("manifest heldout_policy does not match required policy")
    if manifest.get("ingestion_protocol") != "session-by-session":
        errors.append("manifest ingestion_protocol must be session-by-session")
    errors.extend(_validate_retrieval_parity(manifest))

    conditions = manifest.get("conditions")
    if isinstance(conditions, list):
        invalid = sorted(set(conditions) - validate_longmemeval_artifacts.CONDITIONS)
        if invalid:
            errors.append(f"manifest contains unsupported conditions: {', '.join(invalid)}")

    models = _models_from_manifest(manifest)
    if not models:
        errors.append("manifest models must include at least one model ID")
    if not skip_key_check:
        missing_keys = sorted(
            {
                key
                for model in models
                if (key := _required_key_for_model(model)) is not None and not env.get(key)
            }
        )
        if missing_keys:
            errors.append(f"missing provider API keys: {', '.join(missing_keys)}")

    if split_plan:
        errors.extend(_validate_split_plan(split_plan, str(manifest.get("dataset_sha256", ""))))

    if budget_projection:
        try:
            projection = _load_json(budget_projection)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if projection.get("fits_cap") is not True:
                errors.append("budget projection does not fit cap")
            projected_total = projection.get("projected_total_cost_usd")
            if not isinstance(projected_total, (int, float)) or isinstance(projected_total, bool):
                errors.append("budget projection missing projected_total_cost_usd")
            elif float(projected_total) > max_paid_cost:
                errors.append(f"budget projection ${projected_total:.2f} exceeds ${max_paid_cost:.2f}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight-check LongMemEval run artifacts before paid model calls.")
    parser.add_argument("manifest", type=Path, help="Run manifest JSON.")
    parser.add_argument("--split-plan", type=Path)
    parser.add_argument("--budget-projection", type=Path)
    parser.add_argument("--max-paid-cost", type=float, default=180.0)
    parser.add_argument("--skip-key-check", action="store_true", help="Use only for dry-run/local plumbing checks.")
    args = parser.parse_args(argv)

    if args.max_paid_cost < 0:
        parser.error("--max-paid-cost must be non-negative")

    errors = check_preflight(
        manifest_path=args.manifest,
        split_plan=args.split_plan,
        budget_projection=args.budget_projection,
        env=os.environ,
        max_paid_cost=args.max_paid_cost,
        skip_key_check=args.skip_key_check,
    )
    if errors:
        print("LongMemEval preflight failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("LongMemEval preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
