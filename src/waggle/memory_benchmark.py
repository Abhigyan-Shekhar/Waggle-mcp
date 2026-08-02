from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from waggle.graph import MemoryGraph
from waggle.models import NodeType


@dataclass
class MemoryBenchmarkCase:
    case_id: str
    question: str
    question_type: str
    answer: str
    sessions: list[dict[str, str]]
    metadata: dict[str, Any]


@dataclass
class IndexedCase:
    case: MemoryBenchmarkCase
    session_node_ids: dict[str, str]


@dataclass
class BenchmarkResult:
    case_id: str
    question_type: str
    arm: str
    answer: str
    correct: bool
    retrieved_count: int
    latency_seconds: float
    metadata: dict[str, Any]


def _session_text(turns: Any) -> str:
    if isinstance(turns, str):
        return turns
    if isinstance(turns, list):
        chunks = []
        for turn in turns:
            if isinstance(turn, dict):
                role = str(turn.get("role", "")).strip()
                content = str(turn.get("content", "")).strip()
                chunks.append(f"{role}: {content}" if role else content)
            else:
                chunks.append(str(turn))
        return "\n".join(chunk for chunk in chunks if chunk.strip())
    return str(turns)


def load_longmemeval(path: str | Path) -> list[MemoryBenchmarkCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("data", [])
    cases: list[MemoryBenchmarkCase] = []
    for index, row in enumerate(rows):
        session_ids = [str(item) for item in row.get("haystack_session_ids", [])]
        session_payloads = row.get("haystack_sessions", [])
        sessions = []
        for session_index, turns in enumerate(session_payloads):
            session_id = session_ids[session_index] if session_index < len(session_ids) else f"session_{session_index}"
            sessions.append({"session_id": session_id, "text": _session_text(turns)})
        gold = [str(item) for item in row.get("answer_session_ids") or row.get("gold_support_ids") or []]
        cases.append(
            MemoryBenchmarkCase(
                case_id=str(row.get("question_id") or row.get("case_id") or index),
                question=str(row.get("question", "")),
                question_type=str(row.get("question_type") or row.get("category") or "unknown"),
                answer=str(row.get("answer", "")),
                sessions=sessions,
                metadata={**row, "gold_support_ids": gold},
            )
        )
    return cases


def load_locomo(path: str | Path) -> list[MemoryBenchmarkCase]:
    return load_longmemeval(path)


def index_case_into_waggle(case: MemoryBenchmarkCase, graph: MemoryGraph) -> IndexedCase:
    session_node_ids: dict[str, str] = {}
    for session in case.sessions:
        session_id = session["session_id"]
        if session_id in session_node_ids:
            continue
        result = graph.add_node(
            label=f"Session {session_id}",
            content=session["text"],
            node_type=NodeType.NOTE,
            project="longmemeval",
            session_id=session_id,
            metadata={"benchmark_case_id": case.case_id, "session_id": session_id},
        )
        session_node_ids[session_id] = result.node.id
    return IndexedCase(case=case, session_node_ids=session_node_ids)


def retrieve_waggle_graph(indexed: IndexedCase, graph: MemoryGraph, *, limit: int = 20, hops: int = 1) -> Any:
    return graph.query(
        query=indexed.case.question,
        max_nodes=limit,
        max_depth=hops,
        project="longmemeval",
        retrieval_mode="graph",
        include_invalidated=True,
    )


def _format_context(nodes: list[Any], limit: int) -> str:
    chunks = []
    for node in nodes[:limit]:
        session_id = getattr(node, "session_id", "") or getattr(node, "id", "")
        chunks.append(f"[{session_id}]\n{node.content}")
    return "\n\n".join(chunks)


def run_case_cached(
    indexed: IndexedCase,
    graph: MemoryGraph,
    *,
    benchmark: str,
    arm: str,
    answer_model_call: Callable[[str], str],
    judge_model_call: Callable[[str], str],
    answer_model_name: str,
    judge_model_name: str,
    retrieval_limit: int,
    cache: Any = None,
    cache_extra: dict[str, Any] | None = None,
) -> tuple[BenchmarkResult, bool]:
    started = time.perf_counter()
    if arm == "no_context":
        nodes = []
        context = ""
    elif arm == "full_context":
        nodes = []
        context = "\n\n".join(f"[{s['session_id']}]\n{s['text']}" for s in indexed.case.sessions)
    else:
        retrieved = retrieve_waggle_graph(indexed, graph, limit=retrieval_limit)
        nodes = list(retrieved.nodes)
        context = _format_context(nodes, retrieval_limit)

    prompt = f"Question: {indexed.case.question}\n\nContext:\n{context}\n\nAnswer concisely."
    answer = answer_model_call(prompt)
    judge_prompt = (
        f"Gold answer: {indexed.case.answer}\nCandidate answer: {answer}\n"
        "Return only Yes or No for whether the candidate answers the question."
    )
    judge = judge_model_call(judge_prompt).strip().lower()
    correct = judge.startswith("yes")
    return (
        BenchmarkResult(
            case_id=indexed.case.case_id,
            question_type=indexed.case.question_type,
            arm=arm,
            answer=answer,
            correct=correct,
            retrieved_count=len(nodes),
            latency_seconds=time.perf_counter() - started,
            metadata={"benchmark": benchmark, "answer_model": answer_model_name, "judge_model": judge_model_name},
        ),
        False,
    )


def summarize(results: list[BenchmarkResult]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for result in results:
        bucket = summary.setdefault(result.arm, {"total": 0, "correct": 0, "by_question_type": {}})
        bucket["total"] += 1
        bucket["correct"] += int(result.correct)
        by_type = bucket["by_question_type"].setdefault(result.question_type, {"total": 0, "correct": 0})
        by_type["total"] += 1
        by_type["correct"] += int(result.correct)
    for bucket in summary.values():
        bucket["accuracy"] = bucket["correct"] / bucket["total"] if bucket["total"] else 0.0
        for entry in bucket["by_question_type"].values():
            entry["accuracy"] = entry["correct"] / entry["total"] if entry["total"] else 0.0
    return summary


def write_report(path: str | Path, results: list[BenchmarkResult], summary: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"summary": summary, "results": [asdict(result) for result in results]}, indent=2),
        encoding="utf-8",
    )
