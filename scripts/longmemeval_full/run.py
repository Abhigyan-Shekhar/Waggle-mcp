from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import plan_longmemeval_run
from scripts import run_longmemeval_waggle_phase as legacy

from .conditions import ALL_CONDITIONS, ConditionConfig, normalize_condition, run_condition
from .context_builder import token_estimate
from .fixtures import capability_fixtures
from .ingestion import DEFAULT_AGENT_ID, build_case_graph, case_id_for
from .provenance import ConditionResult, file_sha256, git_commit, stable_json_sha


DEFAULT_READER_MODEL = "dry-run-reader"
DEFAULT_PRIMARY_JUDGE_MODEL = "dry-run-judge"
DEFAULT_SECONDARY_JUDGE_MODEL = "dry-run-second-judge"
PROMPT_VERSION = "longmemeval-full-capability-v1"
CONTEXT_ASSEMBLY_SAFETY_MARGIN_TOKENS = 16


class DeterministicEmbeddingModel:
    model_name = "deterministic-local"
    model_id = "deterministic-local:longmemeval-full-v1"

    def embed(self, text: str) -> Any:
        import numpy as np

        vector = np.zeros(32, dtype=np.float32)
        for token in str(text).lower().split():
            vector[sum(ord(ch) for ch in token) % len(vector)] += 1.0
        norm = np.linalg.norm(vector)
        return vector if norm == 0.0 else vector / norm

    def embed_batch(self, texts: list[str]) -> Any:
        import numpy as np

        return np.stack([self.embed(text) for text in texts], axis=0)

    def to_bytes(self, embedding: Any) -> bytes:
        import numpy as np

        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> Any:
        import numpy as np

        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: Any, b: Any) -> float:
        import numpy as np

        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conditions = expand_conditions(args.conditions)
    if not args.dry_run and not args.allow_paid:
        raise SystemExit(
            "Paid/non-dry-run execution requires --allow-paid. "
            "Freeze manifest, inspect config, confirm cost, and export GROQ_API_KEY first."
        )
    if args.allow_paid and args.dry_run:
        raise SystemExit("--allow-paid cannot be combined with --dry-run")
    if args.allow_paid and not os.getenv("GROQ_API_KEY"):
        raise SystemExit("Paid Groq execution requires GROQ_API_KEY in the environment.")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cases, dataset_meta = load_cases(args)
    if args.limit:
        cases = cases[: max(0, args.limit)]
    config_payload = {
        "dataset": args.dataset,
        "dataset_path": str(args.dataset_path or ""),
        "dataset_sha": dataset_meta["dataset_sha"],
        "conditions": conditions,
        "reader_context_budget": args.reader_context_budget,
        "context_assembly_safety_margin_tokens": CONTEXT_ASSEMBLY_SAFETY_MARGIN_TOKENS,
        "retrieval_limit": args.retrieval_limit,
        "max_tool_calls": args.max_tool_calls,
        "reader_model": args.reader_model,
        "primary_judge_model": args.primary_judge_model,
        "secondary_judge_model": args.secondary_judge_model,
        "secondary_judge_policy": args.secondary_judge_policy,
        "prompt_version": PROMPT_VERSION,
        "dry_run": args.dry_run,
        "allow_paid": args.allow_paid,
        "git_commit": git_commit(),
        "pricing_config": str(args.pricing_config or ""),
        "graph_cache_dir": str(args.graph_cache_dir or ""),
        "force_rebuild_graph_cache": args.force_rebuild_graph_cache,
        "defer_window_edges": args.defer_window_edges,
        "keep_graph_cache": args.keep_graph_cache,
    }
    config_payload["config_sha"] = stable_json_sha(config_payload)
    write_json(output_dir / "config.json", config_payload)
    write_json(
        output_dir / "frozen_case_manifest.json",
        {
            "dataset": args.dataset,
            "dataset_sha": dataset_meta["dataset_sha"],
            "case_count": len(cases),
            "cases": [
                {
                    "case_id": case_id_for(case, index),
                    "category": category_for(case),
                    "gold_support_ids": legacy._gold_support_ids(case),
                }
                for index, case in enumerate(cases)
            ],
        },
    )

    result_rows: list[dict[str, Any]] = load_jsonl(output_dir / "results.jsonl")
    retrieval_rows: list[dict[str, Any]] = load_jsonl(output_dir / "retrieval_traces.jsonl")
    tool_rows: list[dict[str, Any]] = load_jsonl(output_dir / "tool_traces.jsonl")
    reader_rows: list[dict[str, Any]] = load_jsonl(output_dir / "reader_requests.jsonl")
    judge_rows: list[dict[str, Any]] = load_jsonl(output_dir / "judge_requests.jsonl")
    completed_pairs = {(str(row.get("case_id")), str(row.get("condition"))) for row in result_rows}
    pricing = load_pricing_config(args.pricing_config)

    condition_config = ConditionConfig(
        reader_context_budget=max(1, args.reader_context_budget - CONTEXT_ASSEMBLY_SAFETY_MARGIN_TOKENS),
        retrieval_limit=args.retrieval_limit,
        max_tool_calls=args.max_tool_calls,
    )
    embedding_model = DeterministicEmbeddingModel() if args.dry_run else load_real_embedding_model()

    for index, case in enumerate(cases):
        case_id = case_id_for(case, index)
        if all((case_id, condition) in completed_pairs for condition in conditions):
            print(f"skip completed case {case_id}", file=sys.stderr, flush=True)
            continue
        case_start = time.perf_counter()
        case_graph = build_case_graph(
            case,
            embedding_model=embedding_model,
            agent_id=DEFAULT_AGENT_ID,
            cache_dir=args.graph_cache_dir,
            force_rebuild_cache=args.force_rebuild_graph_cache,
            progress=args.graph_ingest_progress,
            defer_window_edges=args.defer_window_edges,
        )
        try:
            for condition in conditions:
                if (case_id, condition) in completed_pairs:
                    print(f"skip completed {case_id} {condition}", file=sys.stderr, flush=True)
                    continue
                condition_result = run_condition(condition, case, case_graph, config=condition_config)
                row = build_result_row(
                    case,
                    case_index=index,
                    condition_result=condition_result,
                    config=config_payload,
                    dataset_meta=dataset_meta,
                    dry_run=args.dry_run,
                    elapsed_ms=(time.perf_counter() - case_start) * 1000,
                    pricing=pricing,
                )
                result_rows.append(row)
                retrieval_row = retrieval_trace_row(row, condition_result)
                tool_row = {"case_id": row["case_id"], "condition": row["condition"], "tool_trace": row["tool_trace"]}
                reader_row = {
                    "case_id": row["case_id"],
                    "condition": row["condition"],
                    "dry_run": args.dry_run,
                    "paid": not args.dry_run,
                    "request": {"model": args.reader_model, "prompt": row["reader_prompt"]},
                    "response": row.get("_reader_response", {"answer": row["answer"]}),
                }
                judge_row = {
                    "case_id": row["case_id"],
                    "condition": row["condition"],
                    "dry_run": args.dry_run,
                    "paid": not args.dry_run,
                    "request": {"model": args.primary_judge_model, "prompt": legacy._judge_prompt(case, row["answer"])},
                    "response": row["primary_judgment"],
                }
                retrieval_rows.append(retrieval_row)
                tool_rows.append(tool_row)
                reader_rows.append(reader_row)
                judge_rows.append(judge_row)
                append_jsonl(output_dir / "results.jsonl", [public_result_row(row)])
                append_jsonl(output_dir / "retrieval_traces.jsonl", [retrieval_row])
                append_jsonl(output_dir / "tool_traces.jsonl", [tool_row])
                append_jsonl(output_dir / "reader_requests.jsonl", [reader_row])
                append_jsonl(output_dir / "judge_requests.jsonl", [judge_row])
                completed_pairs.add((str(row["case_id"]), str(row["condition"])))
                print(
                    f"completed {len(completed_pairs)}/{len(cases) * len(conditions)} "
                    f"{row['case_id']} {row['condition']} correct={row['primary_judgment'].get('correct')}",
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            case_graph["tmpdir"].cleanup()
            if (
                args.graph_cache_dir
                and not args.keep_graph_cache
                and all((case_id, condition) in completed_pairs for condition in conditions)
            ):
                cleanup_case_graph_cache(args.graph_cache_dir, case_id)

    if not args.dry_run and args.secondary_judge_policy != "none":
        run_secondary_judges(
            cases=cases,
            rows=result_rows,
            judge_rows=judge_rows,
            model=args.secondary_judge_model,
            policy=args.secondary_judge_policy,
            pricing=pricing,
        )
    result_rows = [public_result_row(row) for row in result_rows]

    write_jsonl(output_dir / "results.jsonl", result_rows)
    write_jsonl(output_dir / "retrieval_traces.jsonl", retrieval_rows)
    write_jsonl(output_dir / "tool_traces.jsonl", tool_rows)
    write_jsonl(output_dir / "reader_requests.jsonl", reader_rows)
    write_jsonl(output_dir / "judge_requests.jsonl", judge_rows)
    write_summary_csv(output_dir / "summary.csv", result_rows)
    write_category_summary_csv(output_dir / "category_summary.csv", result_rows)
    write_budget_summary_csv(output_dir / "budget_summary.csv", result_rows)
    write_placeholder_csv(output_dir / "failure_analysis.csv", ["case_id", "condition", "failure_labels"])
    write_context_efficiency_csv(output_dir / "context_efficiency.csv", result_rows)
    write_graph_contribution_csv(output_dir / "graph_contribution.csv", result_rows)
    write_report(output_dir / "FULL_CAPABILITY_EVALUATION_REPORT.md", config_payload, result_rows)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run full-capability LongMemEval evaluation dry runs.")
    parser.add_argument("--dataset", default="targeted_stress_v2", choices=["longmemeval_s_existing", "longmemeval_m_fresh", "targeted_stress_v2"])
    parser.add_argument("--dataset-path", type=Path, default=None)
    parser.add_argument("--conditions", default="flat_transcript_vector,waggle_production_context")
    parser.add_argument("--reader-context-budget", type=int, default=4096)
    parser.add_argument("--retrieval-limit", type=int, default=10)
    parser.add_argument("--max-tool-calls", type=int, default=6)
    parser.add_argument("--output-dir", default="runs/longmemeval/full-capability")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--reader-model", default=DEFAULT_READER_MODEL)
    parser.add_argument("--primary-judge-model", default=DEFAULT_PRIMARY_JUDGE_MODEL)
    parser.add_argument("--secondary-judge-model", default=DEFAULT_SECONDARY_JUDGE_MODEL)
    parser.add_argument("--secondary-judge-policy", default="all_disagreements", choices=["none", "all_disagreements", "all_rows"])
    parser.add_argument("--pricing-config", type=Path, default=None)
    parser.add_argument("--graph-cache-dir", type=Path, default=None)
    parser.add_argument("--force-rebuild-graph-cache", action="store_true")
    parser.add_argument("--keep-graph-cache", action="store_true")
    parser.add_argument("--graph-ingest-progress", action="store_true")
    parser.add_argument(
        "--defer-window-edges",
        action="store_true",
        help="During case ingestion, derive context-window edges once after all turns instead of after every observe call.",
    )
    return parser.parse_args(argv)


def expand_conditions(raw: str) -> list[str]:
    names = [normalize_condition(item) for item in raw.split(",") if item.strip()]
    if not names or names == ["all"]:
        return list(ALL_CONDITIONS)
    return names


def cleanup_case_graph_cache(cache_dir: str | Path, case_id: str) -> None:
    cache_path = Path(cache_dir)
    for suffix in (".db", ".db-wal", ".db-shm", ".db.lock", ".complete.json"):
        path = cache_path / f"{case_id}{suffix}"
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def load_cases(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if args.dataset_path:
        cases = json.loads(args.dataset_path.read_text(encoding="utf-8"))
        if isinstance(cases, dict) and isinstance(cases.get("cases"), list):
            cases = cases["cases"]
        if not isinstance(cases, list):
            raise ValueError("dataset path must contain a JSON list or {'cases': [...]}")
        return cases, {"dataset_sha": file_sha256(args.dataset_path), "source": str(args.dataset_path)}
    if args.dataset == "targeted_stress_v2":
        cases = built_in_stress_v2_fixture()
        return cases, {"dataset_sha": stable_json_sha(cases), "source": "built_in_stress_v2_fixture"}
    raise ValueError(f"{args.dataset} requires --dataset-path in this implementation")


def built_in_stress_v2_fixture() -> list[dict[str, Any]]:
    return [
        {
            "question_id": "stress_v2_fixture_001",
            "question_type": "knowledge-update",
            "question": "How many free Hilton nights should I plan around?",
            "answer": "two free nights",
            "gold_support_ids": ["s2"],
            "haystack_session_ids": ["s1", "s2", "s3"],
            "haystack_dates": ["2026-01-01", "2026-01-15", "2026-01-20"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "I may have a single free Hilton night, but I need to verify it."},
                    {"role": "assistant", "content": "I will treat that as tentative until you confirm."},
                ],
                [
                    {"role": "user", "content": "I checked my Hilton account. I have two free nights, not one."},
                    {"role": "assistant", "content": "The confirmed current value is two free nights."},
                ],
                [
                    {"role": "user", "content": "For Paris, remind me to compare museum passes."},
                    {"role": "assistant", "content": "Noted."},
                ],
            ],
        },
        {
            "question_id": "stress_v2_fixture_002",
            "question_type": "single-session-user",
            "question": "Which exact audio task did I say was still missing?",
            "answer": "Sound effects",
            "gold_support_ids": ["s4"],
            "haystack_session_ids": ["s4", "s5"],
            "haystack_dates": ["2026-02-01", "2026-02-03"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "The launch checklist is: UI polish, docs, Sound effects, and credits."},
                    {"role": "assistant", "content": "I will keep the checklist wording intact."},
                ],
                [
                    {"role": "user", "content": "Also note that the project uses a blue theme."},
                    {"role": "assistant", "content": "Saved."},
                ],
            ],
        },
        *capability_fixtures(),
    ]


