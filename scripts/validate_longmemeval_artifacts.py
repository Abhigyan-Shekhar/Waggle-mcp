#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OFFICIAL_CATEGORIES = {"SSU", "SSA", "SSP", "KU", "TR", "MS"}
STRESS_CATEGORIES = {"adversarial_contradictions", "cross_session_chains", "agent_decision_memory"}
SPLITS = {"mock", "tune", "heldout", "full", "stratified_150", "stress"}
CONDITIONS = {
    "full_context",
    "flat_vector",
    "waggle_full",
    "ablation_semantic_only",
    "ablation_lexical_only",
    "ablation_temporal_only",
    "ablation_no_graph_expansion",
    "ablation_no_conflict_update",
}
RETRIEVAL_CONDITIONS = CONDITIONS - {"full_context"}
MEMORY_FIRST_CONDITIONS = RETRIEVAL_CONDITIONS - {"flat_vector"}
REQUIRED_ROW_FIELDS = {
    "case_id",
    "suite",
    "split",
    "category",
    "condition",
    "reader_model",
    "judge_model",
    "dataset_sha256",
    "prompt_version",
    "run_artifact",
    "gold_support_ids",
    "retrieved_support_ids",
    "context_tokens",
    "input_tokens",
    "output_tokens",
    "answer",
    "judge_result",
    "latency_seconds",
    "cost_usd",
    "official_table_eligible",
}


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_row(row: dict[str, Any], *, line_number: int, allow_heldout: bool) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_ROW_FIELDS - set(row))
    if missing:
        errors.append(f"line {line_number}: missing required fields: {', '.join(missing)}")
        return errors

    for field in ["case_id", "reader_model", "judge_model", "prompt_version", "run_artifact", "answer"]:
        if not isinstance(row[field], str):
            errors.append(f"line {line_number}: {field} must be a string")
    if not _is_sha256(row["dataset_sha256"]):
        errors.append(f"line {line_number}: dataset_sha256 must be a 64-character hex digest")
    if row["suite"] not in {"longmemeval_s", "supplementary_stress"}:
        errors.append(f"line {line_number}: suite must be longmemeval_s or supplementary_stress")
    if row["split"] not in SPLITS:
        errors.append(f"line {line_number}: unsupported split {row['split']!r}")
    if row["split"] == "heldout" and not allow_heldout:
        errors.append(f"line {line_number}: heldout row found without --allow-heldout")
    if row["condition"] not in CONDITIONS:
        errors.append(f"line {line_number}: unsupported condition {row['condition']!r}")

    if row["suite"] == "longmemeval_s":
        if row["category"] not in OFFICIAL_CATEGORIES:
            errors.append(f"line {line_number}: official LongMemEval-S row has invalid category {row['category']!r}")
    elif row["category"] not in STRESS_CATEGORIES:
        errors.append(f"line {line_number}: supplementary row has invalid stress category {row['category']!r}")

    if row["suite"] == "supplementary_stress" and row["official_table_eligible"] is not False:
        errors.append(f"line {line_number}: supplementary stress rows must set official_table_eligible=false")
    if row["suite"] == "longmemeval_s" and not isinstance(row["official_table_eligible"], bool):
        errors.append(f"line {line_number}: official_table_eligible must be boolean")

    if not _validate_string_list(row["gold_support_ids"]):
        errors.append(f"line {line_number}: gold_support_ids must be a list of strings")
    if not _validate_string_list(row["retrieved_support_ids"]):
        errors.append(f"line {line_number}: retrieved_support_ids must be a list of strings")

    for field in ["context_tokens", "input_tokens", "output_tokens"]:
        if not isinstance(row[field], int) or row[field] < 0:
            errors.append(f"line {line_number}: {field} must be a non-negative integer")
    for field in ["latency_seconds", "cost_usd"]:
        if not _is_number(row[field]) or row[field] < 0:
            errors.append(f"line {line_number}: {field} must be a non-negative number")

    judge_result = row["judge_result"]
    if not isinstance(judge_result, dict) or "score" not in judge_result:
        errors.append(f"line {line_number}: judge_result must be an object with a score field")

    return errors


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON: {exc}")
                continue
            if not isinstance(row, dict):
                errors.append(f"line {line_number}: JSONL entry must be an object")
                continue
            rows.append(row)
    return rows, errors


