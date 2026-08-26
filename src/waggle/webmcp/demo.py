"""Deterministic, cookie-scoped challenge demo state."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from waggle.errors import ValidationFailure
from waggle.models import NodeType, RelationType

from .proposals import ProposalRepository

DEMO_PUBLIC_PROJECT_ID = "waggle-webmcp"
DEMO_COOKIE_NAME = "waggle_demo_session"
DEMO_COOKIE_MAX_AGE = 60 * 60 * 24 * 7
_DEMO_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24,96}$")


@dataclass(frozen=True, slots=True)
class DemoScope:
    session_id: str
    namespace: str
    tenant_id: str
    project_id: str

    def node_id(self, slug: str) -> str:
        return f"demo-{self.namespace}-{slug}"

    def edge_id(self, slug: str) -> str:
        return f"demo-{self.namespace}-edge-{slug}"


def valid_demo_session_id(value: str) -> bool:
    return bool(_DEMO_SESSION_PATTERN.fullmatch(str(value or "")))


def resolve_demo_scope(session_id: str) -> DemoScope:
    normalized = str(session_id or "").strip()
    if not valid_demo_session_id(normalized):
        raise ValidationFailure("Invalid challenge demo session.")
    namespace = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return DemoScope(
        session_id=normalized,
        namespace=namespace,
        tenant_id=f"demo_{namespace}",
        project_id=f"demo_{namespace}_{DEMO_PUBLIC_PROJECT_ID}",
    )


def resolve_public_project(scope: DemoScope, supplied_project: Any) -> str:
    project = str(supplied_project or "").strip()
    if project != DEMO_PUBLIC_PROJECT_ID:
        raise ValidationFailure(f"Challenge demo project_id must be '{DEMO_PUBLIC_PROJECT_ID}'.")
    return scope.project_id


def publicize_demo_payload(value: Any, scope: DemoScope) -> Any:
    """Hide the physical namespace from public browser and WebMCP payloads."""

    if isinstance(value, dict):
        return {key: publicize_demo_payload(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [publicize_demo_payload(item, scope) for item in value]
    if isinstance(value, tuple):
        return tuple(publicize_demo_payload(item, scope) for item in value)
    if value == scope.project_id:
        return DEMO_PUBLIC_PROJECT_ID
    if value == scope.tenant_id:
        return "challenge-demo"
    return value


def _seed_records() -> list[tuple[str, str, str, NodeType, list[str]]]:
    return [
        ("goal", "Project goal", "Give humans and AI agents one governed, portable memory they can safely share.", NodeType.NOTE, ["goal"]),
        ("storage", "Storage architecture", "Use Neo4j as the primary storage engine.", NodeType.DECISION, ["hero", "storage", "architecture"]),
        ("local-first", "Local-first product direction", "Waggle should preserve a local-first default.", NodeType.DECISION, ["local-first", "architecture"]),
        ("human-review", "Human approval boundary", "Human approval is required before an agent-proposed correction becomes authoritative.", NodeType.DECISION, ["governance"]),
        ("one-origin", "Hosted demo topology", "Serve the workspace, WebMCP tools, and API from one origin.", NodeType.DECISION, ["deployment"]),
        ("lineage", "Correction lineage", "Every applied correction must preserve the memory it supersedes.", NodeType.DECISION, ["governance", "provenance"]),
        ("constraint-agent", "Agent write constraint", "Agents must not directly overwrite authoritative memory.", NodeType.NOTE, ["constraint", "governance"]),
        ("constraint-stale", "Stale-state constraint", "Stale proposals must never overwrite newer truth.", NodeType.NOTE, ["constraint", "safety"]),
        ("constraint-scope", "Project isolation constraint", "Every retrieval and mutation must remain inside the active project scope.", NodeType.NOTE, ["constraint", "security"]),
        ("sqlite", "SQLite capability", "SQLite is available without external infrastructure and supports Waggle's local-first runtime.", NodeType.FACT, ["storage", "evidence"]),
        ("neo4j", "Neo4j capability", "Neo4j remains useful as an optional graph backend for networked deployments.", NodeType.FACT, ["storage", "evidence"]),
        ("webmcp", "WebMCP integration", "The workspace exposes four site tools: project brief, authoritative recall, proposal creation, and approved application.", NodeType.FACT, ["webmcp"]),
        ("fingerprint", "Proposal fingerprints", "Every proposal captures the exact target memory version so review and apply can detect stale state.", NodeType.FACT, ["governance", "safety"]),
        ("immutable", "Immutable approvals", "After human approval, the exact approved payload is frozen and cannot be altered by the applying agent.", NodeType.FACT, ["governance"]),
        ("portable", "Portable memory", "Waggle memory can follow a project across supported agents and local development tools.", NodeType.CONCEPT, ["product"]),
        ("audit", "Human-agent audit trail", "Meaningful recalls, proposals, reviews, applications, and resets are visible in the workspace activity timeline.", NodeType.FACT, ["audit"]),
        ("question-host", "Hosted persistence question", "What persistence policy should the public challenge deployment use after judging?", NodeType.QUESTION, ["deployment"]),
        ("question-evidence", "Evidence inspection question", "Should evidence inspection become a fifth WebMCP tool after hosted discovery is proven?", NodeType.QUESTION, ["roadmap"]),
        ("phase5", "Workspace-first experience", "Overview, Memories, Proposals, Activity, and Graph Studio make governed memory understandable without terminal setup.", NodeType.FACT, ["current-state", "workspace"]),
        ("phase6", "Challenge judge mode", "Each browser receives an isolated, deterministic, resettable demonstration workspace.", NodeType.FACT, ["current-state", "demo"]),
        ("recall", "Authoritative recall", "Normal recall returns only current authoritative memory while retaining supersession provenance.", NodeType.FACT, ["retrieval"]),
        ("proposal-state", "Proposal lifecycle", "Pending proposals may become approved, rejected, stale, or applied through explicit transitions.", NodeType.FACT, ["governance"]),
        ("old-context", "Previous memory workflow", "Project context is copied manually between agent conversations.", NodeType.NOTE, ["historical"]),
        ("current-context", "Current memory workflow", "Waggle automatically recalls scoped project context across participating agents.", NodeType.FACT, ["current-state"]),
        ("license", "Open-source license", "The public Waggle repository includes an Apache-2.0 license.", NodeType.FACT, ["release"]),
    ]


def _seed_demo(graph: Any, scope: DemoScope, *, connection: Any) -> None:
    base_time = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    for index, (slug, label, content, node_type, tags) in enumerate(_seed_records()):
        timestamp = base_time + timedelta(minutes=index)
        graph.add_node(
            node_id=scope.node_id(slug),
            label=label,
            content=content,
            node_type=node_type,
            tags=[*tags, f"project:{scope.project_id}"],
            source_prompt="Seeded challenge demonstration fixture.",
            agent_id="waggle-demo",
            project=scope.project_id,
            session_id=scope.session_id,
            valid_from=timestamp,
            observed_at=timestamp,
            metadata={"authority": "authoritative", "source_type": "challenge_demo_seed"},
            connection=connection,
            force_new=True,
        )

    edges = [
        ("storage-local", "storage", "local-first", RelationType.RELATES_TO),
        ("storage-sqlite", "storage", "sqlite", RelationType.RELATES_TO),
        ("storage-neo4j", "storage", "neo4j", RelationType.RELATES_TO),
        ("review-agent", "human-review", "constraint-agent", RelationType.DEPENDS_ON),
        ("fingerprint-stale", "fingerprint", "constraint-stale", RelationType.DERIVED_FROM),
        ("immutable-review", "immutable", "human-review", RelationType.DERIVED_FROM),
        ("recall-lineage", "recall", "lineage", RelationType.DEPENDS_ON),
        ("phase6-origin", "phase6", "one-origin", RelationType.DEPENDS_ON),
        ("phase6-scope", "phase6", "constraint-scope", RelationType.DEPENDS_ON),
        ("current-old", "current-context", "old-context", RelationType.UPDATES),
    ]
    for slug, source, target, relationship in edges:
        graph.add_edge(
            edge_id=scope.edge_id(slug),
            source_id=scope.node_id(source),
            target_id=scope.node_id(target),
            relationship=relationship,
            metadata={"source": "challenge_demo_seed"},
            connection=connection,
        )

    graph.emit_audit_event(
        event_type="demo.workspace.seeded",
        actor_type="system",
        actor_id="Waggle",
        resource_type="project",
        resource_id=scope.project_id,
        action="seed",
        metadata={"project": scope.project_id, "authoritative_memories": 24},
        created_at=base_time + timedelta(minutes=26),
        connection=connection,
    )


def ensure_demo_seed(graph: Any, repository: ProposalRepository, scope: DemoScope) -> None:
    """Create the deterministic fixture once for a fresh cookie namespace."""

    del repository  # Kept in the signature to make the shared store boundary explicit.
    with graph._lock, graph._pool.checkout() as connection:
        existing = graph._fetch_node_row(connection, scope.node_id("storage"))
        if existing is None:
            _seed_demo(graph, scope, connection=connection)


def reset_demo(graph: Any, repository: ProposalRepository, scope: DemoScope) -> dict[str, Any]:
    """Atomically clear and reseed exactly one challenge demo session."""

    with graph._lock, graph._pool.checkout() as connection:
        graph._clear_scope_rows(connection, scope="project", project=scope.project_id, dry_run=False)
        repository.clear_project(
            tenant_id=str(graph.tenant_id),
            project_id=scope.project_id,
            connection=connection,
        )
        connection.execute("DELETE FROM audit_events WHERE tenant_id = ?", (str(graph.tenant_id),))
        _seed_demo(graph, scope, connection=connection)
        graph.emit_audit_event(
            event_type="demo.reset",
            actor_type="human",
            actor_id="local-human",
            resource_type="project",
            resource_id=scope.project_id,
            action="reset",
            metadata={"project": scope.project_id},
            connection=connection,
        )
    return {
        "status": "reset",
        "project_id": DEMO_PUBLIC_PROJECT_ID,
        "authoritative_memory_count": 24,
        "pending_proposal_count": 0,
        "hero_memory_id": scope.node_id("storage"),
        "hero_content": "Use Neo4j as the primary storage engine.",
    }