def build_result_row(
    case: dict[str, Any],
    *,
    case_index: int,
    condition_result: ConditionResult,
    config: dict[str, Any],
    dataset_meta: dict[str, Any],
    dry_run: bool,
    elapsed_ms: float,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    case_id = case_id_for(case, case_index)
    context = condition_result.context
    reader_prompt = build_reader_prompt(case, context)
    if dry_run:
        answer = dry_run_answer(case, context)
        reader_usage = {"input_tokens": 0, "output_tokens": 0}
        reader_response = {"answer": answer, "dry_run": True}
        judgment = dry_run_judge(case, answer)
        primary_judge_cost = 0.0
    else:
        answer, reader_usage, reader_response = groq_answer(reader_prompt, config["reader_model"])
        judgment = paid_primary_judge(case, answer, config["primary_judge_model"])
        primary_judge_cost = token_cost(
            pricing,
            "primary_judge",
            int(judgment.get("judge_input_tokens") or 0),
            int(judgment.get("judge_output_tokens") or 0),
        )
    reader_cost = token_cost(
        pricing,
        "reader",
        int(reader_usage.get("input_tokens") or 0),
        int(reader_usage.get("output_tokens") or 0),
    )
    condition_payload = condition_result.to_row_payload()
    return {
        "case_id": case_id,
        "dataset": config["dataset"],
        "dataset_sha": dataset_meta["dataset_sha"],
        "split": "dry_run" if dry_run else "paid_pilot",
        "category": category_for(case),
        "condition": condition_result.condition,
        "git_commit": config["git_commit"],
        "config_sha": config["config_sha"],
        "prompt_version": config["prompt_version"],
        "reader_model": config["reader_model"],
        "reader_temperature": 0,
        "primary_judge_model": config["primary_judge_model"],
        "secondary_judge_model": config["secondary_judge_model"],
        "embedding_model": DeterministicEmbeddingModel.model_id if dry_run else "configured-real-embedding-model",
        "retrieval_mode": condition_result.retrieval_mode,
        "context_budget": config["reader_context_budget"],
        "tool_budget": config["max_tool_calls"],
        "retrieved_node_ids": condition_payload["retrieved_node_ids"],
        "retrieved_transcript_ids": condition_payload["retrieved_transcript_ids"],
        "retrieved_edge_ids": condition_payload["retrieved_edge_ids"],
        "source_evidence_ids": condition_payload["source_evidence_ids"],
        "tool_trace": condition_payload["tool_trace"],
        "context_items": condition_payload["context_items"],
        "final_context_tokens": token_estimate(context),
        "total_tool_output_tokens": sum(token_estimate(json.dumps(item, default=str)) for item in condition_payload["tool_trace"]),
        "answer": answer,
        "primary_judgment": judgment,
        "secondary_judgment": {},
        "latency_ms": {**condition_payload["latency_ms"], "total_case_condition": elapsed_ms},
        "cost": {
            "reader_usd": round(reader_cost, 8),
            "primary_judge_usd": round(primary_judge_cost, 8),
            "secondary_judge_usd": 0.0,
            "total_usd": round(reader_cost + primary_judge_cost, 8),
        },
        "failure_labels": failure_labels_for_judgment(judgment, dry_run=dry_run),
        "reader_prompt": reader_prompt,
        "adapter_notes": condition_payload["adapter_notes"],
        "_reader_response": reader_response,
    }


def build_reader_prompt(case: dict[str, Any], context: str) -> str:
    return (
        "Answer using only the supplied memory evidence. Prefer currently valid information. "
        "Preserve exact wording when requested. Do not invent unsupported details.\n\n"
        f"Question: {legacy._question(case)}\n\n"
        f"Memory Evidence:\n{context}\n\n"
        "Answer:"
    )


def dry_run_answer(case: dict[str, Any], context: str) -> str:
    return "DRY_RUN_NO_READER_CALL"


def dry_run_judge(case: dict[str, Any], answer: str) -> dict[str, Any]:
    return {"correct": None, "label": "dry_run_unscored", "dry_run": True}


def groq_answer(prompt: str, model: str) -> tuple[str, dict[str, int], dict[str, Any]]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is required for paid Groq calls.")
    max_tokens = int(os.getenv("GROQ_MAX_TOKENS", "400"))
    backoff = 5.0
    last_error = ""
    for _ in range(8):
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "waggle-longmemeval-full/1.0",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                },
                timeout=120.0,
            )
        except requests.exceptions.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"Groq request error; retrying in {backoff:.0f}s: {last_error}", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        if response.status_code == 429:
            last_error = response.text
            print(f"Groq rate limit; retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Groq request failed with status {response.status_code}: {response.text}")
        body = response.json()
        answer = str(body["choices"][0]["message"]["content"] or "").strip()
        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or token_estimate(prompt))
        output_tokens = int(usage.get("completion_tokens") or token_estimate(answer))
        return answer, {"input_tokens": input_tokens, "output_tokens": output_tokens}, {"answer": answer, "usage": usage}
    raise RuntimeError(f"Groq call failed after retries: {last_error}")


