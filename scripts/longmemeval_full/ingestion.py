from __future__ import annotations

# ruff: noqa: E402, I001

import contextlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import run_longmemeval_waggle_phase as legacy


DEFAULT_AGENT_ID = "longmemeval-full"


class _NoopCleanup:
    def cleanup(self) -> None:
        return None


def build_case_graph(
    case: dict[str, Any],
    *,
    embedding_model: Any,
    agent_id: str = DEFAULT_AGENT_ID,
    cache_dir: Path | None = None,
    force_rebuild_cache: bool = False,
    progress: bool = False,
    defer_window_edges: bool = False,
) -> dict[str, Any]:
    """Build a per-case Waggle graph using the same real ingestion path as the old harness."""
    _, MemoryGraph = legacy._load_waggle_classes()
    case_id = case_id_for(case)
    session_payloads = legacy._case_sessions(case)
    if cache_dir is None:
        tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(tmpdir.name) / "longmemeval-full-waggle.db"
        marker_path = None
    else:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmpdir = _NoopCleanup()
        db_path = cache_dir / f"{case_id}.db"
        marker_path = cache_dir / f"{case_id}.complete.json"
        db_existed_before_graph_init = db_path.exists()
        if force_rebuild_cache:
            db_path.unlink(missing_ok=True)
            marker_path.unlink(missing_ok=True)
            db_existed_before_graph_init = False
    graph = MemoryGraph(
        db_path,
        embedding_model,
        tenant_id=f"longmemeval-full-{case_id}",
    )
    project = f"longmemeval-full/{case_id}"
    session_dates: dict[str, str] = {}
    session_messages: dict[str, list[Any]] = {}
    observe_results: list[dict[str, Any]] = []
    expected_message_count = sum(len(payload["messages"]) for payload in session_payloads)
    cache_complete = False
    if marker_path is not None and marker_path.exists() and db_path.exists() and db_existed_before_graph_init:
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            cache_complete = (
                marker.get("case_id") == case_id
                and marker.get("session_count") == len(session_payloads)
                and marker.get("message_count") == expected_message_count
            )
        except Exception:
            cache_complete = False
    for payload in session_payloads:
        session_id = payload["session_id"]
        document_date = payload["document_date"]
        session_dates[session_id] = document_date
        session_messages[session_id] = list(payload["messages"])
    if cache_complete:
        return {
            "graph": graph,
            "tmpdir": tmpdir,
            "project": project,
            "agent_id": agent_id,
            "session_dates": session_dates,
            "session_messages": session_messages,
            "all_session_ids": [payload["session_id"] for payload in session_payloads],
            "observe_results": observe_results,
            "graph_cache_path": str(db_path),
            "graph_cache_hit": True,
        }

    for payload_index, payload in enumerate(session_payloads, start=1):
        session_id = payload["session_id"]
        document_date = payload["document_date"]
        if progress and (payload_index == 1 or payload_index % 25 == 0 or payload_index == len(session_payloads)):
            print(
                f"ingesting {case_id}: session {payload_index}/{len(session_payloads)}",
                file=sys.stderr,
                flush=True,
            )
        observe_results.extend(
            _ingest_session_messages(
                graph,
                messages=payload["messages"],
                document_date=document_date,
                project=project,
                agent_id=agent_id,
                session_id=session_id,
                derive_window_edges=not defer_window_edges,
            )
        )
    if defer_window_edges:
        _derive_case_window_edges(graph, project=project, progress=progress, case_id=case_id)
    if marker_path is not None:
        marker_path.write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "session_count": len(session_payloads),
                    "message_count": expected_message_count,
                    "observe_call_count": len(observe_results),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return {
        "graph": graph,
        "tmpdir": tmpdir,
        "project": project,
        "agent_id": agent_id,
        "session_dates": session_dates,
        "session_messages": session_messages,
        "all_session_ids": [payload["session_id"] for payload in session_payloads],
        "observe_results": observe_results,
        "graph_cache_path": str(db_path) if cache_dir is not None else "",
        "graph_cache_hit": False,
    }


def _ingest_session_messages(
    graph: Any,
    *,
    messages: list[Any],
    document_date: str,
    project: str,
    agent_id: str,
    session_id: str,
    derive_window_edges: bool,
) -> list[dict[str, Any]]:
    """Ingest LongMemEval messages without dropping orphan transcript evidence.

    Normal user->assistant pairs still use Waggle's production observe path so
    typed memory extraction remains realistic. Leading assistant turns, trailing
    user turns, or other unpaired messages are stored as transcript-only records:
    they are valid benchmark evidence, but they should not trigger extraction as
    if they were a normal live conversation pair.
    """
    observe_results: list[dict[str, Any]] = []
    observed_at = _parse_document_date(document_date)
    prefixed_date = False
    index = 0
    while index < len(messages):
        item = messages[index]
        role = legacy._message_role(item) or "unknown"
        text = legacy._message_text(item)
        if not text:
            index += 1
            continue
        text, prefixed_date = _maybe_prefix_document_date(
            text, document_date=document_date, already_prefixed=prefixed_date
        )

        next_index = _next_nonempty_message_index(messages, index + 1)
        if role == "user" and next_index is not None and legacy._message_role(messages[next_index]) == "assistant":
            assistant_text = legacy._message_text(messages[next_index])
            result = graph.observe_conversation(
                user_message=text,
                assistant_response=assistant_text,
                project=project,
                agent_id=agent_id,
                session_id=session_id,
                derive_window_edges=derive_window_edges,
                observed_at=observed_at,
            )
            observe_results.append(_observe_result_summary(result))
            index = next_index + 1
            continue

        _store_transcript_only_message(
            graph,
            role=role,
            text=text,
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            observed_at=observed_at,
        )
        observe_results.append(
            {
                "turn_id": "",
                "verbatim_stored": True,
                "nodes_extracted": 0,
                "edges_inferred": 0,
                "extraction_errors": [],
                "transcript_only": True,
                "role": role,
            }
        )
        index += 1
    return observe_results


