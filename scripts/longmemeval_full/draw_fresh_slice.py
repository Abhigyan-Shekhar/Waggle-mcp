from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CATEGORIES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "knowledge-update",
    "temporal-reasoning",
    "multi-session",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows_from_json(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("cases", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def case_id(row: dict[str, Any]) -> str:
    return str(row.get("question_id") or row.get("case_id") or "").strip()


def collect_ids_from_json(path: Path, valid_ids: set[str]) -> set[str]:
    try:
        rows = rows_from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()
    return {identifier for row in rows if (identifier := case_id(row)) in valid_ids}


def collect_ids_from_jsonl(path: Path, valid_ids: set[str]) -> set[str]:
    found: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return found
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and (identifier := case_id(row)) in valid_ids:
            found.add(identifier)
    return found


def exclusion_sources(
    *,
    source_path: Path,
    output_path: Path,
    runs_root: Path,
    benchmarks_root: Path,
) -> Iterable[Path]:
    yield from sorted(runs_root.rglob("results.jsonl"))
    yield from sorted(runs_root.rglob("*frozen_case_manifest*.json"))
    for path in sorted(benchmarks_root.glob("*.json")):
        if path in {source_path, output_path} or "longmemeval_m_" in path.name:
            continue
        yield path


def draw_stratified(
    rows: list[dict[str, Any]],
    *,
    size: int,
    seed: int,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> list[dict[str, Any]]:
    if size < 1:
        raise ValueError("size must be positive")
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[str(row.get("question_type") or row.get("category") or "")].append(row)
    for category in categories:
        random.Random(f"{seed}:{category}").shuffle(by_category[category])

    selected: list[dict[str, Any]] = []
    cursor = {category: 0 for category in categories}
    while len(selected) < size:
        progressed = False
        for category in categories:
            index = cursor[category]
            if index >= len(by_category[category]):
                continue
            selected.append(by_category[category][index])
            cursor[category] += 1
            progressed = True
            if len(selected) == size:
                break
        if not progressed:
            raise ValueError(f"Only {len(selected)} eligible rows remain; cannot draw {size}")
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Draw a reproducible LongMemEval slice excluding all locally spent cases.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=Path("runs/longmemeval"))
    parser.add_argument("--benchmarks-root", type=Path, default=Path("benchmarks/longmemeval"))
    parser.add_argument("--size", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20260802)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_rows = rows_from_json(json.loads(args.source.read_text(encoding="utf-8")))
    valid_ids = {case_id(row) for row in source_rows}
    if not valid_ids or "" in valid_ids:
        raise SystemExit("Source dataset contains missing or invalid case IDs")

    spent_ids: set[str] = set()
    source_records: list[dict[str, Any]] = []
    for path in exclusion_sources(
        source_path=args.source,
        output_path=args.output,
        runs_root=args.runs_root,
        benchmarks_root=args.benchmarks_root,
    ):
        found = (
            collect_ids_from_jsonl(path, valid_ids)
            if path.suffix == ".jsonl"
            else collect_ids_from_json(path, valid_ids)
        )
        if not found:
            continue
        spent_ids.update(found)
        source_records.append({"path": str(path), "sha256": sha256(path), "matched_case_count": len(found)})

    eligible = [row for row in source_rows if case_id(row) not in spent_ids]
    selected = draw_stratified(eligible, size=args.size, seed=args.seed)
    selected_ids = [case_id(row) for row in selected]
    overlap = sorted(set(selected_ids) & spent_ids)
    if overlap:
        raise SystemExit(f"Fresh-slice invariant failed; selected spent IDs: {overlap}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(selected, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    category_counts: dict[str, int] = defaultdict(int)
    for row in selected:
        category_counts[str(row.get("question_type") or row.get("category") or "")] += 1
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(args.source),
        "source_sha256": sha256(args.source),
        "seed": args.seed,
        "requested_size": args.size,
        "selected_count": len(selected),
        "eligible_count_before_draw": len(eligible),
        "excluded_case_count": len(spent_ids),
        "category_counts": dict(sorted(category_counts.items())),
        "selected_cases": [
            {"case_id": case_id(row), "category": str(row.get("question_type") or row.get("category") or "")}
            for row in selected
        ],
        "exclusion_sources": source_records,
        "spent_overlap": overlap,
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "manifest": str(args.manifest),
        "selected_count": len(selected),
        "excluded_case_count": len(spent_ids),
        "eligible_count_before_draw": len(eligible),
        "category_counts": manifest["category_counts"],
        "spent_overlap": overlap,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
