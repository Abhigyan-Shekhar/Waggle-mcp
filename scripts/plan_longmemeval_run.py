#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OFFICIAL_CATEGORIES = {"SSU", "SSA", "SSP", "KU", "TR", "MS"}
DEFAULT_CONDITIONS = [
    "full_context",
    "flat_vector",
    "waggle_full",
    "ablation_semantic_only",
    "ablation_lexical_only",
    "ablation_temporal_only",
    "ablation_no_graph_expansion",
    "ablation_no_conflict_update",
]
DEFAULT_MODELS = {
    "mock_reader": "gemini-2.5-flash",
    "judge": "gpt-4o",
    "open_reader_primary": "llama-3.3-70b",
    "open_reader_secondary": "Qwen/Qwen3.7-Plus",
}
DEFAULT_RETRIEVAL_CONFIG = {
    "flat_vector": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "chunk_only",
        "answer_context_mode": "source_chunk_only",
        "memory_generation": "none",
        "temporal_fields": [],
    },
    "waggle_full": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "memory_then_chunk",
        "answer_context_mode": "memory_plus_source_chunk",
        "memory_generation": "contextual_atomic_facts",
        "temporal_fields": ["documentDate", "eventDate"],
    },
    "ablation_semantic_only": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "memory_then_chunk",
        "answer_context_mode": "memory_plus_source_chunk",
        "memory_generation": "contextual_atomic_facts",
        "temporal_fields": ["documentDate", "eventDate"],
    },
    "ablation_lexical_only": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "memory_then_chunk",
        "answer_context_mode": "memory_plus_source_chunk",
        "memory_generation": "contextual_atomic_facts",
        "temporal_fields": ["documentDate", "eventDate"],
    },
    "ablation_temporal_only": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "memory_then_chunk",
        "answer_context_mode": "memory_plus_source_chunk",
        "memory_generation": "contextual_atomic_facts",
        "temporal_fields": ["documentDate", "eventDate"],
    },
    "ablation_no_graph_expansion": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "memory_then_chunk",
        "answer_context_mode": "memory_plus_source_chunk",
        "memory_generation": "contextual_atomic_facts",
        "temporal_fields": ["documentDate", "eventDate"],
    },
    "ablation_no_conflict_update": {
        "embedding_model": "all-MiniLM-L6-v2",
        "chunking_policy": "longmemeval-session-chunks-v1",
        "ingestion_granularity": "session",
        "retrieval_unit": "memory_then_chunk",
        "answer_context_mode": "memory_plus_source_chunk",
        "memory_generation": "contextual_atomic_facts",
        "temporal_fields": ["documentDate", "eventDate"],
    },
}
HELDOUT_POLICY = "heldout rows are not inspected until final evaluation"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        cases = payload
    elif isinstance(payload, dict):
        for key in ("data", "cases", "examples", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                cases = value
                break
        else:
            raise ValueError("dataset JSON must be a list or contain one of: data, cases, examples, rows")
    else:
        raise ValueError("dataset JSON must be a list or object")
    if not all(isinstance(item, dict) for item in cases):
        raise ValueError("dataset cases must be JSON objects")
    return cases


def _case_id(case: dict[str, Any], index: int) -> str:
    for key in ("case_id", "id", "question_id", "qid"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return f"case-{index:05d}"


def _category(case: dict[str, Any]) -> str:
    for key in ("category", "question_type", "type", "qtype"):
        value = case.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "SINGLE_SESSION_USER": "SSU",
            "SINGLE_SESSION_ASSISTANT": "SSA",
            "SINGLE_SESSION_PREFERENCE": "SSP",
            "KNOWLEDGE_UPDATE": "KU",
            "TEMPORAL_REASONING": "TR",
            "MULTI_SESSION": "MS",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized in OFFICIAL_CATEGORIES:
            return normalized
    raise ValueError(f"case is missing an official LongMemEval-S category: {_case_id(case, 0)}")


def _stratified_take(
    cases: list[dict[str, str]],
    *,
    count: int,
    seed: int,
    excluded_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    excluded_ids = excluded_ids or set()
    buckets: dict[str, list[dict[str, str]]] = {category: [] for category in sorted(OFFICIAL_CATEGORIES)}
    rng = random.Random(seed)
    for case in cases:
        if case["case_id"] not in excluded_ids:
            buckets[case["category"]].append(case)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    selected: list[dict[str, str]] = []
    while len(selected) < count:
        progressed = False
        for category in sorted(buckets):
            bucket = buckets[category]
            if not bucket:
                continue
            selected.append(bucket.pop())
            progressed = True
            if len(selected) >= count:
                break
        if not progressed:
            break
    selected.sort(key=lambda item: item["case_id"])
    return selected


def build_plan(
    dataset: Path,
    *,
    mock_size: int,
    heldout_size: int,
    seed: int,
) -> dict[str, Any]:
    raw_cases = _load_cases(dataset)
    case_refs = [{"case_id": _case_id(case, index), "category": _category(case)} for index, case in enumerate(raw_cases, 1)]
    duplicate_ids = sorted({item["case_id"] for item in case_refs if sum(1 for other in case_refs if other["case_id"] == item["case_id"]) > 1})
    if duplicate_ids:
        raise ValueError(f"dataset contains duplicate case IDs: {', '.join(duplicate_ids[:5])}")

    heldout = _stratified_take(case_refs, count=heldout_size, seed=seed)
    heldout_ids = {item["case_id"] for item in heldout}
    mock = _stratified_take(case_refs, count=mock_size, seed=seed + 1, excluded_ids=heldout_ids)
    mock_ids = {item["case_id"] for item in mock}
    tune = [item for item in case_refs if item["case_id"] not in heldout_ids | mock_ids]

    return {
        "dataset_path": str(dataset),
        "dataset_sha256": _sha256(dataset),
        "seed": seed,
        "case_count": len(case_refs),
        "splits": {
            "mock": mock,
            "heldout": heldout,
            "tune": tune,
        },
        "notes": [
            "Split files contain IDs and categories only; do not materialize heldout prompts before final evaluation.",
            HELDOUT_POLICY,
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create ID-only LongMemEval split and run-manifest artifacts.")
    parser.add_argument("dataset", type=Path, help="LongMemEval-S JSON dataset.")
    parser.add_argument("--output-dir", type=Path, default=Path("runs/longmemeval"))
    parser.add_argument("--mock-size", type=int, default=30)
    parser.add_argument("--heldout-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260706)
    parser.add_argument("--prompt-version", default="longmemeval-systems-v1")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    if args.mock_size < 1 or args.heldout_size < 1:
        parser.error("--mock-size and --heldout-size must be positive")

    dataset = args.dataset.resolve()
    plan = build_plan(dataset, mock_size=args.mock_size, heldout_size=args.heldout_size, seed=args.seed)

    run_id = args.run_id or f"longmemeval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    split_plan_path = output_dir / "split-plan.json"
    manifest_path = output_dir / "run-manifest.json"
    result_jsonl = output_dir / "results.jsonl"

    split_plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_path": str(dataset),
        "dataset_sha256": plan["dataset_sha256"],
        "prompt_version": args.prompt_version,
        "answering_prompt_style": "supermemory-longmembench-appendix-v1",
        "judge_protocol": "longmemeval-paper-question-specific-prompts",
        "ingestion_protocol": "session-by-session",
        "conditions": DEFAULT_CONDITIONS,
        "models": DEFAULT_MODELS,
        "retrieval_config": DEFAULT_RETRIEVAL_CONFIG,
        "result_jsonl": str(result_jsonl),
        "projected_total_paid_cost_usd": 0.0,
        "max_total_paid_cost_usd": 180.0,
        "heldout_policy": HELDOUT_POLICY,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"Wrote {split_plan_path}")
    print(f"Wrote {manifest_path}")
    print(
        "Split counts: "
        f"mock={len(plan['splits']['mock'])}, "
        f"heldout={len(plan['splits']['heldout'])}, "
        f"tune={len(plan['splits']['tune'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
