"""Deterministic workspace projections over the existing Waggle graph.

This module intentionally contains no storage or retrieval implementation. It
projects the current, scoped graph into application-level responses suitable
for the browser workspace and its WebMCP tools.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from waggle.errors import ValidationFailure

from .proposals import ProposalRepository

_MAX_PROJECT_ID_LENGTH = 512
_MAX_RECALL_QUERY_LENGTH = 4_000
_MAX_RECALL_LIMIT = 10
_MAX_PROPOSED_CONTENT_LENGTH = 20_000
_MAX_REASON_LENGTH = 4_000
_MAX_EVIDENCE_IDS = 20
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


def _validate_project_id(project_id: str) -> str:
    project = str(project_id or "").strip()
    if not project:
        raise ValidationFailure("project_id is required.")
    if len(project) > _MAX_PROJECT_ID_LENGTH:
        raise ValidationFailure(f"project_id must be at most {_MAX_PROJECT_ID_LENGTH} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in project):
        raise ValidationFailure("project_id contains invalid control characters.")
    return project


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

    project = _validate_project_id(project_id)
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


def _node_is_current_authority(node: Any, *, now: datetime) -> bool:
    valid_from = _parse_datetime(getattr(node, "valid_from", None))
    valid_to = _parse_datetime(getattr(node, "valid_to", None))
    if (valid_from is not None and valid_from > now) or (valid_to is not None and valid_to <= now):
        return False
    metadata = getattr(node, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    diagnostics = metadata.get("state_induction_v2")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    knowledge_status = str(metadata.get("knowledge_status") or diagnostics.get("knowledge_status") or "").upper()
    return not (
        metadata.get("superseded_by")
        or metadata.get("logically_superseded")
        or metadata.get("head_rejected_reason")
        or knowledge_status in {"HISTORICAL", "SUPERSEDED", "REJECTED"}
    )


def _authoritative_memory_payload(node: Any, *, supersedes: str | None) -> dict[str, Any]:
    metadata = getattr(node, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "memory_id": str(node.id),
        "type": str(node.node_type.value if hasattr(node.node_type, "value") else node.node_type),
        "content": str(node.content),
        "status": "authoritative",
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
        "source": str(metadata.get("source_type") or node.agent_id or "waggle"),
        "supersedes": supersedes,
    }


def recall_authoritative_memory(
    graph: Any,
    *,
    project_id: str,
    query: str,
    limit: int = 5,
    agent_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Recall current authoritative memories through Waggle's existing retrieval."""

    project = _validate_project_id(project_id)
    query_text = str(query or "").strip()
    if not query_text:
        raise ValidationFailure("query is required.")
    if len(query_text) > _MAX_RECALL_QUERY_LENGTH:
        raise ValidationFailure(f"query must be at most {_MAX_RECALL_QUERY_LENGTH} characters.")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationFailure("limit must be an integer.")
    if limit < 1 or limit > _MAX_RECALL_LIMIT:
        raise ValidationFailure(f"limit must be between 1 and {_MAX_RECALL_LIMIT}.")

    result = graph.query(
        query=query_text,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        max_nodes=min(50, max(20, limit * 4)),
        max_depth=1,
        expand_depth=1,
        retrieval_mode="graph",
        include_invalidated=False,
    )
    now = datetime.now(UTC)
    current_nodes = [node for node in result.nodes if _node_is_current_authority(node, now=now)]

    update_targets: dict[str, list[str]] = {}
    superseded_ids: set[str] = set()
    for edge in result.edges:
        relationship = str(edge.relationship.value if hasattr(edge.relationship, "value") else edge.relationship)
        if relationship != "updates":
            continue
        update_targets.setdefault(str(edge.source_id), []).append(str(edge.target_id))
        superseded_ids.add(str(edge.target_id))

    authoritative = [node for node in current_nodes if str(node.id) not in superseded_ids]
    memories = [
        _authoritative_memory_payload(
            node,
            supersedes=(update_targets.get(str(node.id)) or [None])[0],
        )
        for node in authoritative[:limit]
    ]
    return {
        "query": query_text,
        "project_id": project,
        "memories": memories,
    }


def _node_belongs_to_project(node: Any, project_id: str) -> bool:
    normalized = project_id.strip().lower()
    tags = {str(tag).strip().lower() for tag in getattr(node, "tags", [])}
    return (
        str(getattr(node, "project", "")).strip().lower() == normalized
        or normalized in tags
        or f"project:{normalized}" in tags
    )