def paid_primary_judge(case: dict[str, Any], answer: str, model: str) -> dict[str, Any]:
    prompt = legacy._judge_prompt(case, answer)
    judge_answer, usage, _ = groq_answer(prompt, model)
    label_text = judge_answer.strip().lower()
    score = 1 if label_text.startswith("yes") else 0
    if label_text.startswith("no"):
        score = 0
    return {
        "score": score,
        "correct": bool(score),
        "mode": "paper-style-llm-judge",
        "label": "yes" if score else "no",
        "judge_model": model,
        "judge_response": judge_answer,
        "judge_input_tokens": usage["input_tokens"],
        "judge_output_tokens": usage["output_tokens"],
    }


def run_secondary_judges(
    *,
    cases: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    judge_rows: list[dict[str, Any]],
    model: str,
    policy: str,
    pricing: dict[str, Any] | None,
) -> None:
    cases_by_id = {case_id_for(case, index): case for index, case in enumerate(cases)}
    rows_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_case.setdefault(str(row["case_id"]), []).append(row)
    for case_id, case_rows in rows_by_case.items():
        scores = {row.get("primary_judgment", {}).get("score") for row in case_rows}
        should_judge = policy == "all_rows" or (policy == "all_disagreements" and len(scores) > 1)
        if not should_judge:
            continue
        case = cases_by_id[case_id]
        for row in case_rows:
            judgment = paid_primary_judge(case, str(row.get("answer") or ""), model)
            secondary_cost = token_cost(
                pricing,
                "secondary_judge",
                int(judgment.get("judge_input_tokens") or 0),
                int(judgment.get("judge_output_tokens") or 0),
            )
            row["secondary_judgment"] = judgment
            row["cost"]["secondary_judge_usd"] = round(secondary_cost, 8)
            row["cost"]["total_usd"] = round(
                float(row["cost"].get("reader_usd") or 0.0)
                + float(row["cost"].get("primary_judge_usd") or 0.0)
                + secondary_cost,
                8,
            )
            judge_rows.append(
                {
                    "case_id": row["case_id"],
                    "condition": row["condition"],
                    "dry_run": False,
                    "paid": True,
                    "secondary": True,
                    "request": {"model": model},
                    "response": judgment,
                }
            )


