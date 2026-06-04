from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

import rlm.core.rlm as upstream_rlm_module
from rlm import RLM
from rlm.clients.base_lm import BaseLM
from rlm.core.types import ModelUsageSummary, RLMChatCompletion, UsageSummary
from rlm.logger import RLMLogger

import logging
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from waggle.graph import MemoryGraph


DEFAULT_WAGGLE_RLM_SYSTEM_PROMPT = (
    "Use Waggle retrieval first. Treat raw `context` as fallback only. "
    "When you need tool execution, reply with exactly one ```repl``` block and no extra prose. Never use ```python. "
    "If the question asks for pairs of user IDs matching labels or properties, your very first action must be exactly: "
    '`final_answer = answer_pair_users_by_label(query, labels=[...], top_k=8)` then `FINAL_VAR("final_answer")` in the same `repl` block. '
    "For pair-shaped questions, do not call `answer_with_waggle`, `extract_waggle_records`, `classify_waggle_records`, `index_waggle_fields`, `search_waggle`, or `read_node` before trying `answer_pair_users_by_label`. "
    "As soon as qualifying users are identified for a pair-shaped question, finalize immediately and stop. Do not do extra exploration after you have enough evidence. "
    'For non-pair questions, first action: `answer_with_waggle(question="...", top_k=3)`. '
    "If you need structure, use `extract_waggle_records(query, top_k=3)`, `classify_waggle_records(query, top_k=3)`, or `index_waggle_fields(query, field_name, top_k=3)`. "
    "Only use `search_waggle` or `read_node` for targeted follow-up. "
    "Do not import modules, do not invent fields or objects, and do not parse raw node dicts by guessing keys. "
    "Structured helpers already expose `user`, `date`, `text`, and semantic `label`. "
    "When you are done, return the final answer using FINAL_VAR(...) or FINAL(...)."
)


@dataclass
class RLMRunResult:
    answer: str
    metadata: dict[str, Any] | None
    visited_node_ids: list[str]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "rlm_response": self.answer,
            "rlm_metadata": self.metadata,
            "rlm_visited_node_ids": self.visited_node_ids,
        }


_RECORD_BLOCK_PATTERN = re.compile(
    r"Example\s+(?P<example_id>\d+):\s*"
    r"Text:\s*(?P<text>.*?)\s*"
    r"User:\s*(?P<user>[^\n]+)\s*"
    r"Date:\s*(?P<date>[^\n]+)",
    re.DOTALL,
)


def _parse_chunk_records(content: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for match in _RECORD_BLOCK_PATTERN.finditer(content):
        records.append(
            {
                "example_id": match.group("example_id").strip(),
                "text": match.group("text").strip(),
                "user": match.group("user").strip(),
                "date": match.group("date").strip(),
            }
        )
    return records


def _infer_semantic_label(text: str) -> str:
    normalized = text.strip()
    lowered = normalized.lower()
    if re.search(r"\b\d+(?:[\.,]\d+)?\b", lowered):
        return "numeric value"
    if any(
        token in lowered
        for token in (
            "city of ",
            "capital of ",
            " in new york",
            "in paris",
            "in london",
            "in tokyo",
            "located in ",
            "from paris",
            "from london",
            "from tokyo",
            "highest mountain",
            "downtown",
        )
    ):
        return "location"
    if re.fullmatch(r"[A-Z]{2,10}", normalized):
        return "abbreviation"
    if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+)+", normalized):
        return "human being"
    if any(keyword in lowered for keyword in ("person", "man", "woman", "boy", "girl")):
        return "human being"
    if any(keyword in lowered for keyword in ("concept", "abstract", "idea", "theory")):
        return "description and abstract concept"
    return "entity"


class _MockLM(BaseLM):
    def __init__(self, response_fn: Callable[[str | dict[str, Any]], str]) -> None:
        super().__init__(model_name="mock-rlm")
        self._response_fn = response_fn
        self._calls = 0

    def completion(self, prompt: str | dict[str, Any]) -> str:
        self._calls += 1
        return self._response_fn(prompt)

    async def acompletion(self, prompt: str | dict[str, Any]) -> str:
        return self.completion(prompt)

    def get_usage_summary(self) -> UsageSummary:
        return UsageSummary(
            model_usage_summaries={
                self.model_name: ModelUsageSummary(
                    total_calls=self._calls,
                    total_input_tokens=0,
                    total_output_tokens=0,
                )
            }
        )

    def get_last_usage(self) -> ModelUsageSummary:
        return ModelUsageSummary(
            total_calls=1,
            total_input_tokens=0,
            total_output_tokens=0,
        )


