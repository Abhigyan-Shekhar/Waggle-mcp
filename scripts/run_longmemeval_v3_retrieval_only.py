#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph
from waggle.memory_benchmark import index_case_into_waggle, load_longmemeval, retrieve_waggle_graph


class CaseTimeout(RuntimeError):
    pass


def _timeout_handler(signum, frame):
    raise CaseTimeout("case retrieval timed out")


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int((len(sorted_values) - 1) * q)))
    return float(sorted_values[index])


def _load_completed_cases(partial_jsonl: Path) -> tuple[list[dict[str, object]], set[str], list[str], list[float]]:
    if not partial_jsonl.exists():
        return [], set(), [], []
    per_case: list[dict[str, object]] = []
    completed_case_ids: set[str] = set()
    timed_out_cases: list[str] = []
    latencies: list[float] = []
    with partial_jsonl.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            case_id = str(row["case_id"])
            if case_id in completed_case_ids:
                continue
            per_case.append(row)
            completed_case_ids.add(case_id)
            latency = row.get("latency_seconds")
            if isinstance(latency, (int, float)):
                latencies.append(float(latency))
            if row.get("timed_out"):
                timed_out_cases.append(case_id)
    return per_case, completed_case_ids, timed_out_cases, latencies


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LongMemEval retrieval-only benchmark on the v3 memory harness.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    dataset = root / "benchmarks" / "longmemeval" / "longmemeval_s_cleaned.json"
    output_json = root / "tests" / "artifacts" / "longmemeval_500_new_harness_v3.json"
    output_md = root / "tests" / "artifacts" / "longmemeval_500_new_harness_v3_summary.md"
    partial_jsonl = root / "tests" / "artifacts" / "longmemeval_500_v3_partial.jsonl"

    cases = load_longmemeval(dataset)[: args.limit]
    total = len(cases)
    embedding_model = EmbeddingModel("all-MiniLM-L6-v2")
    tmpdir = tempfile.TemporaryDirectory()
    graph = MemoryGraph(
        Path(tmpdir.name) / "memory-benchmark.db",
        embedding_model,
        dedup_similarity_threshold=1.01,
        dedup_same_label_threshold=1.01,
    )

    per_case, completed_case_ids, timed_out_cases, latencies = _load_completed_cases(partial_jsonl)
    checkpoint_start = len(per_case)
    started = time.perf_counter()

    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    try:
        for index, case in enumerate(cases, start=1):
            if case.case_id in completed_case_ids:
                continue
            case_started = time.perf_counter()
            timed_out = False
            retrieved_session_ids: list[str] = []
            try:
                signal.alarm(args.timeout_seconds)
                indexed = index_case_into_waggle(case, graph)
                retrieved = retrieve_waggle_graph(indexed, graph, limit=20, hops=1)
                seen: set[str] = set()
                for node in retrieved.nodes:
                    session_id = (node.session_id or "").strip()
                    if not session_id or session_id in seen:
                        continue
                    seen.add(session_id)
                    retrieved_session_ids.append(session_id)
                    if len(retrieved_session_ids) >= 20:
                        break
            except CaseTimeout:
                timed_out = True
                print(f"TIMEOUT case_id={case.case_id}", flush=True)
            finally:
                signal.alarm(0)

            gold_ids = [str(item) for item in case.metadata.get("gold_support_ids", [])]
            gold_set = set(gold_ids)
            top5 = retrieved_session_ids[:5]
            top10 = retrieved_session_ids[:10]
            top20 = retrieved_session_ids[:20]
            top5_set = set(top5)
            top10_set = set(top10)
            top20_set = set(top20)
            elapsed_case = time.perf_counter() - case_started
            row = {
                "case_id": case.case_id,
                "question": case.question,
                "question_type": case.question_type,
                "gold_support_ids": gold_ids,
                "retrieved_session_ids_top20": top20,
                "hit_at_5": bool(top5_set & gold_set),
                "exact_at_5": gold_set.issubset(top5_set),
                "exact_at_10": gold_set.issubset(top10_set),
                "exact_at_20": gold_set.issubset(top20_set),
                "timed_out": timed_out,
                "latency_seconds": elapsed_case,
            }
            per_case.append(row)
            latencies.append(elapsed_case)
            completed_case_ids.add(case.case_id)
            if timed_out:
                timed_out_cases.append(case.case_id)
            print(f"[{index}/{total}] case_id={case.case_id} elapsed={elapsed_case:.1f}s", flush=True)

            if len(per_case) - checkpoint_start >= 25:
                partial_jsonl.parent.mkdir(parents=True, exist_ok=True)
                with partial_jsonl.open("a", encoding="utf-8") as handle:
                    for item in per_case[checkpoint_start:]:
                        handle.write(json.dumps(item) + "\n")
                checkpoint_start = len(per_case)

        if len(per_case) > checkpoint_start:
            partial_jsonl.parent.mkdir(parents=True, exist_ok=True)
            with partial_jsonl.open("a", encoding="utf-8") as handle:
                for item in per_case[checkpoint_start:]:
                    handle.write(json.dumps(item) + "\n")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)

    case_count = len(per_case)
    overall_r5 = sum(1 for item in per_case if item["hit_at_5"]) / case_count if case_count else 0.0
    overall_e5 = sum(1 for item in per_case if item["exact_at_5"]) / case_count if case_count else 0.0
    overall_e10 = sum(1 for item in per_case if item["exact_at_10"]) / case_count if case_count else 0.0
    overall_e20 = sum(1 for item in per_case if item["exact_at_20"]) / case_count if case_count else 0.0

    buckets: dict[int, list[dict[str, object]]] = defaultdict(list)
    for item in per_case:
        buckets[len(set(item["gold_support_ids"]))].append(item)

    by_gold_cardinality: dict[str, dict[str, float | int]] = {}
    for cardinality, bucket in sorted(buckets.items()):
        count = len(bucket)
        by_gold_cardinality[str(cardinality)] = {
            "count": count,
            "recall_at_5": sum(1 for item in bucket if item["hit_at_5"]) / count if count else 0.0,
            "exact_at_5": sum(1 for item in bucket if item["exact_at_5"]) / count if count else 0.0,
            "exact_at_10": sum(1 for item in bucket if item["exact_at_10"]) / count if count else 0.0,
            "exact_at_20": sum(1 for item in bucket if item["exact_at_20"]) / count if count else 0.0,
        }

    divergence_examples: list[dict[str, object]] = []
    for item in per_case:
        if not item["hit_at_5"] or item["exact_at_5"]:
            continue
        top5 = list(item["retrieved_session_ids_top20"][:5])
        top5_set = set(top5)
        missing = [gold for gold in item["gold_support_ids"] if gold not in top5_set]
        divergence_examples.append(
            {
                "case_id": item["case_id"],
                "gold_set": item["gold_support_ids"],
                "retrieved_top5": top5,
                "missing": missing,
            }
        )
        if len(divergence_examples) >= 3:
            break

    runtime_seconds = time.perf_counter() - started
    sorted_latencies = sorted(latencies)
    latency_summary = {
        "mean": mean(latencies) if latencies else 0.0,
        "p50": _quantile(sorted_latencies, 0.50),
        "p95": _quantile(sorted_latencies, 0.95),
        "max": max(latencies) if latencies else 0.0,
    }

    payload = {
        "benchmark": "longmemeval",
        "mode": "new_harness_graph",
        "case_count": case_count,
        "embedding_model": "all-MiniLM-L6-v2",
        "scoring_unit": "unique_session_ids_from_retrieved_turn_or_anchor_nodes",
        "timed_out_cases": timed_out_cases,
        "summary": {
            "case_count": case_count,
            "recall_at_5": overall_r5,
            "exact_at_5": overall_e5,
            "exact_at_10": overall_e10,
            "exact_at_20": overall_e20,
            "total_wall_time_seconds": runtime_seconds,
            "per_case_latency_seconds": latency_summary,
            "by_gold_cardinality": by_gold_cardinality,
        },
        "divergence_examples": divergence_examples,
        "per_case": per_case,
        "runtime_seconds": runtime_seconds,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        f"=== new_harness_graph (n={case_count}) ===",
        (
            f"overall          R@5={_fmt_pct(overall_r5)}  "
            f"Exact@5={_fmt_pct(overall_e5)}  "
            f"Exact@10={_fmt_pct(overall_e10)}  "
            f"Exact@20={_fmt_pct(overall_e20)}"
        ),
        f"wall_time_seconds={runtime_seconds:.1f}",
        (
            "latency_seconds "
            f"mean={latency_summary['mean']:.2f} "
            f"p50={latency_summary['p50']:.2f} "
            f"p95={latency_summary['p95']:.2f} "
            f"max={latency_summary['max']:.2f}"
        ),
    ]
    if timed_out_cases:
        lines.append(f"timed_out_cases={timed_out_cases}")
    for cardinality, metrics in sorted(by_gold_cardinality.items(), key=lambda item: int(item[0])):
        lines.append(
            f"cardinality={cardinality}    "
            f"R@5={_fmt_pct(float(metrics['recall_at_5']))}  "
            f"Exact@5={_fmt_pct(float(metrics['exact_at_5']))}  "
            f"Exact@10={_fmt_pct(float(metrics['exact_at_10']))}  "
            f"Exact@20={_fmt_pct(float(metrics['exact_at_20']))}   "
            f"(n={int(metrics['count'])})"
        )
    lines.append("")
    lines.append("Divergence examples")
    for example in divergence_examples:
        lines.append(
            f"- {example['case_id']}: gold={example['gold_set']} "
            f"top5={example['retrieved_top5']} missing={example['missing']}"
        )
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
