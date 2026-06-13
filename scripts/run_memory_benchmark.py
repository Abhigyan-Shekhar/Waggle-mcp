#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from waggle.benchmark_cache import BenchmarkCache
from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph
from waggle.memory_benchmark import (
    index_case_into_waggle,
    load_locomo,
    load_longmemeval,
    run_case_cached,
    summarize,
    write_report,
)


def make_subprocess_caller(command_template: str) -> Callable[[str], str]:
    def call(prompt: str) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_path = Path(tmpdir) / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            command = command_template.format(prompt_file=str(prompt_path), prompt=prompt)
            completed = subprocess.run(shlex.split(command), check=False, capture_output=True, text=True)
            if completed.returncode != 0:
                raise RuntimeError(f"Command failed with exit code {completed.returncode}: {completed.stderr.strip()}")
            return completed.stdout.strip()

    return call


def make_groq_caller(model: str, *, temperature: float = 0.0, max_tokens: int = 400) -> Callable[[str], str]:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    def call(prompt: str) -> str:
        backoff = 2.0
        for _ in range(5):
            try:
                response = client.chat.completions.create(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                message = str(exc).lower()
                if "rate" in message or "429" in message or "tpm" in message:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                raise
        raise RuntimeError(f"Groq call failed after retries for model {model}")

    return call


def dataset_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_model_caller(
    *,
    provider: str,
    command_template: str,
    model_name: str,
    max_tokens: int,
) -> tuple[Callable[[str], str], str]:
    if command_template.strip():
        return make_subprocess_caller(command_template), f"subprocess:{command_template}"
    if provider == "groq":
        return make_groq_caller(model_name, max_tokens=max_tokens), model_name
    raise RuntimeError("A subprocess command template is required unless provider is groq.")


def make_graph(embedding_model: EmbeddingModel) -> MemoryGraph:
    tmpdir = tempfile.TemporaryDirectory()
    graph = MemoryGraph(
        Path(tmpdir.name) / "memory-benchmark.db",
        embedding_model,
        dedup_similarity_threshold=1.01,
        dedup_same_label_threshold=1.01,
    )
    graph._memory_benchmark_tmpdir = tmpdir
    return graph


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LongMemEval or LoCoMo over Waggle retrieval arms.")
    parser.add_argument("--benchmark", required=True, choices=["longmemeval", "locomo"])
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument(
        "--arms",
        nargs="+",
        required=True,
        choices=["waggle_graph", "naive_rag", "full_context", "no_context"],
    )
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--provider", choices=["groq", "subprocess"], default="groq")
    parser.add_argument("--answer-model", default="llama-3.1-8b-instant")
    parser.add_argument("--judge-model", default="llama-3.3-70b-versatile")
    parser.add_argument("--answer-command", default="")
    parser.add_argument("--judge-command", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retrieval-limit", type=int, default=15)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("benchmarks/cache"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    args = parser.parse_args()

    cache: BenchmarkCache | None = None
    if not args.no_cache:
        if args.clear_cache and args.cache_dir.exists():
            shutil.rmtree(args.cache_dir)
            print(f"[cache] cleared {args.cache_dir}")
        cache = BenchmarkCache(args.cache_dir)
        print(f"[cache] using {args.cache_dir} ({cache.stats()['entries']} prior entries)")
    else:
        print("[cache] disabled")

    if args.benchmark == "longmemeval":
        cases = load_longmemeval(args.dataset)
    else:
        cases = load_locomo(args.dataset)
    if args.limit:
        cases = cases[: args.limit]
    print(f"[load] {len(cases)} cases from {args.dataset}")

    answer_call, answer_model_name = build_model_caller(
        provider=args.provider,
        command_template=args.answer_command,
        model_name=args.answer_model,
        max_tokens=400,
    )
    judge_call, judge_model_name = build_model_caller(
        provider=args.provider,
        command_template=args.judge_command,
        model_name=args.judge_model,
        max_tokens=120,
    )
    dataset_digest = dataset_sha256(args.dataset)

    embedding_model = EmbeddingModel(args.embedding_model)
    graph = make_graph(embedding_model)

    results = []
    hits = 0
    misses = 0
    for index, case in enumerate(cases, start=1):
        indexed = index_case_into_waggle(case, graph)
        for arm in args.arms:
            result, was_hit = run_case_cached(
                indexed,
                graph,
                benchmark=args.benchmark,
                arm=arm,
                answer_model_call=answer_call,
                judge_model_call=judge_call,
                answer_model_name=answer_model_name,
                judge_model_name=judge_model_name,
                retrieval_limit=args.retrieval_limit,
                cache=cache,
                cache_extra={"dataset_sha256": dataset_digest},
            )
            results.append(result)
            if was_hit:
                hits += 1
                source = "CACHE"
            else:
                misses += 1
                source = "FRESH"
            status = "✓" if result.correct else "✗"
            print(
                f"[{index}/{len(cases)}] {arm:<14} {status} {source} ({result.question_type}) "
                f"chunks={result.retrieved_count} latency={result.latency_seconds:.1f}s"
            )

    summary = summarize(results)
    write_report(args.output, results, summary)
    print(f"\n[cache] hits={hits} misses={misses}")
    print("\n=== Summary ===")
    for arm, bucket in summary.items():
        print(f"  {arm:<14} acc={bucket['accuracy']*100:.1f}%  ({bucket['correct']}/{bucket['total']})")
        for question_type, entry in bucket["by_question_type"].items():
            print(f"     · {question_type:<22} {entry['correct']}/{entry['total']}  ({entry['accuracy']*100:.1f}%)")
    print(f"\n[done] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
