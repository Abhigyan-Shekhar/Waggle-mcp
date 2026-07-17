#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import requests
import sys
import time
from pathlib import Path
from typing import Any

import plan_longmemeval_run
import validate_longmemeval_artifacts

DEFAULT_MOCK_CONDITIONS = ["full_context", "flat_vector", "waggle_full"]
DEFAULT_READER_MODEL = "gemini-2.5-flash"
DRY_RUN_READER_MODEL = "dry-run-reader"
DRY_RUN_JUDGE_MODEL = "dry-run-judge"


def _token_estimate(text: str) -> int:
    tokens = re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)
    return max(1, int(len(tokens) * 1.25))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _case_id(case: dict[str, Any], index: int) -> str:
    return plan_longmemeval_run._case_id(case, index)


def _case_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_case_text(item) for item in value if _case_text(item))
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("session_id", "speaker", "role", "content", "text", "message", "utterance"):
            if key in value:
                text = _case_text(value[key])
                if text:
                    parts.append(f"{key}: {text}")
        if parts:
            return " | ".join(parts)
        return " ".join(_case_text(item) for item in value.values() if _case_text(item))
    return str(value)


def _question(case: dict[str, Any]) -> str:
    for key in ("question", "query", "input", "prompt"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Answer the LongMemEval case question."


def _question_date(case: dict[str, Any]) -> str:
    for key in ("question_date", "date", "query_date"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _gold_answer(case: dict[str, Any]) -> str:
    for key in ("answer", "gold_answer", "target", "reference_answer"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _gold_support_ids(case: dict[str, Any]) -> list[str]:
    for key in ("gold_support_ids", "answer_session_ids", "support_ids", "evidence_ids"):
        value = case.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    metadata = case.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("gold_support_ids")
        if isinstance(value, list):
            return [str(item) for item in value]
    return []


def _event_dates(item: Any) -> list[str]:
    if isinstance(item, dict):
        for key in ("eventDate", "event_date", "event_dates"):
            value = item.get(key)
            if isinstance(value, list):
                return [str(entry) for entry in value if str(entry).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
    return []


def _document_date(item: Any) -> str | None:
    if isinstance(item, dict):
        for key in ("documentDate", "document_date", "date", "session_date", "timestamp"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _memory_summary(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    for separator in (". ", "? ", "! ", "\n"):
        head = normalized.split(separator, 1)[0].strip()
        if head:
            return head[:180]
    return normalized[:180]


def _history_items(case: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("haystack_sessions", "sessions", "history", "messages", "conversation", "transcript", "memory"):
        value = case.get(key)
        if not value:
            continue
        if isinstance(value, list):
            items: list[dict[str, Any]] = []
            for index, item in enumerate(value, start=1):
                session_id = ""
                if isinstance(item, dict):
                    raw_id = item.get("session_id") or item.get("id") or item.get("sid")
                    if raw_id is not None:
                        session_id = str(raw_id)
                if not session_id:
                    session_id = f"{key}-{index}"
                text = _case_text(item)
                if text:
                    items.append(
                        {
                            "session_id": session_id,
                            "text": text,
                            "memory": _memory_summary(text),
                            "documentDate": _document_date(item),
                            "eventDate": _event_dates(item),
                        }
                    )
            if items:
                return items
        text = _case_text(value)
        if text:
            return [{"session_id": key, "text": text, "memory": _memory_summary(text), "documentDate": None, "eventDate": []}]
    fallback = _case_text({k: v for k, v in case.items() if k not in {"answer", "gold_answer", "target"}})
    return [{"session_id": "case", "text": fallback, "memory": _memory_summary(fallback), "documentDate": None, "eventDate": []}] if fallback else []


def _format_retrieved_context(items: list[dict[str, Any]], *, include_memory: bool) -> str:
    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        lines = [f"Result {index}"]
        if include_memory:
            lines.append(f"Memory: {item['memory']}")
        lines.append(f"Chunk [{item['session_id']}]:")
        lines.append(item["text"])
        temporal_lines: list[str] = []
        if item.get("documentDate"):
            temporal_lines.append(f"documentDate: {item['documentDate']}")
        event_dates = item.get("eventDate") or []
        if event_dates:
            temporal_lines.append(f"eventDate: {', '.join(event_dates)}")
        if temporal_lines:
            lines.append("Temporal Context:")
            lines.extend(temporal_lines)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _context_for_condition(case: dict[str, Any], condition: str) -> tuple[str, list[str], str]:
    items = _history_items(case)
    if condition == "full_context":
        context = "\n\n".join(f"[{item['session_id']}]\n{item['text']}" for item in items)
        return context, [item["session_id"] for item in items], "full_history"

    support_ids = set(_gold_support_ids(case))
    selected: list[dict[str, Any]] = []
    if support_ids:
        selected.extend(item for item in items if item["session_id"] in support_ids)
    if len(selected) < 5:
        selected.extend(item for item in items if item not in selected)
    selected = selected[:5]
    if condition == "flat_vector":
        return _format_retrieved_context(selected, include_memory=False), [item["session_id"] for item in selected], "source_chunk_only"
    return _format_retrieved_context(selected, include_memory=True), [item["session_id"] for item in selected], "memory_plus_source_chunk"


def _build_prompt(case: dict[str, Any], condition: str) -> tuple[str, str, list[str], str]:
    context, retrieved_support_ids, context_mode = _context_for_condition(case, condition)
    if condition == "full_context":
        prompt = (
            "You are a question-answering system. Based on the retrieved context below, answer the question.\n\n"
            f"Question: {_question(case)}\n"
            f"Question Date: {_question_date(case)}\n\n"
            "Retrieved Context:\n"
            f"{context}\n\n"
            "Instructions:\n"
            "If the context contains enough information to answer the question, provide a clear, concise answer.\n"
            "If the context does not contain enough information, respond with \"I don't know\".\n"
            "Base your answer only on the provided context.\n\n"
            "Answer:"
        )
        return prompt, context, retrieved_support_ids, context_mode

    prompt = (
        "You are a question-answering system. Based on the retrieved context below, answer the question.\n\n"
        f"Question: {_question(case)}\n"
        f"Question Date: {_question_date(case)}\n\n"
        "Retrieved Context:\n"
        f"{context}\n\n"
        "Understanding the Context:\n"
        "The context contains search results from a memory system.\n"
        "Memory: a high-level summary or atomic fact.\n"
        "Chunks: the raw source material and the primary source for specifics.\n"
        "Temporal Context: use documentDate for when something was said and eventDate for when it occurred.\n\n"
        "Instructions:\n"
        "Start by scanning memory titles if present to find relevant results.\n"
        "Read the chunks carefully because they contain the raw details.\n"
        "Use temporal context to reason about timing and updates.\n"
        "Synthesize information across multiple results when needed.\n"
        "If the context does not contain enough information, respond with \"I don't know\".\n"
        "Base your answer only on the provided context.\n"
        "Prioritize information from chunks because they are the source material.\n\n"
        "Answer:"
    )
    return prompt, context, retrieved_support_ids, context_mode


def _dry_run_answer(case: dict[str, Any], condition: str) -> str:
    gold = _gold_answer(case)
    if gold:
        return f"DRY_RUN_ANSWER: {gold}"
    return f"DRY_RUN_ANSWER for {_case_id(case, 0)} under {condition}"


def _gemini_answer(prompt: str, model: str) -> tuple[str, int, int]:
    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("Gemini mode requires the google-genai package: pip install google-genai") from exc

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Gemini mode requires GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    answer = response.text or ""
    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) or _token_estimate(prompt)
    output_tokens = getattr(usage, "candidates_token_count", None) or _token_estimate(answer)
    return answer, int(input_tokens), int(output_tokens)


def _groq_answer(prompt: str, model: str) -> tuple[str, int, int]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Groq mode requires GROQ_API_KEY")

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "waggle-longmemeval-mock/1.0",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 400,
        },
        timeout=60.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Groq request failed with status {response.status_code}: {response.text}")

    body = response.json()
    answer = str(body["choices"][0]["message"]["content"] or "").strip()
    input_tokens = _token_estimate(prompt)
    output_tokens = _token_estimate(answer)
    return answer, int(input_tokens), int(output_tokens)


def run_mock_phase(
    *,
    dataset: Path,
    split_plan: Path,
    output: Path,
    conditions: list[str],
    mode: str,
    reader_model: str,
    judge_model: str,
    prompt_version: str,
    input_price_per_mtok: float,
    output_price_per_mtok: float,
) -> int:
    cases = plan_longmemeval_run._load_cases(dataset)
    case_by_id = {_case_id(case, index): case for index, case in enumerate(cases, start=1)}
    plan = _load_json(split_plan)
    dataset_sha256 = plan.get("dataset_sha256") or plan_longmemeval_run._sha256(dataset)
    mock_refs = plan.get("splits", {}).get("mock", [])
    if not isinstance(mock_refs, list) or not mock_refs:
        raise ValueError("split plan must contain a non-empty splits.mock list")

    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output.open("w", encoding="utf-8") as handle:
        for ref in mock_refs:
            if not isinstance(ref, dict) or "case_id" not in ref:
                raise ValueError("split plan mock entries must be objects with case_id")
            case_id = str(ref["case_id"])
            case = case_by_id.get(case_id)
            if case is None:
                raise ValueError(f"case_id {case_id!r} from split plan is missing from dataset")
            category = str(ref.get("category") or plan_longmemeval_run._category(case))

            for condition in conditions:
                started = time.perf_counter()
                prompt, context, retrieved_support_ids, context_mode = _build_prompt(case, condition)
                if mode == "gemini":
                    answer, input_tokens, output_tokens = _gemini_answer(prompt, reader_model)
                    effective_reader_model = reader_model
                    effective_judge_model = judge_model
                elif mode == "groq":
                    answer, input_tokens, output_tokens = _groq_answer(prompt, reader_model)
                    effective_reader_model = reader_model
                    effective_judge_model = judge_model
                else:
                    answer = _dry_run_answer(case, condition)
                    input_tokens = _token_estimate(prompt)
                    output_tokens = _token_estimate(answer)
                    effective_reader_model = DRY_RUN_READER_MODEL
                    effective_judge_model = DRY_RUN_JUDGE_MODEL

                cost = (input_tokens / 1_000_000 * input_price_per_mtok) + (
                    output_tokens / 1_000_000 * output_price_per_mtok
                )
                row = {
                    "case_id": case_id,
                    "suite": "longmemeval_s",
                    "split": "mock",
                    "category": category,
                    "condition": condition,
                    "reader_model": effective_reader_model,
                    "judge_model": effective_judge_model,
                    "dataset_sha256": dataset_sha256,
                    "prompt_version": prompt_version,
                    "run_artifact": str(output),
                    "gold_support_ids": _gold_support_ids(case),
                    "retrieved_support_ids": retrieved_support_ids,
                    "context_tokens": _token_estimate(context),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "answer": answer,
                    "judge_result": {
                        "score": 0,
                        "mode": mode,
                        "rationale": "Mock phase row; do not report as final QA evidence.",
                    },
                    "retrieval_trace": {
                        "ingestion_protocol": "session-by-session",
                        "answering_prompt_style": "supermemory-longmembench-appendix-v1",
                        "context_mode": context_mode,
                    },
                    "latency_seconds": time.perf_counter() - started,
                    "cost_usd": cost,
                    "official_table_eligible": False,
                }
                handle.write(json.dumps(row, sort_keys=True) + "\n")
                written += 1

    errors: list[str] = []
    rows, load_errors = validate_longmemeval_artifacts.load_jsonl(output)
    errors.extend(load_errors)
    for line_number, row in enumerate(rows, start=1):
        errors.extend(validate_longmemeval_artifacts.validate_row(row, line_number=line_number, allow_heldout=False))
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Wrote {written} mock rows to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LongMemEval mock phase and emit schema-valid JSONL rows.")
    parser.add_argument("dataset", type=Path, help="LongMemEval-S JSON dataset.")
    parser.add_argument("--split-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--condition", action="append", choices=sorted(validate_longmemeval_artifacts.CONDITIONS))
    parser.add_argument("--mode", choices=["dry-run", "gemini", "groq"], default="dry-run")
    parser.add_argument("--reader-model", default=DEFAULT_READER_MODEL)
    parser.add_argument("--judge-model", default="gpt-4o")
    parser.add_argument("--prompt-version", default="longmemeval-systems-v1")
    parser.add_argument("--input-price-per-mtok", type=float, default=0.0)
    parser.add_argument("--output-price-per-mtok", type=float, default=0.0)
    args = parser.parse_args(argv)

    if args.mode == "gemini" and not os.getenv("GEMINI_API_KEY"):
        parser.error("--mode gemini requires GEMINI_API_KEY")
    if args.mode == "groq" and not os.getenv("GROQ_API_KEY"):
        parser.error("--mode groq requires GROQ_API_KEY")
    if args.input_price_per_mtok < 0 or args.output_price_per_mtok < 0:
        parser.error("token prices must be non-negative")

    conditions = args.condition or DEFAULT_MOCK_CONDITIONS
    return run_mock_phase(
        dataset=args.dataset.resolve(),
        split_plan=args.split_plan.resolve(),
        output=args.output.resolve(),
        conditions=conditions,
        mode=args.mode,
        reader_model=args.reader_model,
        judge_model=args.judge_model,
        prompt_version=args.prompt_version,
        input_price_per_mtok=args.input_price_per_mtok,
        output_price_per_mtok=args.output_price_per_mtok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