def _load_current_authoritative_node(graph: Any, *, project_id: str, memory_id: str) -> Any:
    try:
        node = graph.get_node(memory_id)
    except ValueError as exc:
        raise ValidationFailure("memory_id does not identify an existing memory.") from exc
    if not _node_belongs_to_project(node, project_id):
        raise ValidationFailure("memory_id does not identify a memory in this project.")
    now = datetime.now(UTC)
    if not _node_is_current_authority(node, now=now):
        raise ValidationFailure("memory_id does not identify a current authoritative memory.")
    related = graph.get_related(node_id=memory_id, max_depth=1)
    if any(
        str(edge.relationship.value if hasattr(edge.relationship, "value") else edge.relationship) == "updates"
        and str(edge.target_id) == memory_id
        for edge in related.edges
    ):
        raise ValidationFailure("memory_id does not identify a current authoritative memory.")
    return node


def _target_version(node: Any) -> str:
    payload = {
        "memory_id": str(node.id),
        "project": str(node.project),
        "label": str(node.label),
        "content": str(node.content),
        "node_type": str(node.node_type.value if hasattr(node.node_type, "value") else node.node_type),
        "tags": list(node.tags),
        "metadata": dict(node.metadata),
        "valid_from": node.valid_from.isoformat() if node.valid_from else None,
        "valid_to": node.valid_to.isoformat() if node.valid_to else None,
        "updated_at": node.updated_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def propose_memory_change(
    graph: Any,
    repository: ProposalRepository,
    *,
    project_id: str,
    memory_id: str,
    proposed_content: str,
    reason: str = "",
    evidence_ids: list[str] | None = None,
    proposed_by_type: str = "agent",
    proposed_by_id: str = "webmcp",
) -> tuple[dict[str, Any], bool]:
    """Persist a pending proposal without changing authoritative graph state."""

    project = _validate_project_id(project_id)
    target_id = str(memory_id or "").strip()
    if not target_id or len(target_id) > 512:
        raise ValidationFailure("memory_id must be a non-empty string of at most 512 characters.")
    content = str(proposed_content or "").strip()
    if not content:
        raise ValidationFailure("proposed_content is required.")
    if len(content) > _MAX_PROPOSED_CONTENT_LENGTH:
        raise ValidationFailure(f"proposed_content must be at most {_MAX_PROPOSED_CONTENT_LENGTH} characters.")
    reason_text = str(reason or "").strip()
    if len(reason_text) > _MAX_REASON_LENGTH:
        raise ValidationFailure(f"reason must be at most {_MAX_REASON_LENGTH} characters.")
    if evidence_ids is None:
        normalized_evidence: list[str] = []
    elif not isinstance(evidence_ids, list) or any(not isinstance(item, str) for item in evidence_ids):
        raise ValidationFailure("evidence_ids must be an array of memory ID strings.")
    else:
        normalized_evidence = list(dict.fromkeys(item.strip() for item in evidence_ids if item.strip()))
    if len(normalized_evidence) > _MAX_EVIDENCE_IDS:
        raise ValidationFailure(f"evidence_ids may contain at most {_MAX_EVIDENCE_IDS} items.")

    target = _load_current_authoritative_node(graph, project_id=project, memory_id=target_id)
    now = datetime.now(UTC)
    for evidence_id in normalized_evidence:
        try:
            evidence = graph.get_node(evidence_id)
        except ValueError as exc:
            raise ValidationFailure(f"Evidence memory does not exist: {evidence_id}") from exc
        if not _node_belongs_to_project(evidence, project):
            raise ValidationFailure(f"Evidence memory belongs to a different project: {evidence_id}")
        valid_from = _parse_datetime(getattr(evidence, "valid_from", None))
        if valid_from is not None and valid_from > now:
            raise ValidationFailure(f"Evidence memory is not yet historically valid: {evidence_id}")

    version = _target_version(target)
    actor_type = str(proposed_by_type or "agent").strip() or "agent"
    actor_id = str(proposed_by_id or "").strip()
    dedupe_payload = json.dumps(
        {
            "actor_type": actor_type,
            "actor_id": actor_id,
            "target_memory_id": target_id,
            "target_memory_version": version,
            "proposed_content": content,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    dedupe_key = hashlib.sha256(dedupe_payload.encode("utf-8")).hexdigest()
    return repository.create_or_get_pending(
        tenant_id=str(graph.tenant_id),
        project_id=project,
        target_memory_id=target_id,
        target_memory_version=version,
        current_content=str(target.content),
        proposed_content=content,
        reason=reason_text,
        evidence_ids=normalized_evidence,
        proposed_by_type=actor_type,
        proposed_by_id=actor_id,
        dedupe_key=dedupe_key,
    )
