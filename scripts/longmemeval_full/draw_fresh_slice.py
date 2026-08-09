from __future__ import annotations

import argparse
import codecs
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


def iter_json_array(path: Path, *, chunk_size: int = 1024 * 1024) -> Iterable[dict[str, Any]]:
    """Stream objects from a top-level JSON array without loading its contents."""
    decoder = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    started = False
    finished = False
    with path.open("rb") as handle:
        while not finished:
            chunk = handle.read(chunk_size)
            buffer += utf8.decode(chunk, final=not chunk)
            cursor = 0
            while True:
                while cursor < len(buffer) and (buffer[cursor].isspace() or buffer[cursor] == ","):
                    cursor += 1
                if not started:
                    if cursor >= len(buffer):
                        break
                    if buffer[cursor] != "[":
                        raise ValueError(f"Expected top-level JSON array in {path}")
                    started = True
                    cursor += 1
                    continue
                while cursor < len(buffer) and (buffer[cursor].isspace() or buffer[cursor] == ","):
                    cursor += 1
                if cursor < len(buffer) and buffer[cursor] == "]":
                    finished = True
                    cursor += 1
                    break
                if cursor >= len(buffer):
                    break
                try:
                    row, end = decoder.raw_decode(buffer, cursor)
                except json.JSONDecodeError:
                    break
                if isinstance(row, dict):
                    yield row
                cursor = end
            buffer = buffer[cursor:]
            if not chunk and not finished:
                if buffer.strip():
                    raise ValueError(f"Incomplete JSON array in {path}")
                break


def iter_rows_from_json(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        prefix = ""
        while not prefix:
            char = handle.read(1)
            if not char:
                return
            if not char.isspace():
                prefix = char
    if prefix == "[":
        yield from iter_json_array(path)
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    yield from rows_from_json(payload)


def case_id(row: dict[str, Any]) -> str:
    return str(row.get("question_id") or row.get("case_id") or "").strip()


def collect_ids_from_json(path: Path) -> set[str]:
    try:
        rows = iter_rows_from_json(path)
        return {identifier for row in rows if (identifier := case_id(row))}
    except (OSError, ValueError, json.JSONDecodeError):
        return set()


def collect_ids_from_jsonl(path: Path) -> set[str]:
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
        if isinstance(row, dict) and (identifier := case_id(row)):
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
        if path in {source_path, output_path}:
            continue
        yield path


def draw_stratified(
    rows: Iterable[dict[str, Any]],
    *,
    size: int,
    seed: int,
    categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> list[dict[str, Any]]:
    if size < 1:
        raise ValueError("size must be positive")
    quotas = {category: size // len(categories) for category in categories}
    for category in categories[: size % len(categories)]:
        quotas[category] += 1
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = {category: 0 for category in categories}
    rng = {category: random.Random(f"{seed}:{category}") for category in categories}
    for row in rows:
        category = str(row.get("question_type") or row.get("category") or "")
        quota = quotas.get(category, 0)
        if quota == 0:
            continue
        seen[category] += 1
        bucket = by_category[category]
        if len(bucket) < quota:
            bucket.append(row)
            continue
        replacement = rng[category].randrange(seen[category])
        if replacement < quota:
            bucket[replacement] = row

    selected: list[dict[str, Any]] = []
    for index in range(max(quotas.values(), default=0)):
        for category in categories:
            if index < len(by_category[category]):
                selected.append(by_category[category][index])
    if len(selected) != size:
        availability = {category: seen[category] for category in categories}
        raise ValueError(f"Only {len(selected)} eligible rows match category quotas; availability={availability}")
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
    spent_ids: set[str] = set()
    source_records: list[dict[str, Any]] = []
    for path in exclusion_sources(
        source_path=args.source,
        output_path=args.output,
        runs_root=args.runs_root,
        benchmarks_root=args.benchmarks_root,
    ):
        found = (
            collect_ids_from_jsonl(path)
            if path.suffix == ".jsonl"
            else collect_ids_from_json(path)
        )
        if not found:
            continue
        spent_ids.update(found)
        source_records.append({"path": str(path), "sha256": sha256(path), "matched_case_count": len(found)})

    source_ids: set[str] = set()
    eligible_count = 0

    def eligible_rows() -> Iterable[dict[str, Any]]:
        nonlocal eligible_count
        for row in iter_rows_from_json(args.source):
            identifier = case_id(row)
            if not identifier:
                raise SystemExit("Source dataset contains missing or invalid case IDs")
            source_ids.add(identifier)
            if identifier in spent_ids:
                continue
            eligible_count += 1
            yield row

    selected = draw_stratified(eligible_rows(), size=args.size, seed=args.seed)
    spent_ids.intersection_update(source_ids)
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
        "eligible_count_before_draw": eligible_count,
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
        "eligible_count_before_draw": eligible_count,
        "category_counts": manifest["category_counts"],
        "spent_overlap": overlap,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