def failure_labels_for_judgment(judgment: dict[str, Any], *, dry_run: bool) -> list[str]:
    if dry_run:
        return ["dry_run_not_scored"]
    if judgment.get("correct") is True or judgment.get("score") == 1:
        return []
    return ["reader_or_retrieval_failure_unattributed"]


def load_pricing_config(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def token_cost(pricing: dict[str, Any] | None, component: str, input_tokens: int, output_tokens: int) -> float:
    if not pricing:
        return 0.0
    item = pricing.get(component) or {}
    return (input_tokens / 1_000_000.0) * float(item.get("input_per_million_usd") or 0.0) + (
        output_tokens / 1_000_000.0
    ) * float(item.get("output_per_million_usd") or 0.0)


def category_for(case: dict[str, Any]) -> str:
    return str(case.get("question_type") or case.get("category") or plan_longmemeval_run._category(case))


def retrieval_trace_row(row: dict[str, Any], condition_result: ConditionResult) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "condition": row["condition"],
        "retrieval_mode": row["retrieval_mode"],
        "retrieved_node_ids": row["retrieved_node_ids"],
        "retrieved_transcript_ids": row["retrieved_transcript_ids"],
        "retrieved_edge_ids": row["retrieved_edge_ids"],
        "source_evidence_ids": row["source_evidence_ids"],
        "context_items": [item.to_dict() for item in condition_result.context_items],
    }


