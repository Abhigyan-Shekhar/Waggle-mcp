from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .conditions import ALL_CONDITIONS
from .context_builder import token_estimate
from .provenance import stable_json_sha


REQUIRED_FILES = {
    "config.json",
    "frozen_case_manifest.json",
    "results.jsonl",
    "retrieval_traces.jsonl",
    "tool_traces.jsonl",
    "reader_requests.jsonl",
    "judge_requests.jsonl",
    "summary.csv",
    "category_summary.csv",
    "budget_summary.csv",
    "failure_analysis.csv",
    "context_efficiency.csv",
    "graph_contribution.csv",
    "FULL_CAPABILITY_EVALUATION_REPORT.md",
}
TOKEN_RECONCILIATION_TOLERANCE = 16


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate longmemeval_full output artifacts.")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    errors = validate_output_dir(args.output_dir)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


def validate_output_dir(output_dir: Path) -> list[str]:
    errors: list[str] = []
    missing = sorted(name for name in REQUIRED_FILES if not (output_dir / name).exists())
    errors.extend(f"missing required artifact: {name}" for name in missing)
    if missing:
        return errors

    config = _load_json(output_dir / "config.json", errors)
    manifest = _load_json(output_dir / "frozen_case_manifest.json", errors)
    results = _load_jsonl(output_dir / "results.jsonl", errors)
    traces = _load_jsonl(output_dir / "retrieval_traces.jsonl", errors)
    if errors:
        return errors

    expected_config_sha = stable_json_sha({key: value for key, value in config.items() if key != "config_sha"})
    if config.get("config_sha") != expected_config_sha:
        errors.append("config hash mismatch")
    if manifest.get("dataset_sha") != config.get("dataset_sha"):
        errors.append("manifest dataset_sha does not match config dataset_sha")

    valid_conditions = set(ALL_CONDITIONS)
    trace_keys = {(row.get("case_id"), row.get("condition")) for row in traces}
    for row in results:
        case_id = row.get("case_id")
        condition = row.get("condition")
        if condition not in valid_conditions:
            errors.append(f"{case_id}: invalid condition {condition!r}")
        if (case_id, condition) not in trace_keys:
            errors.append(f"{case_id}/{condition}: missing retrieval trace")
        budget = int(row.get("context_budget") or 0)
        final_tokens = int(row.get("final_context_tokens") or 0)
        if final_tokens > budget:
            errors.append(f"{case_id}/{condition}: final_context_tokens {final_tokens} exceeds budget {budget}")
        if "gold_support_ids" in row and condition != "oracle_support_context":
            errors.append(f"{case_id}/{condition}: non-oracle row contains gold_support_ids")
        prompt = str(row.get("reader_prompt") or "")
        if condition != "oracle_support_context":
            for forbidden in ("gold_support_ids", "answer_session_ids", "support_ids", "evidence_ids"):
                if forbidden in prompt:
                    errors.append(f"{case_id}/{condition}: hidden oracle field name leaked into reader prompt")
        for item in row.get("context_items") or []:
            _validate_context_item(errors, case_id=str(case_id), condition=str(condition), item=item)
        context_tokens = sum(int(item.get("token_count") or 0) for item in row.get("context_items") or [])
        if context_tokens and final_tokens > context_tokens + TOKEN_RECONCILIATION_TOLERANCE:
            errors.append(f"{case_id}/{condition}: final token count does not reconcile with context item tokens")
    return errors


def _validate_context_item(errors: list[str], *, case_id: str, condition: str, item: dict[str, Any]) -> None:
    item_type = str(item.get("item_type") or "")
    item_id = str(item.get("item_id") or "")
    text = str(item.get("text") or "")
    if not item_type:
        errors.append(f"{case_id}/{condition}: context item missing item_type")
    if not item.get("inclusion_reason"):
        errors.append(f"{case_id}/{condition}: context item missing inclusion_reason")
    if int(item.get("token_count") or 0) <= 0 and text:
        errors.append(f"{case_id}/{condition}: context item missing token_count")
    if item_type in {"node", "edge", "transcript", "hybrid_hit"} and not item_id:
        errors.append(f"{case_id}/{condition}: {item_type} context item has no traceable item_id")
    if item_type == "node" and not item.get("source_node_id"):
        errors.append(f"{case_id}/{condition}: node item missing source_node_id")


def _manifest_support_ids(manifest: dict[str, Any], case_id: Any) -> list[str]:
    for item in manifest.get("cases") or []:
        if item.get("case_id") == case_id:
            return [str(value) for value in item.get("gold_support_ids") or []]
    return []


def _load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            errors.append(f"{path.name}: expected object")
            return {}
        return payload
    except Exception as exc:
        errors.append(f"{path.name}: {exc}")
        return {}


def _load_jsonl(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                errors.append(f"{path.name}:{line_number}: expected object")
                continue
            rows.append(row)
    except Exception as exc:
        errors.append(f"{path.name}: {exc}")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
