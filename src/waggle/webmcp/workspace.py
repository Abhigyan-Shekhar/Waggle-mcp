"""Deterministic workspace projections over the existing Waggle graph.

This module intentionally contains no storage or retrieval implementation. It
projects the current, scoped graph into application-level responses suitable
for the browser workspace and its WebMCP tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from waggle.errors import ValidationFailure

_MAX_PROJECT_ID_LENGTH = 512
_DEFAULT_SECTION_LIMIT = 6
_CONSTRAINT_TAGS = {"constraint", "guardrail", "requirement", "policy"}
_GOAL_TAGS = {"goal", "project-goal", "project_goal"}


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_active(node: dict[str, Any], *, now: datetime) -> bool:
    valid_to = _parse_datetime(node.get("valid_to"))
    return valid_to is None or valid_to > now


def _updated_sort_key(node: dict[str, Any]) -> datetime:
    return (
        _parse_datetime(node.get("updated_at"))
        or _parse_datetime(node.get("created_at"))
        or datetime.min.replace(tzinfo=UTC)
    )


def _project_name(project_id: str) -> str:
    leaf = PurePath(project_id).name or project_id
    words = leaf.replace("_", " ").replace("-", " ").strip().split()
    display_words = [
        "WebMCP" if word.lower() == "webmcp" else "MCP" if word.lower() == "mcp" else word.title() for word in words
    ]
    return " ".join(display_words) or project_id


def _memory_payload(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    evidence = node.get("evidence_records") if isinstance(node.get("evidence_records"), list) else []
    return {
        "memory_id": str(node.get("id", "")),
        "type": str(node.get("node_type", "note")),
        "content": str(node.get("content", "")),
        "authority": str(metadata.get("authority") or "authoritative"),
        "source": str(metadata.get("source_type") or node.get("agent_id") or "waggle"),
        "created_at": node.get("created_at"),
        "updated_at": node.get("updated_at"),
        "evidence_ids": [str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")],
    }


def _has_any_tag(node: dict[str, Any], expected: set[str]) -> bool:
    tags = {str(tag).strip().lower() for tag in node.get("tags", [])}
    return bool(tags & expected)


def _is_constraint(node: dict[str, Any]) -> bool:
    if _has_any_tag(node, _CONSTRAINT_TAGS):
        return True
    label = str(node.get("label", "")).strip().lower()
    content = str(node.get("content", "")).strip().lower()
    return label.startswith(("constraint", "requirement", "guardrail")) or content.startswith(
        ("must ", "must not ", "do not ", "never ")
    )


def _is_goal(node: dict[str, Any]) -> bool:
    if _has_any_tag(node, _GOAL_TAGS):
        return True
    return str(node.get("label", "")).strip().lower() in {"goal", "project goal", "product goal"}


def compile_project_brief(
    graph: Any,
    *,
    project_id: str,
    agent_id: str = "",
    session_id: str = "",
    section_limit: int = _DEFAULT_SECTION_LIMIT,
) -> dict[str, Any]:
    """Compile a compact authoritative project brief from scoped Waggle nodes."""

    project = str(project_id or "").strip()
    if not project:
        raise ValidationFailure("project_id is required.")
    if len(project) > _MAX_PROJECT_ID_LENGTH:
        raise ValidationFailure(f"project_id must be at most {_MAX_PROJECT_ID_LENGTH} characters.")
    if section_limit < 1 or section_limit > 25:
        raise ValidationFailure("section_limit must be between 1 and 25.")

    snapshot = graph.get_graph_snapshot(project=project, agent_id=agent_id, session_id=session_id)
    now = datetime.now(UTC)
    nodes = [node for node in snapshot.get("nodes", []) if _is_active(node, now=now)]
    nodes.sort(key=_updated_sort_key, reverse=True)

    goals = [node for node in nodes if _is_goal(node)]
    constraints = [node for node in nodes if _is_constraint(node) and node not in goals]
    decisions = [
        node
        for node in nodes
        if str(node.get("node_type", "")) == "decision" and node not in goals and node not in constraints
    ]
    open_questions = [node for node in nodes if str(node.get("node_type", "")) == "question"]
    reserved_ids = {str(node.get("id", "")) for node in [*goals, *constraints, *decisions, *open_questions]}
    current_state = [node for node in nodes if str(node.get("id", "")) not in reserved_ids]

    selected = [
        *goals[:1],
        *decisions[:section_limit],
        *constraints[:section_limit],
        *open_questions[:section_limit],
        *current_state[:section_limit],
    ]
    supporting_memory_ids = list(dict.fromkeys(str(node.get("id", "")) for node in selected if node.get("id")))

    return {
        "project": {"id": project, "name": _project_name(project)},
        "goal": str(goals[0].get("content", "")) if goals else "",
        "current_state": [_memory_payload(node) for node in current_state[:section_limit]],
        "decisions": [_memory_payload(node) for node in decisions[:section_limit]],
        "constraints": [_memory_payload(node) for node in constraints[:section_limit]],
        "open_questions": [_memory_payload(node) for node in open_questions[:section_limit]],
        "recent_changes": [_memory_payload(node) for node in nodes[:section_limit]],
        "supporting_memory_ids": supporting_memory_ids,
        "generated_at": now.isoformat(),
    }