def load_real_embedding_model() -> Any:
    EmbeddingModel, _ = legacy._load_waggle_classes()
    return EmbeddingModel()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        handle.flush()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def public_result_row(row: dict[str, Any]) -> dict[str, Any]:
    public = dict(row)
    public.pop("_reader_response", None)
    return public


def write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["condition"], []).append(row)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["condition", "rows", "qa_correct", "avg_context_tokens", "avg_tool_calls"])
        writer.writeheader()
        for condition, condition_rows in sorted(grouped.items()):
            writer.writerow(
                {
                    "condition": condition,
                    "rows": len(condition_rows),
                    "qa_correct": sum(1 for row in condition_rows if row["primary_judgment"].get("correct")),
                    "avg_context_tokens": round(sum(row["final_context_tokens"] for row in condition_rows) / max(1, len(condition_rows)), 2),
                    "avg_tool_calls": round(sum(len(row["tool_trace"]) for row in condition_rows) / max(1, len(condition_rows)), 2),
                }
            )


def write_category_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["condition"], row["category"]), []).append(row)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["condition", "category", "rows", "qa_correct"])
        writer.writeheader()
        for (condition, category), group_rows in sorted(grouped.items()):
            writer.writerow(
                {
                    "condition": condition,
                    "category": category,
                    "rows": len(group_rows),
                    "qa_correct": sum(1 for row in group_rows if row["primary_judgment"].get("correct")),
                }
            )


