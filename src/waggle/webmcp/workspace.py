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

from waggle.errors import ValidationFailure, WaggleError
from waggle.models import RelationType

from .projects import repository_context_for_project, resolve_active_project
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


def _node_value(node: Any, field: str, default: Any = None) -> Any:
    if isinstance(node, dict):
        return node.get(field, default)
    return getattr(node, field, default)


def authority_status(
    node: Any,
    *,
    now: datetime,
    superseded_by_update: bool = False,
) -> str:
    """Return the canonical authority status shared by recall and the UI."""

    metadata = _node_value(node, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    diagnostics = metadata.get("state_induction_v2")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    knowledge_status = str(metadata.get("knowledge_status") or diagnostics.get("knowledge_status") or "").upper()
    if metadata.get("head_rejected_reason") or knowledge_status == "REJECTED":
        return "rejected"
    if knowledge_status == "HISTORICAL":
        return "historical"
    if (
        superseded_by_update
        or metadata.get("superseded_by")
        or metadata.get("logically_superseded")
        or knowledge_status == "SUPERSEDED"
    ):
        return "superseded"

    valid_from = _parse_datetime(_node_value(node, "valid_from"))
    valid_to = _parse_datetime(_node_value(node, "valid_to"))
    if valid_from is not None and valid_from > now:
        return "future"
    if valid_to is not None and valid_to <= now:
        return "expired"
    if metadata.get("authority") == "source_observation":
        return "source_observation"
    return "authoritative"


def project_authority_snapshot(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Annotate graph nodes with the same authority projection used by recall."""

    effective_now = now or datetime.now(UTC)
    superseded_ids = {
        str(edge.get("target_id"))
        for edge in snapshot.get("edges", [])
        if str(edge.get("relationship")) == RelationType.UPDATES.value
    }
    projected = dict(snapshot)
    projected["nodes"] = [
        {
            **node,
            "authority_status": authority_status(
                node,
                now=effective_now,
                superseded_by_update=str(node.get("id")) in superseded_ids,
            ),
        }
        for node in snapshot.get("nodes", [])
    ]
    return projected


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
        "authority": str(metadata.get("authority") or node.get("authority_status") or "unknown"),
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
    project_id: str = "",
    agent_id: str = "",
    session_id: str = "",
    section_limit: int = _DEFAULT_SECTION_LIMIT,
) -> dict[str, Any]:
    """Compile a compact authoritative project brief from scoped Waggle nodes."""

    project = _validate_project_id(resolve_active_project(graph, project_id))
    if section_limit < 1 or section_limit > 25:
        raise ValidationFailure("section_limit must be between 1 and 25.")

    snapshot = graph.get_graph_snapshot(project=project, agent_id=agent_id, session_id=session_id)
    now = datetime.now(UTC)
    snapshot = project_authority_snapshot(snapshot, now=now)
    nodes = [
        node
        for node in snapshot.get("nodes", [])
        if node["authority_status"] == "authoritative"
        and _node_belongs_to_project(node, project)
        and _node_value(node, "metadata", {}).get("authority") != "source_observation"
    ]
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

    repository_context = repository_context_for_project(graph, project)
    registered = repository_context.get("project") or {}
    project_payload = {
        "id": project,
        "name": str(registered.get("project_name") or _project_name(project)),
    }
    if registered:
        project_payload.update(
            {
                "root": registered.get("project_root", ""),
                "repository": registered.get("repository", ""),
                "git_remote": registered.get("git_remote", ""),
                "identity_source": registered.get("identity_source", ""),
            }
        )
    observations = repository_context.get("observations", [])
    repository_recent = [item for item in observations if item.get("category") == "recent_commits"]
    observations_by_category = {str(item.get("category")): item for item in observations}
    decision_payloads = [_memory_payload(node) for node in decisions[:section_limit]]
    purpose = (
        str(goals[0].get("content", ""))
        if goals
        else str(observations_by_category.get("purpose", {}).get("content", ""))
    )
    architecture = [
        observations_by_category[category]
        for category in ("architecture", "storage", "components", "deployment")
        if category in observations_by_category
    ]
    tech_stack = [
        item.strip()
        for item in str(observations_by_category.get("stack", {}).get("content", "")).split(",")
        if item.strip()
    ]
    repository_conflicts: list[dict[str, Any]] = []
    for item in observations:
        if isinstance(item.get("repository_change"), dict):
            repository_conflicts.append(
                {
                    "category": item.get("category"),
                    "memory_id": item.get("memory_id"),
                    **item["repository_change"],
                }
            )
        repository_conflicts.extend(
            {
                "category": item.get("category"),
                "memory_id": item.get("memory_id"),
                **conflict,
            }
            for conflict in item.get("authority_conflicts", [])
            if isinstance(conflict, dict)
        )

    return {
        "project": project_payload,
        "purpose": purpose,
        "purpose_authority": "authoritative" if goals else "source_observation",
        "purpose_provenance": (_memory_payload(goals[0]) if goals else observations_by_category.get("purpose")),
        "goal": purpose,
        "architecture": architecture,
        "tech_stack": tech_stack,
        "current_state": [_memory_payload(node) for node in current_state[:section_limit]],
        "authoritative_decisions": decision_payloads,
        "decisions": decision_payloads,
        "constraints": [_memory_payload(node) for node in constraints[:section_limit]],
        "open_questions": [_memory_payload(node) for node in open_questions[:section_limit]],
        "recent_changes": [
            *[_memory_payload(node) for node in nodes[:section_limit]],
            *repository_recent[:1],
        ][:section_limit],
        "repository_context": observations,
        "repository_conflicts": repository_conflicts,
        "authority_model": {
            "repository_observations": "source_observation",
            "waggle_memory": "authoritative_memory",
        },
        "supporting_memory_ids": supporting_memory_ids,
        "generated_at": now.isoformat(),
    }


def _node_is_current_authority(node: Any, *, now: datetime) -> bool:
    return authority_status(node, now=now) == "authoritative"


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
    project_id: str = "",
    query: str,
    limit: int = 5,
    agent_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Recall current authoritative memories through Waggle's existing retrieval."""

    project = _validate_project_id(resolve_active_project(graph, project_id))
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
    update_targets: dict[str, list[str]] = {}
    superseded_ids: set[str] = set()
    for edge in result.edges:
        relationship = str(edge.relationship.value if hasattr(edge.relationship, "value") else edge.relationship)
        if relationship != "updates":
            continue
        update_targets.setdefault(str(edge.source_id), []).append(str(edge.target_id))
        superseded_ids.add(str(edge.target_id))

    authoritative = [
        node
        for node in result.nodes
        if authority_status(
            node,
            now=now,
            superseded_by_update=str(node.id) in superseded_ids,
        )
        == "authoritative"
        and _node_belongs_to_project(node, project)
        and (getattr(node, "metadata", {}) or {}).get("authority") != "source_observation"
    ]
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
    explicit_project = str(_node_value(node, "project", "")).strip().lower()
    if explicit_project:
        return explicit_project == normalized
    tags = {str(tag).strip().lower() for tag in _node_value(node, "tags", [])}
    return normalized in tags or f"project:{normalized}" in tags


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
    project_id: str = "",
    memory_id: str,
    proposed_content: str,
    reason: str = "",
    evidence_ids: list[str] | None = None,
    proposed_by_type: str = "agent",
    proposed_by_id: str = "webmcp",
) -> tuple[dict[str, Any], bool]:
    """Persist a pending proposal without changing authoritative graph state."""

    project = _validate_project_id(resolve_active_project(graph, project_id))
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
    # Non-security fingerprint for retry deduplication, never a credential verifier.
    # HTTP actor_id is a display name or public API-key record UUID, not the secret.
    # Keep the digest stable so retries also find proposals created before upgrades.
    dedupe_key = hashlib.sha256(dedupe_payload.encode("utf-8"), usedforsecurity=False).hexdigest()
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


def _proposal_error(code: str, message: str, *, status_code: int = 409) -> WaggleError:
    return WaggleError(code, message, status_code=status_code)


def _load_node_from_connection(graph: Any, connection: Any, memory_id: str) -> Any | None:
    row = graph._fetch_node_row(connection, memory_id)
    return graph._row_to_node(row) if row is not None else None


def _proposal_target_is_current(graph: Any, connection: Any, proposal: dict[str, Any]) -> tuple[bool, Any | None]:
    target = _load_node_from_connection(graph, connection, proposal["target"]["memory_id"])
    if target is None or not _node_belongs_to_project(target, proposal["project_id"]):
        return False, target
    if not _node_is_current_authority(target, now=datetime.now(UTC)):
        return False, target
    incoming_update = connection.execute(
        """
        SELECT 1 FROM edges
        WHERE tenant_id = ? AND target_id = ? AND relationship = ?
        LIMIT 1
        """,
        (str(graph.tenant_id), str(target.id), RelationType.UPDATES.value),
    ).fetchone()
    if incoming_update is not None:
        return False, target
    return _target_version(target) == proposal["target"]["version"], target


def review_memory_change(
    graph: Any,
    repository: ProposalRepository,
    *,
    proposal_id: str,
    action: str,
    approved_content: str | None = None,
    review_note: str = "",
    reviewed_by: str = "local-human",
    project_id: str = "",
) -> dict[str, Any]:
    """Apply an immutable human review decision without mutating graph memory."""

    proposal_key = str(proposal_id or "").strip()
    normalized_action = str(action or "").strip().lower()
    if not proposal_key:
        raise ValidationFailure("proposal_id is required.")
    if normalized_action not in {"approve", "reject"}:
        raise ValidationFailure("action must be either 'approve' or 'reject'.")
    reviewer = str(reviewed_by or "local-human").strip() or "local-human"
    note = str(review_note or "").strip()
    if len(note) > _MAX_REASON_LENGTH:
        raise ValidationFailure(f"review_note must be at most {_MAX_REASON_LENGTH} characters.")

    stale = False
    with graph._lock, graph._pool.checkout() as connection:
        proposal = repository.get(
            tenant_id=str(graph.tenant_id),
            proposal_id=proposal_key,
            connection=connection,
        )
        if proposal is None:
            raise ValidationFailure("proposal_id does not identify an existing proposal.")
        if project_id and proposal["project_id"] != project_id:
            raise ValidationFailure("proposal_id does not identify a proposal in this project.")
        if proposal["status"] != "pending":
            raise _proposal_error(
                "PROPOSAL_NOT_PENDING",
                f"Proposal cannot be reviewed from status '{proposal['status']}'.",
            )
        is_current, _ = _proposal_target_is_current(graph, connection, proposal)
        if not is_current:
            repository.mark_stale(
                tenant_id=str(graph.tenant_id),
                proposal_id=proposal_key,
                connection=connection,
            )
            graph.emit_audit_event(
                event_type="proposal.stale",
                actor_type="human",
                actor_id=reviewer,
                resource_type="memory_change_proposal",
                resource_id=proposal_key,
                action="stale",
                metadata={
                    "project_id": proposal["project_id"],
                    "target_memory_id": proposal["target"]["memory_id"],
                    "phase": "review",
                },
                connection=connection,
            )
            stale = True
        else:
            approved_value: str | None = None
            if normalized_action == "approve":
                approved_value = (
                    str(approved_content).strip() if approved_content is not None else proposal["proposed_content"]
                )
                if not approved_value:
                    raise ValidationFailure("approved_content must not be empty.")
                if len(approved_value) > _MAX_PROPOSED_CONTENT_LENGTH:
                    raise ValidationFailure(
                        f"approved_content must be at most {_MAX_PROPOSED_CONTENT_LENGTH} characters."
                    )
            reviewed = repository.review_pending(
                tenant_id=str(graph.tenant_id),
                proposal_id=proposal_key,
                action=normalized_action,
                reviewed_by=reviewer,
                approved_content=approved_value,
                review_note=note,
                connection=connection,
            )
            if reviewed is None:  # pragma: no cover - serialized by the graph lock
                raise _proposal_error("PROPOSAL_NOT_PENDING", "Proposal is no longer pending.")
            event_type = "proposal.rejected"
            if normalized_action == "approve":
                event_type = (
                    "proposal.edited_and_approved"
                    if approved_value != proposal["proposed_content"]
                    else "proposal.approved"
                )
            graph.emit_audit_event(
                event_type=event_type,
                actor_type="human",
                actor_id=reviewer,
                resource_type="memory_change_proposal",
                resource_id=proposal_key,
                action=normalized_action,
                metadata={"project_id": proposal["project_id"], "target_memory_id": proposal["target"]["memory_id"]},
                connection=connection,
            )

    if stale:
        raise _proposal_error(
            "PROPOSAL_STALE",
            "The target memory changed after this proposal was created.",
        )
    return reviewed


def _applied_response(proposal: dict[str, Any], node: Any, *, already_applied: bool) -> dict[str, Any]:
    return {
        "proposal_id": proposal["proposal_id"],
        "status": "applied",
        "authoritative_memory": _authoritative_memory_payload(
            node,
            supersedes=proposal["target"]["memory_id"],
        ),
        "already_applied": already_applied,
        "proposal": proposal,
    }


def apply_approved_memory_change(
    graph: Any,
    repository: ProposalRepository,
    *,
    proposal_id: str,
    project_id: str = "",
    applied_by: str = "webmcp",
    applied_actor_type: str = "agent",
) -> dict[str, Any]:
    """Atomically apply the exact human-approved payload through native update lineage."""

    proposal_key = str(proposal_id or "").strip()
    project = _validate_project_id(resolve_active_project(graph, project_id))
    if not proposal_key:
        raise ValidationFailure("proposal_id is required.")

    stale = False
    with graph._lock, graph._pool.checkout() as connection:
        proposal = repository.get(
            tenant_id=str(graph.tenant_id),
            proposal_id=proposal_key,
            connection=connection,
        )
        if proposal is None or proposal["project_id"] != project:
            raise ValidationFailure("proposal_id does not identify a proposal in this project.")
        if proposal["status"] == "applied":
            result = _load_node_from_connection(graph, connection, str(proposal["result_memory_id"] or ""))
            if result is None:
                raise _proposal_error("APPLIED_MEMORY_MISSING", "The applied proposal result memory is missing.")
            return _applied_response(proposal, result, already_applied=True)
        if proposal["status"] == "stale":
            raise _proposal_error("PROPOSAL_STALE", "The target memory changed after this proposal was created.")
        if proposal["status"] != "approved":
            raise _proposal_error(
                "PROPOSAL_NOT_APPROVED",
                f"Proposal cannot be applied from status '{proposal['status']}'.",
            )

        is_current, target = _proposal_target_is_current(graph, connection, proposal)
        if not is_current or target is None:
            repository.mark_stale(
                tenant_id=str(graph.tenant_id),
                proposal_id=proposal_key,
                connection=connection,
            )
            graph.emit_audit_event(
                event_type="proposal.stale",
                actor_type=applied_actor_type,
                actor_id=applied_by,
                resource_type="memory_change_proposal",
                resource_id=proposal_key,
                action="stale",
                metadata={"project_id": project, "target_memory_id": proposal["target"]["memory_id"], "phase": "apply"},
                connection=connection,
            )
            stale = True
        else:
            approved_value = str(proposal["approved_content"] or "")
            if not approved_value:  # pragma: no cover - review prevents empty approval
                raise _proposal_error("PROPOSAL_NOT_APPROVED", "Proposal has no immutable approved content.")
            metadata = dict(target.metadata)
            for key in (
                "superseded_by",
                "superseded_at",
                "superseded_relationship",
                "logically_superseded",
                "logically_superseded_by",
                "head_rejected_reason",
            ):
                metadata.pop(key, None)
            metadata.update(
                {
                    "authority": "authoritative",
                    "source_type": "human_approved_proposal",
                    "governance": {
                        "proposal_id": proposal_key,
                        "proposed_by": proposal["proposed_by"],
                        "reviewed_by": proposal["reviewed_by"],
                        "reviewed_at": proposal["reviewed_at"],
                        "reason": proposal["reason"],
                        "evidence_ids": proposal["evidence_ids"],
                        "approved_content_sha256": hashlib.sha256(approved_value.encode("utf-8")).hexdigest(),
                    },
                }
            )
            created = graph.add_node(
                label=target.label,
                content=approved_value,
                node_type=target.node_type,
                tags=list(target.tags),
                source_prompt=target.source_prompt,
                source_turn_pair_id=target.source_turn_pair_id,
                agent_id=target.agent_id,
                project=target.project,
                session_id=target.session_id,
                evidence_records=list(target.evidence_records),
                valid_from=datetime.now(UTC),
                context_window_id=target.context_window_id,
                metadata=metadata,
                connection=connection,
                force_new=True,
            ).node
            graph.add_edge(
                source_id=created.id,
                target_id=target.id,
                relationship=RelationType.UPDATES,
                metadata={"proposal_id": proposal_key, "reviewed_by": proposal["reviewed_by"]},
                connection=connection,
            )
            # Main's native updates edge records supersession metadata. Close
            # the reviewed target's validity in this same transaction as well.
            connection.execute(
                "UPDATE nodes SET valid_to = ? WHERE id = ? AND tenant_id = ?",
                (created.valid_from.isoformat(), target.id, str(graph.tenant_id)),
            )
            applied = repository.mark_applied(
                tenant_id=str(graph.tenant_id),
                proposal_id=proposal_key,
                result_memory_id=created.id,
                connection=connection,
            )
            if applied is None:  # pragma: no cover - serialized by the graph lock
                raise _proposal_error("PROPOSAL_NOT_APPROVED", "Proposal is no longer approved.")
            graph.emit_audit_event(
                event_type="proposal.applied",
                actor_type=applied_actor_type,
                actor_id=applied_by,
                resource_type="memory_change_proposal",
                resource_id=proposal_key,
                action="apply",
                metadata={"project_id": project, "target_memory_id": target.id, "result_memory_id": created.id},
                connection=connection,
            )
            graph.emit_audit_event(
                event_type="memory.superseded",
                actor_type=applied_actor_type,
                actor_id=applied_by,
                resource_type="node",
                resource_id=target.id,
                action="supersede",
                metadata={"proposal_id": proposal_key, "result_memory_id": created.id},
                connection=connection,
            )

    if stale:
        raise _proposal_error(
            "PROPOSAL_STALE",
            "The target memory changed after this proposal was created.",
        )
    return _applied_response(applied, created, already_applied=False)