@contextmanager
def _patched_get_client(response_fn: Callable[[str | dict[str, Any]], str] | None):
    if response_fn is None:
        yield
        return

    original = upstream_rlm_module.get_client
    from rlm.core.lm_handler import LMHandler

    original_start = LMHandler.start
    original_stop = LMHandler.stop

    def _fake_get_client(backend: str, backend_kwargs: dict[str, Any] | None = None) -> BaseLM:
        del backend, backend_kwargs
        return _MockLM(response_fn)

    def _fake_start(self) -> tuple[str, int]:
        return (self.host, self._port)

    def _fake_stop(self) -> None:
        return None

    upstream_rlm_module.get_client = _fake_get_client
    LMHandler.start = _fake_start
    LMHandler.stop = _fake_stop
    try:
        yield
    finally:
        upstream_rlm_module.get_client = original
        LMHandler.start = original_start
        LMHandler.stop = original_stop


def build_subprocess_response_fn(command_template: str) -> Callable[[str | dict[str, Any]], str]:
    def _response_fn(prompt: str | dict[str, Any]) -> str:
        rendered = prompt if isinstance(prompt, str) else str(prompt)
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_path = Path(tmpdir) / "prompt.txt"
            prompt_path.write_text(rendered, encoding="utf-8")
            command = command_template.format(prompt_file=str(prompt_path), prompt=rendered)
            completed = subprocess.run(
                shlex.split(command),
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"LLM command failed with exit code {completed.returncode}: {completed.stderr.strip()}"
                )
            return completed.stdout.strip()

    return _response_fn


def build_groq_openai_backend_kwargs(*, api_key: str, model_name: str) -> dict[str, Any]:
    return {
        "api_key": api_key,
        "base_url": "https://api.groq.com/openai/v1",
        "model_name": model_name,
    }


def build_gemini_backend_kwargs(*, api_key: str, model_name: str) -> dict[str, Any]:
    return {
        "api_key": api_key,
        "model_name": model_name,
    }


def build_ollama_backend_kwargs(*, base_url: str, model_name: str) -> dict[str, Any]:
    return {
        "base_url": base_url.rstrip("/"),
        "model_name": model_name,
    }


def run_groq_one_shot(
    *,
    prompt: str,
    api_key: str,
    model_name: str,
    max_tokens: int = 512,
    timeout_seconds: float = 60.0,
) -> str:
    payload = json.dumps(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq request failed with status {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Groq request failed: {exc.reason}") from exc
    return str(body["choices"][0]["message"]["content"] or "").strip()


def run_gemini_one_shot(
    *,
    prompt: str,
    api_key: str,
    model_name: str,
    timeout_seconds: float = 60.0,
) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
    )
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
    )
    return str(response.text or "").strip()


def run_ollama_one_shot(
    *,
    prompt: str,
    model_name: str,
    base_url: str = "http://127.0.0.1:11434",
    timeout_seconds: float = 60.0,
) -> str:
    response = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("message", {}).get("content", "") or "").strip()


def _normalize_final_response(text: str) -> str:
    normalized = str(text).strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        return normalized[1:-1].strip()
    return normalized


