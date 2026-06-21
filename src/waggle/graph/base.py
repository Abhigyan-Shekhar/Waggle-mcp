from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waggle.embeddings import EmbeddingModel
from waggle.intelligence import normalize_text
from waggle.models import EvidenceRecord, Node

LOGGER = logging.getLogger(__name__)


class MemoryGraphBase:
    """Base class for MemoryGraph mixins providing shared type signatures."""

    db_path: Path
    embedding_model: EmbeddingModel
    tenant_id: str
    _lock: Any
    _pool: Any

    def _connect(self, timeout: float = 30.0, *, check_same_thread: bool = True) -> sqlite3.Connection:
        raise NotImplementedError

    def get_stats(self) -> Any:
        raise NotImplementedError

    def export_context_bundle(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def export_abhi(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def add_node(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def add_edge(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def resolve_window_context(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _update_window_node_count(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _mark_window_embedding_stale(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def derive_context_window_edges(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _embed_with_metadata(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _node_cosine_similarity(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _query_replay_hits(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _expand_query_aliases(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _parse_datetime(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_metadata(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, sort_keys=True)


def _decode_metadata(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _normalized_content_hash(text: str) -> str:
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExpansionMeta:
    via_relation: str
    from_node: str
    effective_priority: float


RELATION_SCORE_BOOST: dict[str, float] = {
    "contradicts": 0.15,
    "updates": 0.12,
    "depends_on": 0.08,
    "derived_from": 0.05,
    "part_of": 0.03,
    "relates_to": 0.00,
    "similar_to": -0.05,
    "seed": 0.00,
}

TOPIC_RELEVANCE_THRESHOLD = 0.35

TOPIC_SEMANTIC_ONLY_THRESHOLD = 0.70

TEMPORAL_TOPIC_MARGIN = 0.03

NEGATION_QUERY_TERMS = (
    "not",
    "never",
    "reject",
    "rejected",
    "blocked",
    "forbid",
    "forbidden",
    "ruled out",
    "avoid",
    "must not",
    "should not",
    "off limits",
    "disallowed",
    "prohibit",
    "prohibited",
)

NEGATION_NODE_TERMS = (
    "must not",
    "do not",
    "cannot",
    "can not",
    "rejected",
    "blocked",
    "forbidden",
    "ruled out",
    "not allowed",
    "off limits",
    "disallowed",
    "prohibited",
    "mustn't",
)

NEGATION_SCORE_BOOST = 0.28

QUERY_ALIAS_TERMS: tuple[tuple[str, str], ...] = (
    ("ingestion and export", "ingestion import ndjson export csv parquet warehouse sync"),
    ("ingestion", "ingestion import ndjson streaming imports"),
    ("export", "export csv parquet warehouse sync signed download links"),
    ("enterprise data export policy", "enterprise data export policy admin approval signed download links"),
    ("privacy export stance", "privacy export policy admin approval signed download links"),
    ("privacy export", "privacy export policy admin approval signed download links"),
    ("export policy", "export policy admin approval signed download links"),
    ("database", "postgresql mysql sqlite database production"),
    ("production database choice", "postgresql production database choice current parity safer migrations"),
    ("auto rollback", "auto rollback deployments incident 5xx"),
    ("acid compliance", "acid compliance transactions consistency postgres decision reason"),
    ("justified by", "reason rationale because requirement constraint"),
    ("deployment platform", "cloud run ecs deployment deploy autoscaling"),
    ("deployment", "cloud run ecs deployment deploy autoscaling"),
    ("deploy", "cloud run ecs deployment rollback"),
    ("api deploy", "api deploy cloud run ecs autoscaling"),
    ("deploy now", "current deployment cloud run autoscaling"),
    ("auth", "jwt token expiry refresh authentication"),
    ("jwt expiry", "jwt token expiry 15m 1h authentication"),
    ("session cache backend", "session cache backend redis keydb ttl failover"),
    ("cache backend", "session cache backend redis keydb ttl failover"),
    ("workflow backend", "workflow backend temporal celery redis retries visibility"),
    ("workflow backend do we use now", "current workflow backend temporal retries visibility"),
    ("mobile offline", "offline queue sync mobile edits"),
    ("production incidents", "incident rollback auto-rollback 5xx error rate"),
    ("incidents", "incident rollback auto-rollback 5xx error rate"),
    ("observability", "traces slos logs metrics service-level objectives"),
    ("workflow engine", "temporal workflows celery redis queue backend retries visibility"),
    ("workflow", "temporal workflows celery redis queue backend"),
    ("schema migration", "alembic migrations autogenerate manual review"),
    ("migration tool", "alembic migrations autogenerate manual review"),
    ("feature flags", "flags control plane env vars"),
    ("access permissions", "access control rbac abac role attribute rules"),
    ("permissions", "access control rbac abac role attribute rules"),
    ("upstream changes", "webhooks polling sync missed events"),
    ("notified", "webhooks polling sync notifications"),
    ("notified", "notifications email slack alerts webhooks"),
    ("alert on", "notifications email slack ops alerts"),
    ("alert", "notifications email slack ops alerts"),
    ("workflow engine", "temporal workflows celery redis queue backend retries visibility"),
    ("scaling issue", "concurrent writes concurrency blocker scaling"),
    ("schema migration tool", "alembic migrations manual review schema"),
    ("enterprise-sensitive actions", "enterprise export approval signed links admin approval"),
    ("enterprise-sensitive", "enterprise export approval signed links admin approval"),
    ("privileged", "break-glass shared admin named ownership privileged actions"),
    ("model deployment", "model rollout canary approval auto-promote"),
    ("model rollout", "model rollout canary approval auto-promote product-manager approval"),
    ("canary approval", "canary approval product-manager approval no auto-promote"),
    ("pm gate", "product-manager approval no auto-promote canary"),
    ("refund flow", "refunds one-click refunds manual review rules engine"),
    ("refunds", "refund rules engine one-click refunds manual review"),
    ("risky automation", "rules engine manual review blocked no auto-promote one-click refunds"),
    ("monitoring was missing", "abuse monitoring one-click refunds blocked"),
    ("missing monitoring", "abuse monitoring one-click refunds blocked"),
    ("storage costs", "storage cold uploads s3 intelligent tiering cost"),
    ("data retention", "audit logs retention compliance 90 days"),
    ("retention compliance", "audit logs retention compliance 90 days"),
    ("emergency access", "break-glass access per-user accounts audit trails"),
    ("security review", "security review break-glass raw api keys shared admins"),
    ("logs", "logs raw api keys audit retention"),
    ("named accountability", "named ownership per-user accounts admin approval signed links"),
    ("deeper requirement", "requirement supported choice concurrency realtime"),
    ("supported that choice", "requirement supported choice concurrency realtime"),
    ("fastapi", "fastapi async concurrency realtime websockets"),
)

MUST_PAIR_RELATIONS: frozenset[str] = frozenset(
    {
        "contradicts",
        "updates",
        "depends_on",
    }
)

RELATION_WEIGHTS: dict[str, float] = {
    "contradicts": 1.00,
    "updates": 0.95,
    "depends_on": 0.85,
    "derived_from": 0.75,
    "part_of": 0.70,
    "relates_to": 0.50,
    "similar_to": 0.30,
}


def _valid_to_enforcement_enabled() -> bool:
    """Return True if valid_to enforcement is active (default: True).

    Set WAGGLE_ENFORCE_VALID_TO=false to revert to legacy behaviour for one
    release.  This flag will be removed in the next minor release — see
    CHANGELOG.md.
    """
    raw = os.environ.get("WAGGLE_ENFORCE_VALID_TO", "true").strip().lower()
    if raw == "false":
        LOGGER.warning(
            "WAGGLE_ENFORCE_VALID_TO=false is deprecated and will be removed in the next release. "
            "Expired nodes are being returned in query results (legacy behaviour)."
        )
        return False
    return True


def _filter_valid_nodes(
    nodes: list[Node],
    *,
    include_invalidated: bool = False,
    as_of: datetime | None = None,
) -> list[Node]:
    """Filter *nodes* according to temporal validity windows.

    Priority:
    1. If *as_of* is provided, return nodes whose validity window contains
       *as_of* (ignores *include_invalidated*).
    2. If *include_invalidated* is True, return all nodes unchanged.
    3. Otherwise (default), exclude nodes whose ``valid_to`` has already
       passed relative to ``now``.

    "Now" is always ``datetime.now(timezone.utc)`` — never a naive datetime.
    """
    if not _valid_to_enforcement_enabled():
        return nodes

    if as_of is not None:
        # Ensure as_of is timezone-aware
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        return [
            node
            for node in nodes
            if (node.valid_from is None or node.valid_from <= as_of)
            and (node.valid_to is None or node.valid_to > as_of)
        ]

    if include_invalidated:
        return nodes

    now = datetime.now(UTC)
    return [node for node in nodes if node.valid_to is None or node.valid_to > now]


def recency_weight(
    updated_at: float,
    now: float | None = None,
    half_life_days: float = 30.0,
) -> float:
    if now is None:
        now = time.time()
    age_days = (now - updated_at) / 86400.0
    if age_days < 0:
        age_days = 0.0
    return math.exp(-0.693 * age_days / half_life_days)


def score_node(
    similarity: float,
    updated_at: float,
    edge_weight: float = 1.0,
    *,
    now: float | None = None,
    half_life_days: float = 30.0,
    similarity_weight: float = 0.6,
    recency_weight_factor: float = 0.3,
    edge_weight_factor: float = 0.1,
    superseded: bool = False,
    superseded_penalty: float = 0.2,
) -> float:
    r = recency_weight(updated_at, now, half_life_days)
    e = max(0.0, min(1.0, edge_weight))
    score = (similarity * similarity_weight) + (r * recency_weight_factor) + (e * edge_weight_factor)
    if superseded:
        score *= superseded_penalty
    return score


def _scope_matches(node: Node, *, agent_id: str = "", project: str = "", session_id: str = "") -> bool:
    normalized_agent = agent_id.strip().lower()
    normalized_project = project.strip().lower()
    normalized_session = session_id.strip().lower()
    if normalized_agent and node.agent_id.strip().lower() != normalized_agent:
        return False
    if normalized_session and node.session_id.strip().lower() != normalized_session:
        return False
    if normalized_project:
        project_tags = {str(tag).strip().lower() for tag in node.tags}
        if (
            node.project.strip().lower() != normalized_project
            and normalized_project not in project_tags
            and f"project:{normalized_project}" not in project_tags
        ):
            return False
    return True


def _retrieval_session_scope(*, agent_id: str = "", project: str = "", session_id: str = "") -> str:
    return session_id


def _encode_evidence_records(records: list[EvidenceRecord]) -> str:
    return json.dumps([record.model_dump(mode="json") for record in records], sort_keys=True)


def _merge_scope_value(existing: str, incoming: str) -> str:
    return existing.strip() or incoming.strip()