def write_budget_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "condition", "context_budget", "final_context_tokens", "within_budget"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "case_id": row["case_id"],
                    "condition": row["condition"],
                    "context_budget": row["context_budget"],
                    "final_context_tokens": row["final_context_tokens"],
                    "within_budget": row["final_context_tokens"] <= row["context_budget"],
                }
            )


def write_context_efficiency_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "condition", "final_context_tokens", "context_items", "tool_output_tokens"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "case_id": row["case_id"],
                    "condition": row["condition"],
                    "final_context_tokens": row["final_context_tokens"],
                    "context_items": len(row["context_items"]),
                    "tool_output_tokens": row["total_tool_output_tokens"],
                }
            )


def write_graph_contribution_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "condition", "nodes", "edges", "transcripts"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "case_id": row["case_id"],
                    "condition": row["condition"],
                    "nodes": len(row["retrieved_node_ids"]),
                    "edges": len(row["retrieved_edge_ids"]),
                    "transcripts": len(row["retrieved_transcript_ids"]),
                }
            )


def write_placeholder_csv(path: Path, headers: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()


def write_report(path: Path, config: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    conditions = sorted({row["condition"] for row in rows})
    lines = [
        "# Full-Capability LongMemEval Evaluation Report",
        "",
        "This report was generated from a dry run. It validates ingestion, retrieval/context paths, budgets, and provenance serialization without paid reader or judge calls.",
        "",
        "## Configuration",
        "",
        f"- Dataset: `{config['dataset']}`",
        f"- Dataset SHA: `{config['dataset_sha']}`",
        f"- Git commit: `{config['git_commit']}`",
        f"- Config SHA: `{config['config_sha']}`",
        f"- Context budget: `{config['reader_context_budget']}`",
        f"- Conditions: {', '.join(f'`{condition}`' for condition in conditions)}",
        "",
        "## Production Code Path Mapping",
        "",
        "| Evaluation operation | Production function called | Adapter used? | Behavioural differences |",
        "| -------------------- | -------------------------- | ------------: | ----------------------- |",
        "| Ingestion | `MemoryGraph.observe_conversation` | Thin case loader | Per-case temporary SQLite graph |",
        "| Flat transcript vector | `MemoryGraph.search_transcript_records` | Thin formatter | No graph nodes or edges included |",
        "| Existing graph-guided context | legacy `_context_from_waggle(... condition='waggle_full')` | Historical adapter | Renamed only in new outputs |",
        "| Production hybrid query | `MemoryGraph.query(... retrieval_mode='hybrid')` | Thin provenance wrapper | Mirrors MCP `query_graph` default retrieval mode |",
        "| Full production context | `RecursiveContextController.build_context` | Thin budget/provenance wrapper | No post-build large session append |",
        "| Agentic MCP | `prime_context`, `query`, `get_related` | Deterministic bounded tool runner | Simulates tool sequence without LLM tool selection in dry run |",
        "",
        "## Current Limitation",
        "",
        "Paid reader/judge execution is intentionally disabled in this implementation pass. Before paid evaluation, freeze the manifest and confirm model/cost configuration.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
