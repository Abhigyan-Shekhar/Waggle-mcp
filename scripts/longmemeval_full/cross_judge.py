"""Resumable independent judging for completed LongMemEval result artifacts."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from scripts.longmemeval_full.ingestion import case_id_for


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", type=Path, required=True)
    parser.add_argument("--results-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-retries", type=int, default=8)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def judge_prompt(case: dict[str, Any], answer: str) -> str:
    return (
        "You are grading a long-term-memory question-answering benchmark.\n"
        "Decide whether the candidate answer is semantically correct given the reference answer. "
        "Require correct numbers, dates, ordering, entities, qualifiers, and explicit uncertainty. "
        "Do not accept a plausible answer that contradicts or overstates the reference.\n\n"
        f"Question: {case.get('question', '')}\n"
        f"Reference answer: {case.get('answer', '')}\n"
        f"Candidate answer: {answer}\n\n"
        "Return exactly Yes or No."
    )


def call_groq(*, api_key: str, model: str, prompt: str, max_retries: int) -> tuple[str, dict[str, int]]:
    backoff = 3.0
    with httpx.Client(timeout=90.0) as client:
        for attempt in range(max_retries):
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_completion_tokens": 8,
                    "reasoning_effort": "none",
                },
            )
            if response.status_code == 200:
                payload = response.json()
                usage = payload.get("usage") or {}
                return str(payload["choices"][0]["message"]["content"]), {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                }
            if response.status_code != 429:
                raise RuntimeError(f"Groq request failed ({response.status_code}): {response.text}")
            if attempt + 1 == max_retries:
                raise RuntimeError(f"Groq rate limit persisted: {response.text}")
            retry_after = response.headers.get("retry-after")
            sleep_for = float(retry_after) if retry_after else backoff
            time.sleep(min(max(sleep_for, 1.0), 120.0))
            backoff = min(backoff * 2, 60.0)
    raise AssertionError("unreachable")


def main() -> int:
    args = parse_args()
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise SystemExit("GROQ_API_KEY is required")

    cases = json.loads(args.dataset_path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise SystemExit("Dataset file must contain a JSON list of LongMemEval cases")
    cases_by_id = {case_id_for(case, index): case for index, case in enumerate(cases)}
    results = read_jsonl(args.results_path)
    completed = {(row["case_id"], row["condition"]) for row in read_jsonl(args.output_path)}

    for index, row in enumerate(results, start=1):
        key = (str(row["case_id"]), str(row["condition"]))
        if key in completed:
            continue
        case = cases_by_id[key[0]]
        response, usage = call_groq(
            api_key=api_key,
            model=args.model,
            prompt=judge_prompt(case, str(row.get("answer") or "")),
            max_retries=args.max_retries,
        )
        label = response.strip().lower().rstrip(".")
        if label not in {"yes", "no"}:
            raise RuntimeError(f"Unexpected judge response for {key}: {response!r}")
        append_jsonl(
            args.output_path,
            {
                "case_id": key[0],
                "condition": key[1],
                "model": args.model,
                "correct": label == "yes",
                "response": response,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            },
        )
        print(f"completed {index}/{len(results)} {key[0]} {key[1]} correct={label == 'yes'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