def _observe_result_summary(result: Any) -> dict[str, Any]:
    return {
        "turn_id": getattr(result, "turn_id", ""),
        "verbatim_stored": getattr(result, "verbatim_stored", False),
        "nodes_extracted": getattr(result, "nodes_extracted", 0),
        "edges_inferred": getattr(result, "edges_inferred", 0),
        "extraction_errors": list(getattr(result, "extraction_errors", []) or []),
    }


def _maybe_prefix_document_date(text: str, *, document_date: str, already_prefixed: bool) -> tuple[str, bool]:
    if document_date and not already_prefixed:
        return f"[documentDate: {document_date}]\n{text}", True
    return text, already_prefixed


def _next_nonempty_message_index(messages: list[Any], start_index: int) -> int | None:
    for index in range(start_index, len(messages)):
        if legacy._message_text(messages[index]):
            return index
    return None


def _store_transcript_only_message(
    graph: Any,
    *,
    role: str,
    text: str,
    project: str,
    agent_id: str,
    session_id: str,
    observed_at: datetime | None = None,
) -> None:
    with graph._lock, contextlib.closing(graph._connect()) as connection:
        turn_index = graph._next_transcript_turn_index(
            connection,
            session_id=session_id,
            project=project,
            agent_id=agent_id,
        )
        try:
            graph._store_transcript_record(
                connection,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                observed_at=observed_at or datetime.now(UTC),
                turn_index=turn_index,
                role=role,
                transcript_text=text,
                metadata={"longmemeval_transcript_only": True, "reason": "unpaired_message"},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _parse_document_date(value: str) -> datetime | None:
    match = re.search(
        r"(?P<year>\d{4})/(?P<month>\d{1,2})/(?P<day>\d{1,2})"
        r"(?:\s+\([^)]+\))?(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
        value or "",
    )
    if not match:
        return None
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        int(match.group("hour") or 0),
        int(match.group("minute") or 0),
        tzinfo=UTC,
    )


def _derive_case_window_edges(graph: Any, *, project: str, progress: bool, case_id: str) -> None:
    """Derive context-window edges once per window after bulk case ingestion."""
    try:
        windows = graph.list_context_windows(project=project, limit=10_000)
    except Exception as exc:
        if progress:
            print(
                f"window edge derivation skipped for {case_id}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        return
    entity_cache: dict[str, dict[str, dict[str, str]]] = {}
    for index, window in enumerate(windows, start=1):
        if progress and (index == 1 or index % 25 == 0 or index == len(windows)):
            print(f"extracting window entities {case_id}: window {index}/{len(windows)}", file=sys.stderr, flush=True)
        try:
            entities = graph.extract_window_entities(window.id)
        except Exception as exc:
            if progress:
                print(
                    f"window entity extraction failed for {case_id} {window.id}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            entities = []
        entity_cache[window.id] = {
            str(entity.get("label", "")).strip().lower(): entity
            for entity in entities
            if str(entity.get("label", "")).strip()
        }

    for index, window in enumerate(windows, start=1):
        if progress and (index == 1 or index % 25 == 0 or index == len(windows)):
            print(f"deriving cached window edges {case_id}: window {index}/{len(windows)}", file=sys.stderr, flush=True)
        current_by_label = entity_cache.get(window.id, {})
        created_temporal = False
        other_windows = [candidate for candidate in windows if candidate.id != window.id][:200]
        for other_window in other_windows:
            if not created_temporal:
                try:
                    graph.create_context_window_edge(
                        source_window_id=other_window.id,
                        target_window_id=window.id,
                        edge_type="temporal_sequence",
                        shared_entities=[],
                        weight=1.0,
                    )
                except Exception as exc:
                    if progress:
                        print(
                            f"temporal window edge failed for {case_id} {window.id}: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                created_temporal = True
            other_by_label = entity_cache.get(other_window.id, {})
            overlap = set(current_by_label) & set(other_by_label)
            if not overlap:
                continue
            has_conflict = any(
                _normalized_edge_text(current_by_label[label].get("content", ""))
                != _normalized_edge_text(other_by_label[label].get("content", ""))
                for label in overlap
            )
            edge_type = "supersedes" if has_conflict else "entity_overlap"
            denominator = max(len(current_by_label), len(other_by_label), 1)
            try:
                graph.create_context_window_edge(
                    source_window_id=other_window.id,
                    target_window_id=window.id,
                    edge_type=edge_type,
                    shared_entities=sorted(overlap),
                    weight=len(overlap) / denominator,
                )
            except Exception as exc:
                if progress:
                    print(
                        f"cached window edge failed for {case_id} {window.id}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )


def _normalized_edge_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def case_id_for(case: dict[str, Any], default_index: int = 0) -> str:
    return str(case.get("question_id") or case.get("case_id") or case.get("id") or f"case-{default_index:04d}")