def run_waggle_rlm(
    graph: MemoryGraph,
    *,
    question: str,
    context: str,
    project: str,
    session_id: str,
    retrieval_mode: str,
    max_nodes: int,
    max_depth: int,
    system_prompt: str = DEFAULT_WAGGLE_RLM_SYSTEM_PROMPT,
    max_iterations: int = 30,
    backend: str = "openai",
    backend_kwargs: dict[str, Any] | None = None,
    mock_response_fn: Callable[[str | dict[str, Any]], str] | None = None,
) -> RLMRunResult:
    visited_node_ids: set[str] = set()

    def _format_matches(matches: list[dict[str, Any]]) -> str:
        if not matches:
            return ""
        sections: list[str] = []
        for index, match in enumerate(matches, start=1):
            sections.append(
                "\n".join(
                    [
                        f"[Match {index}] id={match['id']}",
                        f"label={match['label']}",
                        match["content"],
                    ]
                ).strip()
            )
        return "\n\n".join(section for section in sections if section).strip()

    def _structured_match_records(matches: list[dict[str, Any]]) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for match in matches:
            node_records = _parse_chunk_records(match["content"])
            for record in node_records:
                enriched = dict(record)
                enriched["label"] = _infer_semantic_label(record["text"])
                enriched["semantic_label"] = enriched["label"]
                enriched["node_id"] = match["id"]
                enriched["node_label"] = match["label"]
                records.append(enriched)
        return records

    def _session_matches() -> list[dict[str, Any]]:
        result = graph.aggregate(
            query="",
            max_nodes=10_000,
            max_depth=0,
            project=project,
            session_id=session_id,
        )
        payload: list[dict[str, Any]] = []
        for node in result.nodes:
            visited_node_ids.add(node.id)
            payload.append(
                {
                    "id": node.id,
                    "label": node.label,
                    "content": node.content,
                }
            )
        return payload

    def _expanded_pair_queries(query: str, labels: list[str]) -> list[str]:
        expanded = [query.strip()]
        normalized_labels = {label.strip().lower() for label in labels if str(label).strip()}
        if normalized_labels:
            expanded.append(" ".join(sorted(normalized_labels)))
            expanded.append(f"user ids with {' or '.join(sorted(normalized_labels))}")
        if "numeric value" in normalized_labels:
            expanded.extend(
                [
                    "users with numbers or money values",
                    "dollars amounts numeric values by user",
                ]
            )
        if "location" in normalized_labels:
            expanded.extend(
                [
                    "users with cities or locations",
                    "city of paris location by user",
                ]
            )
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in expanded:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _query_waggle(query: str, top_k: int) -> list[dict[str, Any]]:
        if retrieval_mode == "aggregate":
            result = graph.aggregate(
                query=query,
                max_nodes=max(1, min(int(top_k), max_nodes)),
                max_depth=1,
                project=project,
                session_id=session_id,
            )
        else:
            result = graph.query(
                query=query,
                max_nodes=max(1, min(int(top_k), max_nodes)),
                max_depth=1,
                retrieval_mode=retrieval_mode,
                project=project,
                session_id=session_id,
            )
        payload: list[dict[str, Any]] = []
        for node in result.nodes:
            visited_node_ids.add(node.id)
            payload.append(
                {
                    "id": node.id,
                    "label": node.label,
                    "content": node.content,
                }
            )
        return payload

    def search_waggle(query: str, top_k: int = 3) -> list[dict[str, Any]]:
        matches = _query_waggle(query, top_k)
        compact_lines = [f"{match['id']} | {match['label']}" for match in matches]
        if compact_lines:
            logger.debug("\n".join(compact_lines))
        return matches

    def gather_waggle_context(query: str, top_k: int = 3) -> str:
        matches = _query_waggle(query, top_k)
        evidence_bundle = _format_matches(matches)
        if evidence_bundle:
            logger.debug(evidence_bundle)
        else:
            logger.debug("")
        if not matches:
            return ""
        return evidence_bundle

    def extract_waggle_records(query: str, top_k: int = 3) -> list[dict[str, str]]:
        matches = _query_waggle(query, top_k)
        records = _structured_match_records(matches)
        logger.debug(json.dumps(records, indent=2))
        return records

    def classify_waggle_records(query: str, top_k: int = 3) -> list[dict[str, str]]:
        records = extract_waggle_records(query, top_k=top_k)
        logger.debug(json.dumps(records, indent=2))
        return records

    def index_waggle_fields(query: str, field_name: str, top_k: int = 3) -> dict[str, list[str]]:
        normalized_field = field_name.strip().lower()
        records = extract_waggle_records(query, top_k=top_k)
        index: dict[str, list[str]] = {}
        for record in records:
            field_value = record.get(normalized_field, "").strip()
            if not field_value:
                continue
            text_value = record.get("text", "").strip()
            bucket = index.setdefault(field_value, [])
            if text_value and text_value not in bucket:
                bucket.append(text_value)
        logger.debug(json.dumps(index, indent=2))
        return index

    def pair_users_by_label(query: str, labels: list[str], top_k: int = 8) -> list[tuple[str, str]]:
        normalized_labels = {label.strip().lower() for label in labels if str(label).strip()}
        del query, top_k
        # Pair tasks care about user recall, not record salience.
        # Scan the full session-scoped context so each user's qualifying records
        # can contribute before we build the pair clique deterministically.
        records = _structured_match_records(_session_matches())
        logger.debug(json.dumps(records, indent=2))
        users: set[str] = set()
        for record in records:
            label = record.get("label", "").strip().lower()
            if label in normalized_labels:
                user_value = record.get("user", "").strip()
                if user_value:
                    users.add(user_value)
        ordered_users = sorted(users)
        pairs: list[tuple[str, str]] = []
        for index, first_user in enumerate(ordered_users):
            for second_user in ordered_users[index + 1 :]:
                pairs.append((first_user, second_user))
        logger.debug(json.dumps(pairs, indent=2))
        return pairs

    def answer_pair_users_by_label(query: str, labels: list[str], top_k: int = 8) -> str:
        pairs = pair_users_by_label(query, labels=labels, top_k=top_k)
        formatted = "\n".join(f"({left}, {right})" for left, right in pairs)
        logger.debug(formatted)
        return formatted

    def answer_with_waggle(
        question_text: str = "",
        top_k: int = 3,
        *,
        question: str = "",
        query: str = "",
    ) -> str:
        resolved_question = question or query or question_text
        matches = _query_waggle(resolved_question, top_k)
        evidence_bundle = _format_matches(matches)
        packet_lines = [
            f"Question: {resolved_question}",
            "Use the evidence below to answer directly. If the evidence is insufficient, say so briefly.",
            "",
            "Evidence:",
            evidence_bundle or "(no evidence found)",
        ]
        packet = "\n".join(packet_lines).strip()
        logger.debug(packet)
        return packet

    def read_node(node_id: str) -> str:
        node = graph.get_node(str(node_id))
        visited_node_ids.add(node.id)
        logger.debug(node.content)
        return node.content

    logger = RLMLogger()
    with _patched_get_client(mock_response_fn):
        rlm = RLM(
            backend=backend,
            backend_kwargs=backend_kwargs or {"model_name": "mock-rlm"},
            environment="local",
            depth=0,
            max_depth=max_depth,
            max_iterations=max_iterations,
            custom_system_prompt=system_prompt,
            logger=logger,
            custom_tools={
                "answer_with_waggle": {
                    "tool": answer_with_waggle,
                    "description": "First-choice helper. Retrieves relevant Waggle evidence for a question and prints an answer-ready evidence packet.",
                },
                "extract_waggle_records": {
                    "tool": extract_waggle_records,
                    "description": "Retrieve Waggle evidence, parse chunk text into structured records with keys like `example_id`, `text`, `user`, `date`, and semantic `label`, print them as JSON, and return the list.",
                },
                "classify_waggle_records": {
                    "tool": classify_waggle_records,
                    "description": "Retrieve and return structured Waggle records with semantic `label` already inferred from each record text.",
                },
                "index_waggle_fields": {
                    "tool": index_waggle_fields,
                    "description": "Retrieve Waggle evidence, parse structured records, and build a field-to-text index such as `user -> [texts...]`.",
                },
                "pair_users_by_label": {
                    "tool": pair_users_by_label,
                    "description": "Retrieve classified Waggle records and return all sorted user-id pairs where both users have at least one record whose semantic `label` is in the provided label list.",
                },
                "answer_pair_users_by_label": {
                    "tool": answer_pair_users_by_label,
                    "description": "Retrieve, classify, and format all sorted user-id pairs where both users have at least one record whose semantic `label` is in the provided label list. Returns the final newline-delimited answer string.",
                },
                "gather_waggle_context": {
                    "tool": gather_waggle_context,
                    "description": "Retrieve the top Waggle nodes for a query, print the evidence bundle, and return it as a string.",
                },
                "search_waggle": {
                    "tool": search_waggle,
                    "description": "Search Waggle memory, print a compact node list, and return node dicts with `id`, `label`, and `content`.",
                },
                "read_node": {
                    "tool": read_node,
                    "description": "Read the full content of a specific Waggle node by id, print it, and return it.",
                },
            },
            custom_sub_tools={
                "answer_with_waggle": {
                    "tool": answer_with_waggle,
                    "description": "First-choice helper. Retrieves relevant Waggle evidence for a question and prints an answer-ready evidence packet.",
                },
                "extract_waggle_records": {
                    "tool": extract_waggle_records,
                    "description": "Retrieve Waggle evidence, parse chunk text into structured records with keys like `example_id`, `text`, `user`, `date`, and semantic `label`, print them as JSON, and return the list.",
                },
                "classify_waggle_records": {
                    "tool": classify_waggle_records,
                    "description": "Retrieve and return structured Waggle records with semantic `label` already inferred from each record text.",
                },
                "index_waggle_fields": {
                    "tool": index_waggle_fields,
                    "description": "Retrieve Waggle evidence, parse structured records, and build a field-to-text index such as `user -> [texts...]`.",
                },
                "pair_users_by_label": {
                    "tool": pair_users_by_label,
                    "description": "Retrieve classified Waggle records and return all sorted user-id pairs where both users have at least one record whose semantic `label` is in the provided label list.",
                },
                "answer_pair_users_by_label": {
                    "tool": answer_pair_users_by_label,
                    "description": "Retrieve, classify, and format all sorted user-id pairs where both users have at least one record whose semantic `label` is in the provided label list. Returns the final newline-delimited answer string.",
                },
                "gather_waggle_context": {
                    "tool": gather_waggle_context,
                    "description": "Retrieve the top Waggle nodes for a query, print the evidence bundle, and return it as a string.",
                },
                "search_waggle": {
                    "tool": search_waggle,
                    "description": "Search Waggle memory, print a compact node list, and return node dicts with `id`, `label`, and `content`.",
                },
                "read_node": {
                    "tool": read_node,
                    "description": "Read the full content of a specific Waggle node by id, print it, and return it.",
                },
            },
        )
        completion: RLMChatCompletion = rlm.completion(context, root_prompt=question)
    return RLMRunResult(
        answer=_normalize_final_response(completion.response),
        metadata=completion.metadata,
        visited_node_ids=sorted(visited_node_ids),
    )