def validate_manifest(path: Path, *, max_paid_cost: float) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"manifest: invalid JSON: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest: must be a JSON object"]

    required = {
        "run_id",
        "created_at",
        "dataset_path",
        "dataset_sha256",
        "prompt_version",
        "answering_prompt_style",
        "judge_protocol",
        "ingestion_protocol",
        "conditions",
        "models",
        "retrieval_config",
        "result_jsonl",
        "projected_total_paid_cost_usd",
        "max_total_paid_cost_usd",
        "heldout_policy",
    }
    missing = sorted(required - set(manifest))
    if missing:
        errors.append(f"manifest: missing required fields: {', '.join(missing)}")
        return errors

    if not _is_sha256(manifest["dataset_sha256"]):
        errors.append("manifest: dataset_sha256 must be a 64-character hex digest")
    if not isinstance(manifest["conditions"], list) or not manifest["conditions"]:
        errors.append("manifest: conditions must be a non-empty list")
    if not isinstance(manifest["models"], dict):
        errors.append("manifest: models must be an object")
    if manifest.get("ingestion_protocol") != "session-by-session":
        errors.append("manifest: ingestion_protocol must be session-by-session")
    retrieval_config = manifest["retrieval_config"]
    if not isinstance(retrieval_config, dict):
        errors.append("manifest: retrieval_config must be an object")
    elif isinstance(manifest["conditions"], list):
        retrieval_conditions = [condition for condition in manifest["conditions"] if condition in RETRIEVAL_CONDITIONS]
        reference: tuple[str, str] | None = None
        for condition in retrieval_conditions:
            config = retrieval_config.get(condition)
            if not isinstance(config, dict):
                errors.append(f"manifest: retrieval_config missing object for {condition}")
                continue
            embedding_model = config.get("embedding_model")
            chunking_policy = config.get("chunking_policy")
            ingestion_granularity = config.get("ingestion_granularity")
            retrieval_unit = config.get("retrieval_unit")
            answer_context_mode = config.get("answer_context_mode")
            memory_generation = config.get("memory_generation")
            temporal_fields = config.get("temporal_fields")
            if not isinstance(embedding_model, str) or not embedding_model:
                errors.append(f"manifest: retrieval_config.{condition}.embedding_model must be a non-empty string")
            if not isinstance(chunking_policy, str) or not chunking_policy:
                errors.append(f"manifest: retrieval_config.{condition}.chunking_policy must be a non-empty string")
            if ingestion_granularity != "session":
                errors.append(f"manifest: retrieval_config.{condition}.ingestion_granularity must be session")
            if condition == "flat_vector":
                if retrieval_unit != "chunk_only":
                    errors.append("manifest: retrieval_config.flat_vector.retrieval_unit must be chunk_only")
                if answer_context_mode != "source_chunk_only":
                    errors.append("manifest: retrieval_config.flat_vector.answer_context_mode must be source_chunk_only")
                if memory_generation != "none":
                    errors.append("manifest: retrieval_config.flat_vector.memory_generation must be none")
                if temporal_fields != []:
                    errors.append("manifest: retrieval_config.flat_vector.temporal_fields must be an empty list")
            elif condition in MEMORY_FIRST_CONDITIONS:
                if retrieval_unit != "memory_then_chunk":
                    errors.append(f"manifest: retrieval_config.{condition}.retrieval_unit must be memory_then_chunk")
                if answer_context_mode != "memory_plus_source_chunk":
                    errors.append(
                        f"manifest: retrieval_config.{condition}.answer_context_mode must be memory_plus_source_chunk"
                    )
                if memory_generation != "contextual_atomic_facts":
                    errors.append(
                        f"manifest: retrieval_config.{condition}.memory_generation must be contextual_atomic_facts"
                    )
                if temporal_fields != ["documentDate", "eventDate"]:
                    errors.append(
                        f"manifest: retrieval_config.{condition}.temporal_fields must be ['documentDate', 'eventDate']"
                    )
            if isinstance(embedding_model, str) and isinstance(chunking_policy, str):
                current = (embedding_model, chunking_policy)
                if reference is None:
                    reference = current
                elif current != reference:
                    errors.append("manifest: retrieval-assisted conditions must share embedding_model and chunking_policy")
    for field in ["projected_total_paid_cost_usd", "max_total_paid_cost_usd"]:
        if not _is_number(manifest[field]) or manifest[field] < 0:
            errors.append(f"manifest: {field} must be a non-negative number")
    if _is_number(manifest.get("max_total_paid_cost_usd")) and manifest["max_total_paid_cost_usd"] > max_paid_cost:
        errors.append(f"manifest: max_total_paid_cost_usd exceeds ${max_paid_cost:.2f}")
    if _is_number(manifest.get("projected_total_paid_cost_usd")) and manifest[
        "projected_total_paid_cost_usd"
    ] > max_paid_cost:
        errors.append(f"manifest: projected_total_paid_cost_usd exceeds ${max_paid_cost:.2f}")
    if manifest["heldout_policy"] != "heldout rows are not inspected until final evaluation":
        errors.append("manifest: heldout_policy must match the required held-out protection string")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Waggle LongMemEval result artifacts.")
    parser.add_argument("jsonl", type=Path, help="Result JSONL file to validate.")
    parser.add_argument("--manifest", type=Path, help="Optional run manifest JSON file.")
    parser.add_argument("--max-paid-cost", type=float, default=180.0)
    parser.add_argument("--allow-heldout", action="store_true", help="Allow held-out rows for final evaluation.")
    args = parser.parse_args(argv)

    errors: list[str] = []
    rows, load_errors = load_jsonl(args.jsonl)
    errors.extend(load_errors)
    for line_number, row in enumerate(rows, start=1):
        errors.extend(validate_row(row, line_number=line_number, allow_heldout=args.allow_heldout))

    total_cost = sum(float(row.get("cost_usd", 0.0)) for row in rows if _is_number(row.get("cost_usd")))
    if total_cost > args.max_paid_cost:
        errors.append(f"total row cost ${total_cost:.2f} exceeds ${args.max_paid_cost:.2f}")

    if args.manifest:
        errors.extend(validate_manifest(args.manifest, max_paid_cost=args.max_paid_cost))

    if errors:
        print("LongMemEval artifact validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Validated {len(rows)} rows; total recorded cost ${total_cost:.2f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
