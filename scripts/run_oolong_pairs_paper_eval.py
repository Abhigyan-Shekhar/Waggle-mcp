"""
Paper-aligned OOLONG-Pairs evaluation for Waggle.

This script follows the OOLONG-Pairs benchmark shape described in the RLM paper:
- evaluate the 20 synthetic pair-aggregation tasks
- use exact-match scoring on the set of returned pairs
- compare a full-context baseline against Waggle retrieval

It does NOT implement recursive language models. It is a Waggle-vs-full-context
benchmark over the OOLONG-Pairs task family.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))

import groq

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph
from waggle.oolong_benchmark import _index_context_window, load_oolong_examples

DEFAULT_DATASET = "benchmarks/data/oolong_20.jsonl"
DEFAULT_DB_PATH = "/private/tmp/oolong-pairs-paper-eval.db"


@dataclass
class CaseResult:
    example_id: str
    mode: str
    retrieved_node_count: int
    retrieved_tokens: int
    prompt_tokens: int
    latency_s: float
    exact_match: bool
    gold_pairs: list[str]
    predicted_pairs: list[str]


def _normalize_pairs(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        text = str(raw).strip()
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                values = [str(item).strip() for item in parsed if str(item).strip()]
            else:
                values = [part.strip() for part in text.split("|") if part.strip()]
        except Exception:
            values = [part.strip() for part in text.split("|") if part.strip()]

    normalized: set[str] = set()
    for value in values:
        match = re.match(r"\((\d+)\s*,\s*(\d+)\)", value)
        if match is None:
            continue
        first = int(match.group(1))
        second = int(match.group(2))
        normalized.add(f"({min(first, second)}, {max(first, second)})")
    return sorted(normalized)


def _extract_pairs_from_llm(raw: str) -> list[str]:
    pairs: set[str] = set()
    for line in raw.splitlines():
        match = re.search(r"\((\d+)\s*,\s*(\d+)\)", line.strip())
        if match is None:
            continue
        first = int(match.group(1))
        second = int(match.group(2))
        pairs.add(f"({min(first, second)}, {max(first, second)})")
    return sorted(pairs)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.split())) if text.strip() else 0


def build_prompt(context_text: str, question: str) -> str:
    return (
        "You are solving an OOLONG-Pairs benchmark task using context retrieved from Waggle memory.\n"
        "The text block below is Waggle-retrieved evidence. Treat it as your working memory.\n"
        "You do not have any external tools in this call. You must solve the task strictly from the Waggle evidence provided.\n\n"
        "Required method:\n"
        "1. Read every example in the Waggle evidence.\n"
        "2. Infer the label of each Text entry from semantics.\n"
        "3. Build the relevant set of user IDs that satisfy the condition in the question.\n"
        "4. Produce all valid unique pairs from that set.\n"
        "5. Never invent a user ID not present in the Waggle evidence.\n"
        "6. Never output self-pairs like (107, 107).\n"
        "7. If the evidence is insufficient, return an empty answer rather than guessing.\n\n"
        "Return only the final answer as newline-separated pairs in the form (user_id_1, user_id_2).\n"
        "Do not include reasoning or explanation in the final output.\n\n"
        f"Waggle evidence:\n{context_text}\n\n"
        f"Task:\n{question}\n"
    )


def build_graph(dataset_path: str, db_path: str, embedding_model: EmbeddingModel) -> tuple[MemoryGraph, list]:
    if os.path.exists(db_path):
        os.remove(db_path)
    graph = MemoryGraph(
        db_path=db_path,
        embedding_model=embedding_model,
        dedup_similarity_threshold=1.01,
        dedup_same_label_threshold=1.01,
    )
    examples = load_oolong_examples(dataset_path, dataset_kind="synth")
    indexed: set[str] = set()
    for example in examples:
        if example.context_window_id in indexed:
            continue
        _index_context_window(graph, example, project="oolong-pairs", chunk_lines=12, overlap_lines=3)
        indexed.add(example.context_window_id)
    return graph, examples


def call_groq(client: groq.Groq, prompt: str, *, model: str, max_tokens: int) -> str:
    for attempt in range(6):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=max_tokens,
            )
            return (response.choices[0].message.content or "").strip()
        except groq.RateLimitError as exc:
            message = str(exc)
            wait_match = re.search(r"try again in ([0-9.]+)s", message)
            wait_seconds = float(wait_match.group(1)) + 1.0 if wait_match else min(60.0, 5.0 * (2 ** attempt))
            print(f"\n[rate limit] sleeping {wait_seconds:.1f}s before retry", flush=True)
            time.sleep(wait_seconds)
    return ""


def run_case(
    client: groq.Groq,
    graph: MemoryGraph,
    example,
    *,
    mode: str,
    model: str,
    max_nodes: int,
    max_tokens: int,
) -> CaseResult:
    if mode == "full_context":
        context_text = example.context_text
        retrieved_node_count = 1
        retrieved_tokens = _estimate_tokens(context_text)
    elif mode == "waggle_topk":
        result = graph.query(
            query=example.question,
            max_nodes=max_nodes,
            max_depth=1,
            retrieval_mode="graph",
            project="oolong-pairs",
            session_id=example.context_window_id,
        )
        chunks = [node.content for node in result.nodes]
        context_text = "\n\n".join(chunks)
        retrieved_node_count = len(chunks)
        retrieved_tokens = sum(_estimate_tokens(chunk) for chunk in chunks)
    elif mode == "waggle_query_aggregate":
        result = graph.aggregate(
            query=example.question,
            max_nodes=max_nodes,
            max_depth=0,
            project="oolong-pairs",
            session_id=example.context_window_id,
        )
        chunks = [node.content for node in result.nodes]
        context_text = "\n\n".join(chunks)
        retrieved_node_count = len(chunks)
        retrieved_tokens = sum(_estimate_tokens(chunk) for chunk in chunks)
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    prompt = build_prompt(context_text, example.question)
    prompt_tokens = _estimate_tokens(prompt)
    started = time.time()
    raw_answer = call_groq(client, prompt, model=model, max_tokens=max_tokens)
    latency_s = time.time() - started

    gold_pairs = _normalize_pairs(example.raw_answer)
    predicted_pairs = _extract_pairs_from_llm(raw_answer)

    return CaseResult(
        example_id=example.example_id,
        mode=mode,
        retrieved_node_count=retrieved_node_count,
        retrieved_tokens=retrieved_tokens,
        prompt_tokens=prompt_tokens,
        latency_s=latency_s,
        exact_match=predicted_pairs == gold_pairs,
        gold_pairs=gold_pairs,
        predicted_pairs=predicted_pairs,
    )


def summarize(mode: str, cases: list[CaseResult]) -> dict[str, object]:
    correct = sum(1 for case in cases if case.exact_match)
    total = len(cases)
    return {
        "mode": mode,
        "case_count": total,
        "correct": correct,
        "accuracy_pct": round((100.0 * correct / total), 1) if total else 0.0,
        "avg_retrieved_tokens": round(sum(case.retrieved_tokens for case in cases) / total) if total else 0,
        "avg_prompt_tokens": round(sum(case.prompt_tokens for case in cases) / total) if total else 0,
        "avg_latency_s": round(sum(case.latency_s for case in cases) / total, 2) if total else 0.0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a paper-aligned OOLONG-Pairs benchmark for Waggle.")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--model", default="llama-3.1-8b-instant")
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--modes", default="full_context,waggle_topk,waggle_query_aggregate")
    parser.add_argument("--max-nodes", type=int, default=64)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--output", default="benchmarks/data/oolong_pairs_paper_eval_report.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GROQ_API_KEY is required.")

    client = groq.Groq(api_key=api_key)
    embedding_model = EmbeddingModel(args.embedding_model)
    graph, examples = build_graph(args.dataset, args.db_path, embedding_model)
    if args.limit is not None:
        examples = examples[: args.limit]
    modes = [mode.strip() for mode in str(args.modes).split(",") if mode.strip()]
    if not modes:
        raise SystemExit("At least one mode is required.")

    print("\n" + "=" * 84)
    print("  OOLONG-PAIRS PAPER-ALIGNED EVAL (Groq + Waggle)")
    print("=" * 84)
    print(f"\nDataset: {args.dataset}")
    print(f"Cases:   {len(examples)}")
    print(f"Model:   {args.model}")
    print(f"Embeds:  {args.embedding_model}")

    results_by_mode: dict[str, list[CaseResult]] = {mode: [] for mode in modes}
    total_calls = len(examples) * len(results_by_mode)
    done = 0

    for example in examples:
        for mode in modes:
            print(f"[{done + 1}/{total_calls}] {example.example_id} | mode={mode}", end=" ", flush=True)
            case = run_case(
                client,
                graph,
                example,
                mode=mode,
                model=args.model,
                max_nodes=args.max_nodes,
                max_tokens=args.max_tokens,
            )
            results_by_mode[mode].append(case)
            print(
                f"chunks={case.retrieved_node_count} prompt_tok={case.prompt_tokens} "
                f"{'✅' if case.exact_match else '❌'} ({case.latency_s:.1f}s)"
            )
            done += 1
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

    summaries = {mode: summarize(mode, cases) for mode, cases in results_by_mode.items()}

    print("\n" + "=" * 84)
    print("  SUMMARY")
    print("=" * 84)
    for mode in modes:
        summary = summaries[mode]
        print(
            f"{mode:<14} accuracy={summary['accuracy_pct']:>5}% "
            f"correct={summary['correct']}/{summary['case_count']} "
            f"avg_retrieved_tokens={summary['avg_retrieved_tokens']} "
            f"avg_prompt_tokens={summary['avg_prompt_tokens']} "
            f"avg_latency={summary['avg_latency_s']}s"
        )

    output = {
        "dataset": args.dataset,
        "model": args.model,
        "max_nodes": args.max_nodes,
        "max_tokens": args.max_tokens,
        "limit": args.limit,
        "summaries": summaries,
        "cases": {
            mode: [asdict(case) for case in cases]
            for mode, cases in results_by_mode.items()
        },
    }
    Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nSaved report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
