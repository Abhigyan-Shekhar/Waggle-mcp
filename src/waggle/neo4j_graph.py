from __future__ import annotations

import base64
import json
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import networkx as nx
import numpy as np

from waggle.abhi import (
    ABHI_ENCRYPTION_ALGORITHM,
    ABHI_SPEC_VERSION,
    abhi_to_snapshot,
    diff_abhi_files,
    dispatch_abhi_event,
    filter_snapshot_by_scope,
    inspect_abhi_document,
    load_abhi_chunk_file,
    load_abhi_document,
    merge_abhi_files,
    query_abhi_file,
    validate_abhi_document,
    write_abhi_document,
)
from waggle.auth import api_key_prefix, generate_api_key, hash_api_key, verify_api_key
from waggle.context_bundle import build_context_bundle, build_query_summary, export_context_bundle_files
from waggle.errors import AuthenticationError, ValidationFailure
from waggle.evidence import build_observation_evidence, merge_evidence_records, merge_validity_windows
from waggle.intelligence import (
    canonical_concept_overlap,
    compatible_node_types,
    contains_conflicting_months,
    contains_conflicting_numbers,
    content_token_jaccard,
    describes_rejected_or_limited_option,
    detect_conflict_reason,
    extract_choice_entity,
    extract_conversation_candidates,
    infer_label,
    infer_node_type,
    infer_relationship,
    infer_temporal_hints,
    is_acronym_match,
    label_similarity,
    lexical_overlap,
    normalize_text,
    paraphrase_dedup_score,
    parse_since_value,
    score_node,
    split_atomic_items,
    summarize_topic,
    temporal_score_adjustment,
    tokenize_text,
    type_aware_dedup_threshold,
    within_time_window,
)
from waggle.markdown_vault import (
    evidence_from_lines,
    iter_vault_documents,
    render_node_document,
    slugify,
    vault_filename,
)
from waggle.models import (
    AbhiChunkLoadResult,
    AbhiDiffResult,
    AbhiExportResult,
    AbhiImportResult,
    AbhiInspectResult,
    AbhiMergeResult,
    AbhiQueryResult,
    AbhiValidationResult,
    ApiKeyCreateResult,
    ApiKeyRecord,
    AuditEventRecord,
    BackupResult,
    ConflictEntry,
    ConflictListResult,
    ConflictRecord,
    ConnectedNodeStat,
    ContextBundleExportResult,
    ContextScopeResult,
    ContextTimelineItem,
    ContextWindow,
    ContextWindowEdge,
    Edge,
    EvidenceRecord,
    FusionHit,
    GraphDiffResult,
    GraphStats,
    ImportResult,
    MarkdownVaultExportResult,
    MarkdownVaultImportResult,
    Node,
    NodeHistoryResult,
    NodeStoreResult,
    NodeType,
    ObservationResult,
    PrimeContextResult,
    RecentNodeStat,
    RelationType,
    ReplayHit,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
    SubgraphResult,
    TenantRecord,
    TimelineResult,
    TopicCluster,
    TopicResult,
    TranscriptRecord,
    normalize_relationship,
    utc_now,
)

SCHEMA_VERSION = 2

_UI_STATE_CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def _default_ui_state() -> dict[str, Any]:
    return {
        "positions": {},
        "zoom": 1.0,
        "viewport": {"center_x": 0, "center_y": 0},
        "groups": [],
        "collapsed_groups": [],
        "selected_nodes": [],
    }


def _parse_datetime(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_metadata(metadata: dict[str, Any] | None) -> str:
    return json.dumps(metadata or {}, sort_keys=True)


def _decode_metadata(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _decode_list(raw: Any) -> list[Any]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _decode_string_list(raw: Any) -> list[str]:
    return [str(item).strip() for item in _decode_list(raw) if str(item).strip()]


def _encode_evidence_records(records: list[EvidenceRecord]) -> str:
    return json.dumps([record.model_dump(mode="json") for record in records], sort_keys=True)


def _decode_evidence_records(raw: Any) -> list[EvidenceRecord]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, list):
        payload = raw
    else:
        return []
    if not isinstance(payload, list):
        return []
    records: list[EvidenceRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        records.append(EvidenceRecord.model_validate(item))
    return records


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


class Neo4jMemoryGraph:
    """Neo4j-backed graph memory with the same behavior as the SQLite backend."""

    def __init__(
        self,
        *,
        uri: str,
        username: str,
        password: str,
        database: str | None,
        embedding_model: Any,
        tenant_id: str = "local-default",
        dedup_similarity_threshold: float = 0.97,
        dedup_same_label_threshold: float = 0.9,
        export_dir: str | Path | None = None,
        api_key_environment: str = "test",
        _driver: Any | None = None,
        _owns_driver: bool = True,
    ) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Neo4j backend requested but the neo4j package is not installed. "
                'Install it with `pip install -e ".[neo4j]"`.'
            ) from exc

        self._driver = _driver or GraphDatabase.driver(uri, auth=(username, password))
        self._owns_driver = _owns_driver
        self._uri = uri
        self._username = username
        self._password = password
        self.database = database or None
        self.embedding_model = embedding_model
        self.tenant_id = tenant_id.strip() or "local-default"
        self.dedup_similarity_threshold = dedup_similarity_threshold
        self.dedup_same_label_threshold = dedup_same_label_threshold
        self.export_dir = Path(export_dir).expanduser() if export_dir is not None else Path.cwd() / "exports"
        self.api_key_environment = api_key_environment
        self._lock = threading.RLock()
        self._initialize_database()

    def _session(self):
        return self._driver.session(database=self.database) if self.database else self._driver.session()

    def _initialize_database(self) -> None:
        with self._lock, self._session() as session:
            session.run(
                """
                CREATE CONSTRAINT waggle_node_id IF NOT EXISTS
                FOR (n:MemoryNode) REQUIRE n.id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_edge_id IF NOT EXISTS
                FOR ()-[r:MEMORY_EDGE]-() REQUIRE r.id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_transcript_id IF NOT EXISTS
                FOR (t:MemoryTranscript) REQUIRE t.id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_tenant_id IF NOT EXISTS
                FOR (t:GraphTenant) REQUIRE t.tenant_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_api_key_id IF NOT EXISTS
                FOR (a:GraphApiKey) REQUIRE a.api_key_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_retention_policy_tenant IF NOT EXISTS
                FOR (p:GraphRetentionPolicy) REQUIRE p.tenant_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_retention_run_id IF NOT EXISTS
                FOR (r:GraphRetentionPruneRun) REQUIRE r.run_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_audit_event_id IF NOT EXISTS
                FOR (a:GraphAuditEvent) REQUIRE a.event_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_node_tenant_updated IF NOT EXISTS
                FOR (n:MemoryNode) ON (n.tenant_id, n.updated_at)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_node_tenant_type IF NOT EXISTS
                FOR (n:MemoryNode) ON (n.tenant_id, n.node_type)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_transcript_tenant_observed IF NOT EXISTS
                FOR (t:MemoryTranscript) ON (t.tenant_id, t.observed_at)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_transcript_tenant_session_turn IF NOT EXISTS
                FOR (t:MemoryTranscript) ON (t.tenant_id, t.session_id, t.turn_index)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_api_key_hash IF NOT EXISTS
                FOR (a:GraphApiKey) ON (a.key_hash)
                """
            ).consume()
            session.run(
                """
                MATCH (n:MemoryNode)
                WHERE n.tenant_id IS NULL
                SET n.tenant_id = $tenant_id
                """,
                tenant_id=self.tenant_id,
            ).consume()
            session.run(
                """
                MATCH ()-[r:MEMORY_EDGE]->()
                WHERE r.tenant_id IS NULL
                SET r.tenant_id = $tenant_id
                """,
                tenant_id=self.tenant_id,
            ).consume()
            self.ensure_tenant(self.tenant_id)

    def for_tenant(self, tenant_id: str) -> Neo4jMemoryGraph:
        return Neo4jMemoryGraph(
            uri=self._uri,
            username=self._username,
            password=self._password,
            database=self.database,
            embedding_model=self.embedding_model,
            tenant_id=tenant_id,
            dedup_similarity_threshold=self.dedup_similarity_threshold,
            dedup_same_label_threshold=self.dedup_same_label_threshold,
            export_dir=self.export_dir,
            api_key_environment=self.api_key_environment,
            _driver=self._driver,
            _owns_driver=False,
        )

    def ensure_tenant(self, tenant_id: str, name: str = "") -> TenantRecord:
        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id:
            raise ValidationFailure("Tenant ID cannot be empty.")
        created_at = utc_now()
        with self._lock, self._session() as session:
            record = session.run(
                """
                MERGE (t:GraphTenant {tenant_id: $tenant_id})
                ON CREATE SET t.name = $name, t.status = 'active', t.created_at = $created_at
                ON MATCH SET t.name = CASE WHEN $name <> '' THEN $name ELSE t.name END
                RETURN t.tenant_id AS tenant_id, t.name AS name, t.status AS status, t.created_at AS created_at
                """,
                tenant_id=normalized_tenant_id,
                name=name.strip(),
                created_at=created_at.isoformat(),
            ).single()
        return TenantRecord(
            tenant_id=record["tenant_id"],
            name=record["name"] or "",
            status=record["status"],
            created_at=_parse_datetime(record["created_at"]),
        )

    def _delete_label_batch(
        self,
        session: Any,
        *,
        match_query: str,
        delete_query: str,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        deleted = 0
        limit = max(1, int(batch_size))
        while True:
            rows = session.run(
                match_query,
                tenant_id=self.tenant_id,
                cutoff=cutoff.isoformat(),
                limit=limit,
            )
            ids = [record["id"] for record in rows]
            if not ids:
                return deleted
            session.run(delete_query, ids=ids).consume()
            deleted += len(ids)

    def _delete_old_export_files(self, *, cutoff: datetime) -> int:
        if not self.export_dir.exists():
            return 0
        deleted = 0
        cutoff_ts = cutoff.timestamp()
        for path in self.export_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink(missing_ok=True)
                    deleted += 1
            except FileNotFoundError:
                continue
        return deleted

    def _store_retention_run(self, run: RetentionPruneRunRecord, *, session: Any | None = None) -> None:
        owns_session = session is None
        active_session = session or self._session()
        try:
            active_session.run(
                """
                MERGE (r:GraphRetentionPruneRun {run_id: $run_id})
                SET r.tenant_id = $tenant_id,
                    r.status = $status,
                    r.cutoff = $cutoff,
                    r.started_at = $started_at,
                    r.completed_at = $completed_at,
                    r.deleted_nodes = $deleted_nodes,
                    r.deleted_edges = $deleted_edges,
                    r.deleted_transcripts = $deleted_transcripts,
                    r.deleted_context_windows = $deleted_context_windows,
                    r.deleted_context_window_edges = $deleted_context_window_edges,
                    r.deleted_exports = $deleted_exports,
                    r.duration_ms = $duration_ms,
                    r.error_message = $error_message
                """,
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                status=run.status,
                cutoff=run.cutoff.isoformat(),
                started_at=run.started_at.isoformat(),
                completed_at=run.completed_at.isoformat() if run.completed_at else None,
                deleted_nodes=run.deleted_nodes,
                deleted_edges=run.deleted_edges,
                deleted_transcripts=run.deleted_transcripts,
                deleted_context_windows=run.deleted_context_windows,
                deleted_context_window_edges=run.deleted_context_window_edges,
                deleted_exports=run.deleted_exports,
                duration_ms=run.duration_ms,
                error_message=run.error_message,
            ).consume()
        finally:
            if owns_session:
                active_session.close()

    def emit_audit_event(
        self,
        *,
        event_type: str,
        actor_type: str = "system",
        actor_id: str = "",
        api_key_id: str = "",
        resource_type: str = "",
        resource_id: str = "",
        action: str = "",
        status: str = "success",
        ip_address: str = "",
        user_agent: str = "",
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        session: Any | None = None,
    ) -> AuditEventRecord:
        event = AuditEventRecord(
            tenant_id=self.tenant_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            api_key_id=api_key_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action or event_type,
            status=status,
            ip_address=ip_address,
            user_agent=user_agent,
            created_at=created_at or utc_now(),
            metadata=metadata or {},
        )
        owns_session = session is None
        active_session = session or self._session()
        try:
            active_session.run(
                """
                CREATE (a:GraphAuditEvent {
                    event_id: $event_id,
                    tenant_id: $tenant_id,
                    event_type: $event_type,
                    actor_type: $actor_type,
                    actor_id: $actor_id,
                    api_key_id: $api_key_id,
                    resource_type: $resource_type,
                    resource_id: $resource_id,
                    action: $action,
                    status: $status,
                    ip_address: $ip_address,
                    user_agent: $user_agent,
                    created_at: $created_at,
                    metadata: $metadata
                })
                """,
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                api_key_id=event.api_key_id,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                action=event.action,
                status=event.status,
                ip_address=event.ip_address,
                user_agent=event.user_agent,
                created_at=event.created_at.isoformat(),
                metadata=event.metadata,
            ).consume()
        finally:
            if owns_session:
                active_session.close()
        return event

    def list_audit_events(
        self,
        *,
        limit: int = 100,
        event_type: str = "",
        actor_id: str = "",
        resource_id: str = "",
        resource_type: str = "",
        status: str = "",
    ) -> list[AuditEventRecord]:
        predicates = ["a.tenant_id = $tenant_id"]
        params: dict[str, Any] = {"tenant_id": self.tenant_id, "limit": max(1, int(limit))}
        if event_type.strip():
            predicates.append("a.event_type = $event_type")
            params["event_type"] = event_type.strip()
        if actor_id.strip():
            predicates.append("a.actor_id = $actor_id")
            params["actor_id"] = actor_id.strip()
        if resource_id.strip():
            predicates.append("a.resource_id = $resource_id")
            params["resource_id"] = resource_id.strip()
        if resource_type.strip():
            predicates.append("a.resource_type = $resource_type")
            params["resource_type"] = resource_type.strip()
        if status.strip():
            predicates.append("a.status = $status")
            params["status"] = status.strip()
        query = f"""
            MATCH (a:GraphAuditEvent)
            WHERE {" AND ".join(predicates)}
            RETURN a
            ORDER BY a.created_at DESC
            LIMIT $limit
        """
        with self._lock, self._session() as session:
            rows = [record["a"] for record in session.run(query, **params)]
        return [
            AuditEventRecord(
                event_id=props["event_id"],
                tenant_id=props["tenant_id"],
                event_type=props["event_type"],
                actor_type=props.get("actor_type") or "system",
                actor_id=props.get("actor_id") or "",
                api_key_id=props.get("api_key_id") or "",
                resource_type=props.get("resource_type") or "",
                resource_id=props.get("resource_id") or "",
                action=props.get("action") or "",
                status=props.get("status") or "success",
                ip_address=props.get("ip_address") or "",
                user_agent=props.get("user_agent") or "",
                created_at=_parse_datetime(props["created_at"]),
                metadata=props.get("metadata") or {},
            )
            for props in rows
        ]

    def create_api_key(
        self,
        tenant_id: str,
        name: str = "",
        *,
        expires_at: datetime | None = None,
        created_by: str = "",
        scopes: list[str] | None = None,
    ) -> ApiKeyCreateResult:
        tenant = self.ensure_tenant(tenant_id)
        raw_api_key = generate_api_key(self.api_key_environment)
        record = ApiKeyRecord(
            api_key_id=str(uuid4()),
            tenant_id=tenant.tenant_id,
            key_hash=hash_api_key(raw_api_key),
            prefix=api_key_prefix(raw_api_key),
            name=name.strip(),
            expires_at=expires_at,
            created_by=created_by.strip(),
            scopes=scopes,
        )
        with self._lock, self._session() as session:
            session.run(
                """
                MATCH (t:GraphTenant {tenant_id: $tenant_id})
                CREATE (a:GraphApiKey {
                    api_key_id: $api_key_id,
                    tenant_id: $tenant_id,
                    key_hash: $key_hash,
                    prefix: $prefix,
                    name: $name,
                    status: $status,
                    created_at: $created_at,
                    expires_at: $expires_at,
                    revoked_at: $revoked_at,
                    last_used_at: $last_used_at,
                    created_by: $created_by,
                    scopes: $scopes
                })
                CREATE (t)-[:OWNS_API_KEY]->(a)
                """,
                api_key_id=record.api_key_id,
                tenant_id=record.tenant_id,
                key_hash=record.key_hash,
                prefix=record.prefix,
                name=record.name,
                status=record.status,
                created_at=record.created_at.isoformat(),
                expires_at=record.expires_at.isoformat() if record.expires_at else None,
                revoked_at=None,
                last_used_at=None,
                created_by=record.created_by,
                scopes=record.scopes,
            ).consume()
        return ApiKeyCreateResult(record=record, raw_api_key=raw_api_key)

    def list_api_keys(self, tenant_id: str) -> list[ApiKeyRecord]:
        with self._lock, self._session() as session:
            rows = session.run(
                """
                MATCH (a:GraphApiKey {tenant_id: $tenant_id})
                RETURN a.api_key_id AS api_key_id, a.tenant_id AS tenant_id, a.key_hash AS key_hash,
                       a.prefix AS prefix, a.name AS name, a.status AS status, a.created_at AS created_at,
                       a.expires_at AS expires_at, a.revoked_at AS revoked_at, a.last_used_at AS last_used_at,
                       a.created_by AS created_by, a.scopes AS scopes
                ORDER BY a.created_at DESC
                """,
                tenant_id=tenant_id,
            )
            return [
                ApiKeyRecord(
                    api_key_id=row["api_key_id"],
                    tenant_id=row["tenant_id"],
                    key_hash=row["key_hash"],
                    prefix=row["prefix"] or "",
                    name=row["name"] or "",
                    status=row["status"],
                    created_at=_parse_datetime(row["created_at"]),
                    expires_at=_parse_datetime(row["expires_at"]) if row["expires_at"] else None,
                    revoked_at=_parse_datetime(row["revoked_at"]) if row["revoked_at"] else None,
                    last_used_at=_parse_datetime(row["last_used_at"]) if row["last_used_at"] else None,
                    created_by=row["created_by"] or "",
                    scopes=row["scopes"] or [],
                )
                for row in rows
            ]

    def revoke_api_key(self, api_key_id: str) -> None:
        with self._lock, self._session() as session:
            session.run(
                """
                MATCH (a:GraphApiKey {api_key_id: $api_key_id})
                SET a.status = 'revoked', a.revoked_at = $revoked_at
                """,
                api_key_id=api_key_id,
                revoked_at=utc_now().isoformat(),
            ).consume()

    def get_retention_policy(
        self,
        *,
        default_enabled: bool = False,
        default_retention_days: int = 90,
        default_prune_interval_hours: int = 24,
    ) -> RetentionPolicyRecord:
        now = utc_now()
        with self._lock, self._session() as session:
            record = session.run(
                """
                MERGE (p:GraphRetentionPolicy {tenant_id: $tenant_id})
                ON CREATE SET
                    p.enabled = $enabled,
                    p.retention_days = $retention_days,
                    p.prune_interval_hours = $prune_interval_hours,
                    p.created_at = $created_at,
                    p.updated_at = $updated_at
                RETURN p
                """,
                tenant_id=self.tenant_id,
                enabled=bool(default_enabled),
                retention_days=int(default_retention_days),
                prune_interval_hours=int(default_prune_interval_hours),
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            ).single()
        props = record["p"]
        return RetentionPolicyRecord(
            tenant_id=props["tenant_id"],
            enabled=bool(props["enabled"]),
            retention_days=int(props["retention_days"]),
            prune_interval_hours=int(props["prune_interval_hours"]),
            last_pruned_at=_parse_datetime(props["last_pruned_at"]) if props.get("last_pruned_at") else None,
            created_at=_parse_datetime(props["created_at"]),
            updated_at=_parse_datetime(props["updated_at"]),
        )

    def update_retention_policy(
        self,
        *,
        enabled: bool | None = None,
        retention_days: int | None = None,
        prune_interval_hours: int | None = None,
        default_enabled: bool = False,
        default_retention_days: int = 90,
        default_prune_interval_hours: int = 24,
    ) -> RetentionPolicyRecord:
        current = self.get_retention_policy(
            default_enabled=default_enabled,
            default_retention_days=default_retention_days,
            default_prune_interval_hours=default_prune_interval_hours,
        )
        next_enabled = current.enabled if enabled is None else bool(enabled)
        next_retention_days = current.retention_days if retention_days is None else int(retention_days)
        next_prune_interval_hours = (
            current.prune_interval_hours if prune_interval_hours is None else int(prune_interval_hours)
        )
        if next_retention_days < 1:
            raise ValidationFailure("Retention days must be at least 1.")
        if next_prune_interval_hours < 1:
            raise ValidationFailure("Prune interval hours must be at least 1.")
        updated_at = utc_now()
        with self._lock, self._session() as session:
            session.run(
                """
                MATCH (p:GraphRetentionPolicy {tenant_id: $tenant_id})
                SET p.enabled = $enabled,
                    p.retention_days = $retention_days,
                    p.prune_interval_hours = $prune_interval_hours,
                    p.updated_at = $updated_at
                """,
                tenant_id=self.tenant_id,
                enabled=next_enabled,
                retention_days=next_retention_days,
                prune_interval_hours=next_prune_interval_hours,
                updated_at=updated_at.isoformat(),
            ).consume()
        return self.get_retention_policy(
            default_enabled=default_enabled,
            default_retention_days=default_retention_days,
            default_prune_interval_hours=default_prune_interval_hours,
        )

    def list_retention_runs(self, *, limit: int = 20) -> list[RetentionPruneRunRecord]:
        with self._lock, self._session() as session:
            rows = session.run(
                """
                MATCH (r:GraphRetentionPruneRun {tenant_id: $tenant_id})
                RETURN r
                ORDER BY r.started_at DESC
                LIMIT $limit
                """,
                tenant_id=self.tenant_id,
                limit=max(1, int(limit)),
            )
            records = [record["r"] for record in rows]
        return [
            RetentionPruneRunRecord(
                run_id=props["run_id"],
                tenant_id=props["tenant_id"],
                status=props["status"],
                cutoff=_parse_datetime(props["cutoff"]),
                started_at=_parse_datetime(props["started_at"]),
                completed_at=_parse_datetime(props["completed_at"]) if props.get("completed_at") else None,
                deleted_nodes=int(props.get("deleted_nodes") or 0),
                deleted_edges=int(props.get("deleted_edges") or 0),
                deleted_transcripts=int(props.get("deleted_transcripts") or 0),
                deleted_context_windows=int(props.get("deleted_context_windows") or 0),
                deleted_context_window_edges=int(props.get("deleted_context_window_edges") or 0),
                deleted_exports=int(props.get("deleted_exports") or 0),
                duration_ms=int(props.get("duration_ms") or 0),
                error_message=props.get("error_message") or "",
            )
            for props in records
        ]

    def prune_retention(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = 1000,
        default_enabled: bool = False,
        default_retention_days: int = 90,
        default_prune_interval_hours: int = 24,
    ) -> RetentionPruneRunRecord:
        policy = self.get_retention_policy(
            default_enabled=default_enabled,
            default_retention_days=default_retention_days,
            default_prune_interval_hours=default_prune_interval_hours,
        )
        current_time = now or utc_now()
        cutoff = current_time - timedelta(days=policy.retention_days)
        started_at = utc_now()
        run = RetentionPruneRunRecord(
            tenant_id=self.tenant_id,
            status="completed",
            cutoff=cutoff,
            started_at=started_at,
        )
        if not policy.enabled:
            run.status = "skipped"
            run.completed_at = started_at
            self._store_retention_run(run)
            return run

        try:
            with self._lock, self._session() as session:
                run.deleted_context_window_edges = self._delete_label_batch(
                    session,
                    match_query="""
                        MATCH ()-[r:CONTEXT_WINDOW_EDGE]->()
                        WHERE r.tenant_id = $tenant_id AND r.created_at < $cutoff
                        RETURN r.id AS id
                        LIMIT $limit
                    """,
                    delete_query="""
                        MATCH ()-[r:CONTEXT_WINDOW_EDGE]->()
                        WHERE r.id IN $ids
                        DELETE r
                    """,
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                run.deleted_edges = self._delete_label_batch(
                    session,
                    match_query="""
                        MATCH ()-[r:MEMORY_EDGE]->()
                        WHERE r.tenant_id = $tenant_id AND r.created_at < $cutoff
                        RETURN r.id AS id
                        LIMIT $limit
                    """,
                    delete_query="""
                        MATCH ()-[r:MEMORY_EDGE]->()
                        WHERE r.id IN $ids
                        DELETE r
                    """,
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                run.deleted_nodes = self._delete_label_batch(
                    session,
                    match_query="""
                        MATCH (n:MemoryNode {tenant_id: $tenant_id})
                        WHERE n.created_at < $cutoff
                        RETURN n.id AS id
                        LIMIT $limit
                    """,
                    delete_query="""
                        MATCH (n:MemoryNode)
                        WHERE n.id IN $ids
                        DETACH DELETE n
                    """,
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                run.deleted_transcripts = self._delete_label_batch(
                    session,
                    match_query="""
                        MATCH (t:MemoryTranscript {tenant_id: $tenant_id})
                        WHERE t.observed_at < $cutoff
                        RETURN t.id AS id
                        LIMIT $limit
                    """,
                    delete_query="""
                        MATCH (t:MemoryTranscript)
                        WHERE t.id IN $ids
                        DELETE t
                    """,
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                run.deleted_context_windows = self._delete_label_batch(
                    session,
                    match_query="""
                        MATCH (w:ContextWindow {tenant_id: $tenant_id})
                        WHERE w.created_at < $cutoff
                        RETURN w.id AS id
                        LIMIT $limit
                    """,
                    delete_query="""
                        MATCH (w:ContextWindow)
                        WHERE w.id IN $ids
                        DETACH DELETE w
                    """,
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                run.deleted_exports = self._delete_old_export_files(cutoff=cutoff)
                completed_at = utc_now()
                run.completed_at = completed_at
                run.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
                session.run(
                    """
                    MATCH (p:GraphRetentionPolicy {tenant_id: $tenant_id})
                    SET p.last_pruned_at = $last_pruned_at, p.updated_at = $updated_at
                    """,
                    tenant_id=self.tenant_id,
                    last_pruned_at=completed_at.isoformat(),
                    updated_at=completed_at.isoformat(),
                ).consume()
                self._store_retention_run(run, session=session)
        except Exception as exc:
            completed_at = utc_now()
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = completed_at
            run.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
            self._store_retention_run(run)
            raise
        return run

    def authenticate_api_key(self, raw_api_key: str) -> ApiKeyRecord:
        key_hash = hash_api_key(raw_api_key)
        with self._lock, self._session() as session:
            row = session.run(
                """
                MATCH (a:GraphApiKey {key_hash: $key_hash})
                RETURN a.api_key_id AS api_key_id, a.tenant_id AS tenant_id, a.key_hash AS key_hash,
                       a.prefix AS prefix, a.name AS name, a.status AS status, a.created_at AS created_at,
                       a.expires_at AS expires_at, a.revoked_at AS revoked_at, a.last_used_at AS last_used_at,
                       a.created_by AS created_by, a.scopes AS scopes
                LIMIT 1
                """,
                key_hash=key_hash,
            ).single()
            if row is None or not verify_api_key(raw_api_key, row["key_hash"]):
                raise AuthenticationError("Invalid API key.")
            if row["status"] != "active":
                raise AuthenticationError("Invalid API key.")
            expires_at = _parse_datetime(row["expires_at"]) if row["expires_at"] else None
            if expires_at is not None and expires_at <= utc_now():
                raise AuthenticationError("API key expired.")
            session.run(
                """
                MATCH (a:GraphApiKey {api_key_id: $api_key_id})
                SET a.last_used_at = $last_used_at
                """,
                api_key_id=row["api_key_id"],
                last_used_at=utc_now().isoformat(),
            ).consume()
        return ApiKeyRecord(
            api_key_id=row["api_key_id"],
            tenant_id=row["tenant_id"],
            key_hash=row["key_hash"],
            prefix=row["prefix"] or "",
            name=row["name"] or "",
            status=row["status"],
            created_at=_parse_datetime(row["created_at"]),
            expires_at=expires_at,
            revoked_at=_parse_datetime(row["revoked_at"]) if row["revoked_at"] else None,
            last_used_at=utc_now(),
            created_by=row["created_by"] or "",
            scopes=row["scopes"] or [],
        )

    def add_node(
        self,
        *,
        node_id: str | None = None,
        label: str,
        content: str,
        node_type: NodeType,
        tags: list[str] | None = None,
        source_prompt: str = "",
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        evidence_records: list[EvidenceRecord] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> NodeStoreResult:
        node_kwargs: dict[str, Any] = {}
        if node_id is not None and str(node_id).strip():
            node_kwargs["id"] = str(node_id).strip()
        node = Node(
            **node_kwargs,
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            label=label,
            content=content,
            node_type=node_type,
            tags=tags or [],
            source_prompt=source_prompt,
            evidence_records=evidence_records or [],
            valid_from=valid_from,
            valid_to=valid_to,
        )
        embedding = self.embedding_model.embed(node.content)

        with self._lock, self._session() as session:
            existing = [
                self._node_from_props(record["n"])
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id, node_type: $node_type})
                    RETURN n
                    """,
                    tenant_id=self.tenant_id,
                    node_type=node.node_type.value,
                )
            ]
            duplicate = self._find_duplicate_node(existing_nodes=existing, node=node, embedding=embedding)
            if duplicate is not None:
                existing_node, dedup_reason, similarity = duplicate
                merged_node = self._merge_duplicate_node(
                    session,
                    existing_node=existing_node,
                    incoming_node=node,
                )
                return NodeStoreResult(
                    node=merged_node,
                    created=False,
                    dedup_reason=dedup_reason,
                    similarity=similarity,
                )

            session.run(
                """
                CREATE (n:MemoryNode {
                    id: $id,
                    tenant_id: $tenant_id,
                    agent_id: $agent_id,
                    project: $project,
                    session_id: $session_id,
                    label: $label,
                    content: $content,
                    node_type: $node_type,
                    tags: $tags,
                    embedding: $embedding,
                    source_prompt: $source_prompt,
                    evidence_records: $evidence_records,
                    valid_from: $valid_from,
                    valid_to: $valid_to,
                    created_at: $created_at,
                    updated_at: $updated_at,
                    access_count: $access_count
                })
                """,
                **self._node_create_params(node=node, embedding=embedding),
            ).consume()
            conflicts = self._register_conflicts(session, node)
        return NodeStoreResult(node=node, created=True, conflicts=conflicts)

    def add_edge(
        self,
        *,
        edge_id: str | None = None,
        source_id: str,
        target_id: str,
        relationship: str | RelationType,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        edge_kwargs: dict[str, Any] = {}
        if edge_id is not None and str(edge_id).strip():
            edge_kwargs["id"] = str(edge_id).strip()
        edge = Edge(
            **edge_kwargs,
            tenant_id=self.tenant_id,
            source_id=source_id,
            target_id=target_id,
            relationship=relationship,
            weight=weight,
            metadata=metadata or {},
        )
        with self._lock, self._session() as session:
            self._require_node(session, edge.source_id)
            self._require_node(session, edge.target_id)
            existing_edge = self._find_existing_edge(
                session,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
            )
            if existing_edge is not None:
                return existing_edge
            session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
                MATCH (target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                CREATE (source)-[:MEMORY_EDGE {
                    id: $id,
                    tenant_id: $tenant_id,
                    relationship: $relationship,
                    weight: $weight,
                    metadata: $metadata,
                    created_at: $created_at
                }]->(target)
                """,
                id=edge.id,
                tenant_id=self.tenant_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
                weight=edge.weight,
                metadata=_encode_metadata(edge.metadata),
                created_at=edge.created_at.isoformat(),
            ).consume()
        return edge

    def get_node(self, node_id: str) -> Node:
        with self._lock, self._session() as session:
            node = self._fetch_node(session, node_id)
            if node is None:
                raise ValueError(f"Node not found: {node_id}")
            return node

    def get_ui_state(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        key = (self.tenant_id, project.strip(), agent_id.strip(), session_id.strip())
        with self._lock, self._session() as session:
            record = session.run(
                """
                MATCH (ui:GraphUIState {
                    tenant_id: $tenant_id,
                    project: $project,
                    agent_id: $agent_id,
                    session_id: $session_id
                })
                RETURN ui.positions AS positions,
                       ui.zoom AS zoom,
                       ui.viewport AS viewport,
                       ui.groups AS groups,
                       ui.collapsed_groups AS collapsed_groups,
                       ui.selected_nodes AS selected_nodes
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                project=project.strip(),
                agent_id=agent_id.strip(),
                session_id=session_id.strip(),
            ).single()
        if record is None:
            return json.loads(json.dumps(_UI_STATE_CACHE.get(key, _default_ui_state())))
        value = {
            "positions": _decode_metadata(record["positions"]),
            "zoom": float(record["zoom"]) if record["zoom"] is not None else 1.0,
            "viewport": _decode_metadata(record["viewport"]) or {"center_x": 0, "center_y": 0},
            "groups": _decode_list(record["groups"]),
            "collapsed_groups": _decode_string_list(record["collapsed_groups"]),
            "selected_nodes": _decode_string_list(record["selected_nodes"]),
        }
        _UI_STATE_CACHE[key] = json.loads(json.dumps(value))
        return json.loads(json.dumps(value))

    def save_ui_state(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        positions: dict[str, Any] | None = None,
        zoom: float | None = None,
        viewport: dict[str, Any] | None = None,
        groups: list[dict[str, Any]] | None = None,
        collapsed_groups: list[str] | None = None,
        selected_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        key = (self.tenant_id, project.strip(), agent_id.strip(), session_id.strip())
        current = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        merged = {
            "positions": positions if positions is not None else current["positions"],
            "zoom": float(zoom if zoom is not None else current["zoom"]),
            "viewport": viewport if viewport is not None else current["viewport"],
            "groups": groups if groups is not None else current["groups"],
            "collapsed_groups": collapsed_groups if collapsed_groups is not None else current["collapsed_groups"],
            "selected_nodes": selected_nodes if selected_nodes is not None else current["selected_nodes"],
        }
        with self._lock, self._session() as session:
            session.run(
                """
                MERGE (ui:GraphUIState {
                    tenant_id: $tenant_id,
                    project: $project,
                    agent_id: $agent_id,
                    session_id: $session_id
                })
                SET ui.positions = $positions,
                    ui.zoom = $zoom,
                    ui.viewport = $viewport,
                    ui.groups = $groups,
                    ui.collapsed_groups = $collapsed_groups,
                    ui.selected_nodes = $selected_nodes,
                    ui.updated_at = $updated_at
                """,
                tenant_id=self.tenant_id,
                project=project.strip(),
                agent_id=agent_id.strip(),
                session_id=session_id.strip(),
                positions=_encode_metadata(merged["positions"]),
                zoom=merged["zoom"],
                viewport=_encode_metadata(merged["viewport"]),
                groups=json.dumps(merged["groups"], sort_keys=True),
                collapsed_groups=json.dumps(merged["collapsed_groups"], sort_keys=True),
                selected_nodes=json.dumps(merged["selected_nodes"], sort_keys=True),
                updated_at=utc_now().isoformat(),
            ).consume()
        _UI_STATE_CACHE[key] = json.loads(json.dumps(merged))
        return merged

    def ensure_repo(self, project: str = "") -> str:
        del project
        return "default"

    def ensure_context_window(self, session_id: str = "", repo_id: str | None = None) -> str:
        del repo_id
        return session_id.strip() or "default"

    def resolve_window_context(self, project: str | None = None, session_id: str | None = None) -> tuple[str, str]:
        return (self.ensure_repo(project or "default"), self.ensure_context_window(session_id or "default", "default"))

    def list_context_windows(
        self,
        *,
        project: str = "",
        status: str = "",
        limit: int = 20,
    ) -> list[ContextWindow]:
        del project, status, limit
        return []

    def get_context_window(self, window_id: str) -> ContextWindow:
        return ContextWindow(
            id=window_id,
            tenant_id=self.tenant_id,
            repo_id="default",
            session_id=window_id,
            title="",
            status="active",
            node_count=0,
        )

    def get_context_window_edges(self, window_id: str) -> list[ContextWindowEdge]:
        del window_id
        return []

    def close_context_window(self, window_id: str) -> ContextWindow:
        window = self.get_context_window(window_id)
        window.status = "closed"
        window.closed_at = utc_now()
        window.updated_at = window.closed_at
        return window

    def get_repo_windows(
        self,
        repo_id: str,
        *,
        exclude: str | None = None,
        include_archived: bool = False,
    ) -> list[ContextWindow]:
        del repo_id, exclude, include_archived
        return []

    def get_window_nodes(self, window_id: str, node_types: list[NodeType] | None = None) -> list[Node]:
        del window_id, node_types
        return []

    def compute_window_embedding(self, window_id: str) -> np.ndarray | None:
        del window_id
        return None

    def get_window_embedding(self, window_id: str) -> np.ndarray | None:
        del window_id
        return None

    def extract_window_entities(self, window_id: str) -> list[dict[str, str]]:
        del window_id
        return []

    def derive_context_window_edges(self, window_id: str, repo_id: str) -> list[ContextWindowEdge]:
        del window_id, repo_id
        return []

    def get_nodes_without_window(self) -> list[Node]:
        return []

    def assign_nodes_to_window(self, node_ids: list[str], window_id: str) -> int:
        del node_ids, window_id
        return 0

    def list_repos(self) -> list[dict[str, Any]]:
        return []

    def update_window_node_count(self, window_id: str) -> int:
        del window_id
        return 0

    def mark_window_embedding_stale(self, window_id: str) -> None:
        del window_id

    def tiered_query(
        self,
        *,
        query: str,
        project: str = "",
        repo_id: str | None = None,
        max_nodes: int = 20,
        max_depth: int = 2,
        top_k_windows: int | None = None,
    ) -> SubgraphResult:
        del repo_id, top_k_windows
        result = self.query(query=query, project=project, max_nodes=max_nodes, max_depth=max_depth)
        result.retrieval_mode = "flat_fallback"
        return result

    def debug_retrieval(
        self,
        *,
        query: str,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 10,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        result = self.query(
            query=query,
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
        return {
            "query": query.strip(),
            "repo_id": "default",
            "project": project,
            "agent_id": agent_id,
            "session_id": session_id,
            "retrieval_mode": "flat_fallback",
            "embedding_preview": [],
            "windows_evaluated": 0,
            "all_windows": [],
            "selected_windows": [],
            "flat_top_nodes": [
                {
                    "node_id": node.id,
                    "label": node.label,
                    "node_type": node.node_type.value,
                    "project": node.project,
                    "session_id": node.session_id,
                    "context_window_id": node.context_window_id,
                    "similarity_score": node.similarity_score,
                    "recency_score": node.recency_score,
                    "edge_score": node.edge_score,
                    "final_score": node.final_score,
                    "updated_at": node.updated_at.isoformat(),
                }
                for node in result.nodes[:max_nodes]
            ],
            "tiered_top_nodes": [],
            "tiered_result_mode": "flat_fallback",
        }

    def aggregate(
        self,
        *,
        query: str = "",
        node_types: list[str] | None = None,
        tags: list[str] | None = None,
        max_nodes: int = 1000,
        max_depth: int = 1,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> SubgraphResult:
        query_text = query.strip()
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._session() as session:
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            total_nodes = len(node_records)
            if total_nodes == 0:
                return SubgraphResult(query=query_text, total_nodes_in_graph=0)

            target_types = {t.lower() for t in node_types} if node_types else None
            target_tags = {t.lower() for t in tags} if tags else None

            candidates: list[Node] = []
            embeddings_by_id: dict[str, np.ndarray] = {}
            for props in node_records:
                node = self._node_from_props(props)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                    continue
                if target_types and node.node_type.value.lower() not in target_types:
                    continue
                if target_tags:
                    node_tags = {t.lower() for t in node.tags}
                    if not any(tag in node_tags for tag in target_tags):
                        continue
                candidates.append(node)
                if props.get("embedding"):
                    embeddings_by_id[node.id] = np.array(props["embedding"], dtype=np.float32)

            if not candidates:
                return SubgraphResult(query=query_text, total_nodes_in_graph=total_nodes)

            if query_text:
                query_embedding = self.embedding_model.embed(query_text)
                scored_candidates = []
                for node in candidates:
                    similarity = 0.0
                    emb = embeddings_by_id.get(node.id)
                    if emb is not None:
                        similarity = max(self.embedding_model.cosine_similarity(query_embedding, emb), 0.0)
                    scored_candidates.append((similarity, node))
                scored_candidates.sort(key=lambda item: item[0], reverse=True)
                selected_nodes = [node for _, node in scored_candidates[:max_nodes]]
            else:
                candidates.sort(key=lambda node: node.updated_at.timestamp(), reverse=True)
                selected_nodes = candidates[:max_nodes]

            if max_depth > 0 and selected_nodes:
                selected_ids = [node.id for node in selected_nodes]
                graph = self._load_graph(session)
                expanded_depths = self._expand_node_depths(graph, selected_ids, max_depth)
                expanded_ids = set(expanded_depths.keys())
                missing_ids = expanded_ids - {node.id for node in selected_nodes}
                if missing_ids:
                    for props in node_records:
                        if props["id"] in missing_ids:
                            selected_nodes.append(self._node_from_props(props))

            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(session, selected_ids)
            self._increment_access_counts(session, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="aggregate",
                query=query_text,
                total_nodes_in_graph=total_nodes,
            )

    def query(
        self,
        *,
        query: str,
        max_nodes: int = 20,
        max_depth: int = 2,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        retrieval_mode: str = "hybrid",
    ) -> SubgraphResult:
        query_text = query.strip()
        if not query_text:
            raise ValueError("Query cannot be empty.")
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")
        normalized_mode = {"replay": "verbatim", "fusion": "hybrid"}.get(
            retrieval_mode.strip().lower(), retrieval_mode.strip().lower()
        )
        # Accept "hybrid_no_rerank" as alias for "hybrid" (reranking is configurable via HybridRetrievalConfig)
        if normalized_mode == "hybrid_no_rerank":
            normalized_mode = "hybrid"
        if normalized_mode not in {"graph", "verbatim", "hybrid"}:
            raise ValidationFailure(
                "retrieval_mode must be one of: graph, verbatim, hybrid, hybrid_no_rerank (benchmark modes: graph_only, verbatim_only)."
            )

        graph_result = (
            self._query_graph_only(
                query=query_text,
                max_nodes=max_nodes,
                max_depth=max_depth,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            if normalized_mode in {"graph", "hybrid"}
            else None
        )
        replay_hits = (
            self._query_replay_hits(
                query=query_text,
                max_hits=max_nodes,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            if normalized_mode in {"verbatim", "hybrid"}
            else []
        )
        if normalized_mode == "graph":
            graph_result.retrieval_mode = "graph"
            return graph_result
        if normalized_mode == "verbatim":
            return SubgraphResult(
                replay_hits=replay_hits,
                retrieval_mode="verbatim",
                query=query_text,
                total_nodes_in_graph=graph_result.total_nodes_in_graph if graph_result is not None else 0,
            )
        fusion_hits = self._build_fusion_hits(graph_result or SubgraphResult(query=query_text), replay_hits)
        return SubgraphResult(
            nodes=graph_result.nodes if graph_result is not None else [],
            edges=graph_result.edges if graph_result is not None else [],
            replay_hits=replay_hits,
            fusion_hits=fusion_hits[:max_nodes],
            retrieval_mode="hybrid",
            query=query_text,
            total_nodes_in_graph=graph_result.total_nodes_in_graph if graph_result is not None else 0,
        )

    def _query_graph_only(
        self,
        *,
        query: str,
        max_nodes: int,
        max_depth: int,
        agent_id: str,
        project: str,
        session_id: str,
    ) -> SubgraphResult:
        with self._lock, self._session() as session:
            temporal_hints = infer_temporal_hints(query)
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            total_nodes = len(node_records)
            if total_nodes == 0:
                return SubgraphResult(query=query, total_nodes_in_graph=0)

            nodes_by_id = {
                props["id"]: node
                for props in node_records
                for node in [self._node_from_props(props)]
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            }
            if not nodes_by_id:
                return SubgraphResult(query=query, total_nodes_in_graph=total_nodes)
            embeddings_by_id = {
                props["id"]: np.array(props.get("embedding") or [], dtype=np.float32)
                for props in node_records
                if props.get("embedding") and props["id"] in nodes_by_id
            }

            query_embedding = self.embedding_model.embed(query)
            similarity_by_id = {
                node_id: max(self.embedding_model.cosine_similarity(query_embedding, embedding), 0.0)
                for node_id, embedding in embeddings_by_id.items()
            }
            lexical_by_id = {
                node_id: lexical_overlap(query, node.label, node.content) for node_id, node in nodes_by_id.items()
            }

            seed_count = min(total_nodes, max(1, max_nodes // 2))
            seed_candidates = [
                (
                    node_id,
                    (0.72 * similarity_by_id.get(node_id, 0.0)) + (0.28 * lexical_by_id.get(node_id, 0.0)),
                    self._seed_temporal_order(nodes_by_id[node_id], temporal_hints),
                )
                for node_id in nodes_by_id
            ]
            if temporal_hints.recency_mode in {"latest", "oldest"}:
                ranked_seed_ids = [
                    item[0]
                    for item in sorted(
                        seed_candidates,
                        key=lambda item: (item[2], -item[1], nodes_by_id[item[0]].label.lower()),
                    )[:seed_count]
                ]
            else:
                ranked_seed_ids = [
                    item[0]
                    for item in sorted(
                        seed_candidates,
                        key=lambda item: (-item[1], item[2], nodes_by_id[item[0]].label.lower()),
                    )[:seed_count]
                ]

            graph = self._load_graph(session)
            expanded_depths = self._expand_node_depths(graph, ranked_seed_ids, max_depth)
            candidate_nodes = [nodes_by_id[node_id] for node_id in expanded_depths]
            temporal_candidates = [node for node in candidate_nodes if within_time_window(node, temporal_hints)]
            if temporal_candidates:
                candidate_nodes = temporal_candidates
            max_access = max((node.access_count for node in candidate_nodes), default=0)
            degree_by_id = dict(graph.degree(expanded_depths.keys()))
            max_degree = max(degree_by_id.values(), default=0)
            scored_nodes = self._sort_scored_nodes(
                candidate_nodes,
                temporal_hints=temporal_hints,
                similarity_by_id=similarity_by_id,
                lexical_by_id=lexical_by_id,
                degree_by_id=degree_by_id,
                max_access=max_access,
                max_degree=max_degree,
                max_depth=max_depth,
                expanded_depths=expanded_depths,
            )
            selected_nodes = scored_nodes[:max_nodes]
            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(session, selected_ids)
            self._increment_access_counts(session, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="graph",
                query=query,
                total_nodes_in_graph=total_nodes,
            )

    def _query_replay_hits(
        self,
        *,
        query: str,
        max_hits: int,
        agent_id: str,
        project: str,
        session_id: str,
    ) -> list[ReplayHit]:
        with self._lock, self._session() as session:
            records = list(
                session.run(
                    """
                    MATCH (t:MemoryTranscript {tenant_id: $tenant_id})
                    RETURN t
                    ORDER BY t.observed_at DESC, t.turn_index DESC
                    """,
                    tenant_id=self.tenant_id,
                )
            )
        if not records:
            return []

        rows = [self._transcript_from_props(record["t"]) for record in records]
        query_embedding = self.embedding_model.embed(query)
        temporal_hints = infer_temporal_hints(query)
        timestamps = np.asarray([row.observed_at.timestamp() for row in rows], dtype=np.float64)
        max_timestamp = float(np.max(timestamps))
        min_timestamp = float(np.min(timestamps))
        span = max(max_timestamp - min_timestamp, 1.0)
        hits: list[tuple[float, ReplayHit]] = []
        for row, raw_timestamp, record in zip(rows, timestamps, records, strict=True):
            if not self._transcript_scope_matches(row, agent_id=agent_id, project=project, session_id=session_id):
                continue
            embedding = np.asarray(record["t"].get("embedding") or [], dtype=np.float32)
            semantic_score = max(self.embedding_model.cosine_similarity(query_embedding, embedding), 0.0)
            lexical_score = lexical_overlap(query, row.role, row.transcript_text)
            temporal_score = 0.0
            if temporal_hints.recency_mode == "latest":
                temporal_score = float((raw_timestamp - min_timestamp) / span)
            elif temporal_hints.recency_mode == "oldest":
                temporal_score = float((max_timestamp - raw_timestamp) / span)
            role_score = 1.0 if row.role == "user" else 0.8
            score = (0.6 * semantic_score) + (0.2 * lexical_score) + (0.1 * temporal_score) + (0.1 * role_score)
            hits.append(
                (
                    score,
                    ReplayHit(
                        score=score,
                        session_id=row.session_id,
                        turn_index=row.turn_index,
                        role=row.role,
                        transcript_text=row.transcript_text,
                        transcript_snippet=row.transcript_text[:280],
                        observed_at=row.observed_at,
                    ),
                )
            )
        hits.sort(key=lambda item: (-item[0], -item[1].observed_at.timestamp(), item[1].turn_index))
        return [hit for _, hit in hits[:max_hits]]

    def _build_fusion_hits(
        self,
        graph_result: SubgraphResult,
        replay_hits: list[ReplayHit],
    ) -> list[FusionHit]:
        combined: dict[str, FusionHit] = {}
        graph_edge_map: dict[str, list[dict[str, Any]]] = {}
        graph_nodes_by_session = {node.session_id: node for node in graph_result.nodes if node.session_id}
        replay_by_session = {hit.session_id for hit in replay_hits if hit.session_id}
        for edge in graph_result.edges:
            payload = {
                "id": edge.id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "relationship": edge.relationship,
                "weight": edge.weight,
            }
            graph_edge_map.setdefault(edge.source_id, []).append(payload)
            graph_edge_map.setdefault(edge.target_id, []).append(payload)
        for index, node in enumerate(graph_result.nodes, start=1):
            key = f"graph:{node.id}"
            source_lane = "both" if node.session_id and node.session_id in replay_by_session else "graph"
            combined[key] = FusionHit(
                content=node.content,
                score=1.0 / (60 + index),
                source_lane=source_lane,
                graph_rank=index,
                fused_rank=index,
                node_id=node.id,
                node_type=node.node_type.value,
                edges=graph_edge_map.get(node.id, []),
                session_id=node.session_id or None,
            )
        for index, hit in enumerate(replay_hits, start=1):
            key = f"replay:{hit.session_id}:{hit.turn_index}:{hit.role}"
            matching_graph = graph_nodes_by_session.get(hit.session_id) if hit.session_id else None
            if matching_graph is not None:
                existing = combined.get(f"graph:{matching_graph.id}")
                if existing is not None:
                    existing.score += 1.0 / (60 + index)
                    existing.source_lane = "both"
                    existing.replay_rank = index
                    existing.session_id = hit.session_id
                    existing.transcript_snippet = hit.transcript_snippet
                    existing.turn_index = hit.turn_index
                    continue
                key = f"both:{matching_graph.id}:{hit.session_id}:{hit.turn_index}"
            existing = combined.get(key)
            contribution = 1.0 / (60 + index)
            if existing is None:
                combined[key] = FusionHit(
                    content=hit.transcript_text,
                    score=contribution,
                    source_lane="replay" if matching_graph is None else "both",
                    replay_rank=index,
                    fused_rank=index,
                    node_id=matching_graph.id if matching_graph is not None else None,
                    node_type=matching_graph.node_type.value if matching_graph is not None else None,
                    edges=graph_edge_map.get(matching_graph.id, []) if matching_graph is not None else None,
                    session_id=hit.session_id,
                    transcript_snippet=hit.transcript_snippet,
                    turn_index=hit.turn_index,
                )
                continue
            existing.score += contribution
            existing.source_lane = "both"
            existing.replay_rank = index
            existing.session_id = hit.session_id
            existing.transcript_snippet = hit.transcript_snippet
            existing.turn_index = hit.turn_index

        ordered = sorted(
            combined.values(),
            key=lambda hit: (-hit.score, hit.graph_rank or 10**6, hit.replay_rank or 10**6, hit.content.lower()),
        )
        for index, hit in enumerate(ordered, start=1):
            hit.fused_rank = index
        return ordered

    def get_related(self, *, node_id: str, max_depth: int = 2) -> SubgraphResult:
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._session() as session:
            self._require_node(session, node_id)
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            nodes_by_id = {props["id"]: self._node_from_props(props) for props in node_records}
            graph = self._load_graph(session)
            related_ids = list(self._expand_node_depths(graph, [node_id], max_depth))

            ordered_nodes: list[Node] = []
            seen: set[str] = set()
            for related_id in [node_id, *related_ids]:
                if related_id in seen:
                    continue
                seen.add(related_id)
                ordered_nodes.append(nodes_by_id[related_id])

            edges = self._fetch_edges_for_nodes(session, [node.id for node in ordered_nodes])
            self._increment_access_counts(session, [node.id for node in ordered_nodes])
            for node in ordered_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=ordered_nodes,
                edges=edges,
                query=f"related:{node_id}",
                total_nodes_in_graph=len(nodes_by_id),
            )


def update_node(
    self,
    *,
    node_id: str,
    content: str | None = None,
    label: str | None = None,
    tags: list[str] | None = None,
    agent_id: str | None = None,
    project: str | None = None,
    session_id: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    evidence_records: list[EvidenceRecord] | None = None,
) -> Node:
    if (
        content is None
        and label is None
        and tags is None
        and agent_id is None
        and project is None
        and session_id is None
        and valid_from is None
        and valid_to is None
        and evidence_records is None
    ):
        raise ValueError("At least one field must be provided for update.")

    with self._lock, self._session() as session:
        node = self._fetch_node(session, node_id)

        if node is None:
            raise ValueError(f"Node not found: {node_id}")

        updated_node = Node(
            id=node.id,
            tenant_id=node.tenant_id,
            agent_id=agent_id if agent_id is not None else node.agent_id,
            project=project if project is not None else node.project,
            session_id=session_id if session_id is not None else node.session_id,
            label=label if label is not None else node.label,
            content=content if content is not None else node.content,
            node_type=node.node_type,
            tags=tags if tags is not None else node.tags,
            source_prompt=node.source_prompt,
            evidence_records=evidence_records if evidence_records is not None else node.evidence_records,
            valid_from=valid_from if valid_from is not None else node.valid_from,
            valid_to=valid_to if valid_to is not None else node.valid_to,
            created_at=node.created_at,
            updated_at=utc_now(),
            access_count=node.access_count,
        )

        embedding = None
        if content is not None:
            embedding = self.embedding_model.embed(updated_node.content).astype(np.float32).tolist()

        session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $tenant_id, id: $id})
            SET n.label = $label,
                n.content = $content,
                n.tags = $tags,
                n.agent_id = $agent_id,
                n.project = $project,
                n.session_id = $session_id,
                n.valid_from = $valid_from,
                n.valid_to = $valid_to,
                n.evidence_records = $evidence_records,
                n.updated_at = $updated_at,
                n.embedding = CASE
                    WHEN $embedding IS NULL THEN n.embedding
                    ELSE $embedding
                END
            """,
            id=updated_node.id,
            tenant_id=self.tenant_id,
            label=updated_node.label,
            content=updated_node.content,
            tags=updated_node.tags,
            agent_id=updated_node.agent_id,
            project=updated_node.project,
            session_id=updated_node.session_id,
            valid_from=updated_node.valid_from.isoformat() if updated_node.valid_from else None,
            valid_to=updated_node.valid_to.isoformat() if updated_node.valid_to else None,
            evidence_records=_encode_evidence_records(updated_node.evidence_records),
            updated_at=updated_node.updated_at.isoformat(),
            embedding=embedding,
        ).consume()

        return updated_node

    def update_edge(
        self,
        *,
        edge_id: str,
        source_id: str | None = None,
        target_id: str | None = None,
        relationship: str | RelationType | None = None,
        weight: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        if source_id is None and target_id is None and relationship is None and weight is None and metadata is None:
            raise ValueError("At least one field must be provided for edge update.")

        with self._lock, self._session() as session:
            existing = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                id=edge_id,
            ).single()
            if existing is None:
                raise ValueError(f"Edge not found: {edge_id}")
            edge = Edge(
                id=existing["id"],
                tenant_id=self.tenant_id,
                source_id=existing["source_id"],
                target_id=existing["target_id"],
                relationship=existing["relationship"],
                weight=float(existing["weight"]),
                metadata=_decode_metadata(existing["metadata"]),
                created_at=_parse_datetime(existing["created_at"]),
            )
            updated_edge = Edge(
                id=edge.id,
                tenant_id=edge.tenant_id,
                source_id=source_id if source_id is not None else edge.source_id,
                target_id=target_id if target_id is not None else edge.target_id,
                relationship=relationship if relationship is not None else edge.relationship,
                weight=weight if weight is not None else edge.weight,
                metadata=metadata if metadata is not None else edge.metadata,
                created_at=edge.created_at,
            )
            self._require_node(session, updated_edge.source_id)
            self._require_node(session, updated_edge.target_id)
            session.run(
                """
                MATCH (old_source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(old_target:MemoryNode {tenant_id: $tenant_id})
                MATCH (new_source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
                MATCH (new_target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                CREATE (new_source)-[:MEMORY_EDGE {
                    id: $id,
                    tenant_id: $tenant_id,
                    relationship: $relationship,
                    weight: $weight,
                    metadata: $metadata,
                    created_at: $created_at
                }]->(new_target)
                DELETE r
                """,
                id=updated_edge.id,
                tenant_id=self.tenant_id,
                source_id=updated_edge.source_id,
                target_id=updated_edge.target_id,
                relationship=updated_edge.relationship,
                weight=updated_edge.weight,
                metadata=_encode_metadata(updated_edge.metadata),
                created_at=updated_edge.created_at.isoformat(),
            ).consume()
            return updated_edge

    def delete_edge(self, *, edge_id: str) -> Edge:
        with self._lock, self._session() as session:
            existing = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                id=edge_id,
            ).single()
            if existing is None:
                raise ValueError(f"Edge not found: {edge_id}")
            edge = Edge(
                id=existing["id"],
                tenant_id=self.tenant_id,
                source_id=existing["source_id"],
                target_id=existing["target_id"],
                relationship=existing["relationship"],
                weight=float(existing["weight"]),
                metadata=_decode_metadata(existing["metadata"]),
                created_at=_parse_datetime(existing["created_at"]),
            )
            session.run(
                """
                MATCH (:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(:MemoryNode {tenant_id: $tenant_id})
                DELETE r
                """,
                tenant_id=self.tenant_id,
                id=edge_id,
            ).consume()
            return edge

    def delete_node(self, *, node_id: str) -> Node:
        with self._lock, self._session() as session:
            node = self._fetch_node(session, node_id)
            if node is None:
                raise ValueError(f"Node not found: {node_id}")
            session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id, id: $id})
                DETACH DELETE n
                """,
                tenant_id=self.tenant_id,
                id=node_id,
            ).consume()
            return node

    def list_recent_nodes(
        self,
        limit: int = 10,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> list[Node]:
        with self._lock, self._session() as session:
            selected: list[Node] = []
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                RETURN n
                ORDER BY n.updated_at DESC, n.created_at DESC
                """,
                tenant_id=self.tenant_id,
            ):
                node = self._node_from_props(record["n"])
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                    continue
                selected.append(node)
                if len(selected) >= max(1, limit):
                    break
            return selected

    def list_context_scopes(self) -> ContextScopeResult:
        with self._lock, self._session() as session:
            nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
        return ContextScopeResult(
            agent_ids=sorted({node.agent_id for node in nodes if node.agent_id}),
            projects=sorted({node.project for node in nodes if node.project}),
            session_ids=sorted({node.session_id for node in nodes if node.session_id}),
        )

    def get_stats(self) -> GraphStats:
        with self._lock, self._session() as session:
            total_nodes = session.run(
                "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN count(n) AS count",
                tenant_id=self.tenant_id,
            ).single()["count"]
            total_edges = session.run(
                "MATCH ()-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->() RETURN count(r) AS count",
                tenant_id=self.tenant_id,
            ).single()["count"]
            if int(total_nodes) == 0:
                return GraphStats(
                    total_nodes=0,
                    total_edges=int(total_edges),
                    node_type_breakdown={node_type.value: 0 for node_type in NodeType},
                )

            counts = {node_type.value: 0 for node_type in NodeType}
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                RETURN n.node_type AS node_type, count(n) AS count
                """,
                tenant_id=self.tenant_id,
            ):
                counts[record["node_type"]] = record["count"]

            most_connected_nodes = [
                ConnectedNodeStat(
                    id=record["id"],
                    label=record["label"],
                    node_type=NodeType(record["node_type"]),
                    connection_count=record["connection_count"],
                )
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id})
                    OPTIONAL MATCH (n)-[r:MEMORY_EDGE {tenant_id: $tenant_id}]-()
                    WITH n, count(r) AS connection_count
                    RETURN n.id AS id, n.label AS label, n.node_type AS node_type,
                           connection_count AS connection_count, n.updated_at AS updated_at
                    ORDER BY connection_count DESC, updated_at DESC
                    LIMIT 5
                    """,
                    tenant_id=self.tenant_id,
                )
            ]
            most_recent_nodes = [
                RecentNodeStat(
                    id=record["id"],
                    label=record["label"],
                    node_type=NodeType(record["node_type"]),
                    updated_at=_parse_datetime(record["updated_at"]),
                )
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id})
                    RETURN n.id AS id, n.label AS label, n.node_type AS node_type, n.updated_at AS updated_at
                    ORDER BY n.updated_at DESC, n.created_at DESC
                    LIMIT 5
                    """,
                    tenant_id=self.tenant_id,
                )
            ]
            return GraphStats(
                total_nodes=int(total_nodes),
                total_edges=int(total_edges),
                node_type_breakdown=counts,
                most_connected_nodes=most_connected_nodes,
                most_recent_nodes=most_recent_nodes,
            )

    def export_graph_html(
        self,
        *,
        output_path: str | Path | None = None,
        include_physics: bool = True,
    ) -> Path:
        try:
            from pyvis.network import Network
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyvis is not installed. Install the project dependencies again.") from exc

        with self._lock, self._session() as session:
            nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            edges = [
                Edge(
                    id=record["id"],
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at ASC
                    """,
                    tenant_id=self.tenant_id,
                )
            ]

        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-{timestamp}.html"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)

        network = Network(
            height="800px",
            width="100%",
            directed=True,
            bgcolor="#0f172a",
            font_color="#e2e8f0",
        )
        network.barnes_hut()
        if not include_physics:
            network.toggle_physics(False)

        palette = {
            NodeType.FACT: "#38bdf8",
            NodeType.ENTITY: "#34d399",
            NodeType.CONCEPT: "#fbbf24",
            NodeType.PREFERENCE: "#fb7185",
            NodeType.DECISION: "#c084fc",
            NodeType.QUESTION: "#f97316",
            NodeType.NOTE: "#94a3b8",
        }
        for node in nodes:
            title_lines = [
                f"<b>{node.label}</b>",
                f"Type: {node.node_type.value}",
                f"Created: {node.created_at.isoformat()}",
                f"Updated: {node.updated_at.isoformat()}",
                f"Access Count: {node.access_count}",
                "",
                node.content,
            ]
            if node.tags:
                title_lines.insert(4, f"Tags: {', '.join(node.tags)}")
            network.add_node(
                node.id,
                label=node.label,
                title="<br>".join(title_lines),
                color=palette[node.node_type],
                shape="dot",
                size=18 + min(node.access_count, 8) * 2,
            )
        for edge in edges:
            network.add_edge(
                edge.source_id,
                edge.target_id,
                label=edge.relationship,
                title=f"weight={edge.weight}",
                value=max(edge.weight, 0.1),
                arrows="to",
            )
        destination.write_text(network.generate_html(notebook=False), encoding="utf-8")
        return destination

    def export_window_graph_html(
        self,
        *,
        project: str = "",
        output_path: str | Path | None = None,
        include_physics: bool = True,
    ) -> Path:
        del project, include_physics
        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-window-graph-{timestamp}.html"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "<!doctype html><html><body><p>Neo4j context-window graph visualization is not implemented yet.</p></body></html>",
            encoding="utf-8",
        )
        return destination

    def export_graph_backup(self, *, output_path: str | Path | None = None) -> BackupResult:
        with self._lock, self._session() as session:
            snapshot = self._build_backup_snapshot(session)

        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-backup-{timestamp}.json"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)

        destination.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return BackupResult(
            output_path=str(destination),
            tenant_id=self.tenant_id,
            schema_version=SCHEMA_VERSION,
            node_count=len(snapshot["nodes"]),
            edge_count=len(snapshot["edges"]),
        )

    def export_abhi(
        self,
        *,
        output_path: str | Path | None = None,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        include_embeddings: bool = False,
        passphrase: str = "",
    ) -> AbhiExportResult:
        with self._lock, self._session() as session:
            snapshot = self._build_backup_snapshot(session, include_embeddings=include_embeddings)
        snapshot["ui"] = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        filtered = filter_snapshot_by_scope(snapshot, project=project, agent_id=agent_id, session_id=session_id)
        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-memory-{timestamp}.abhi"
        else:
            destination = Path(output_path).expanduser()
        return write_abhi_document(filtered, output_path=destination, passphrase=passphrase)

    def get_graph_snapshot(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._session() as session:
            snapshot = self._build_backup_snapshot(session)
        filtered = filter_snapshot_by_scope(snapshot, project=project, agent_id=agent_id, session_id=session_id)
        filtered["ui"] = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        return filtered

    def export_context_bundle(
        self,
        *,
        mode: str = "prime",
        query: str = "",
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 25,
        max_depth: int = 2,
        retrieval_mode: str = "graph",
        format: str = "both",
        output_path: str | Path | None = None,
        include_edges: bool = True,
        include_timestamps: bool = True,
        include_source_prompt: bool = False,
        audience: str = "llm",
    ) -> ContextBundleExportResult:
        normalized_mode = mode.strip().lower()
        normalized_format = format.strip().lower()
        normalized_audience = audience.strip().lower()
        normalized_retrieval_mode = {"replay": "verbatim", "fusion": "hybrid"}.get(
            retrieval_mode.strip().lower(), retrieval_mode.strip().lower()
        )
        if normalized_mode not in {"prime", "query", "graph"}:
            raise ValidationFailure("mode must be one of: prime, query, graph.")
        if normalized_format not in {"markdown", "json", "both"}:
            raise ValidationFailure("format must be one of: markdown, json, both.")
        if normalized_audience not in {"llm", "human"}:
            raise ValidationFailure("audience must be one of: llm, human.")
        if normalized_retrieval_mode not in {"graph", "verbatim", "hybrid"}:
            raise ValidationFailure("retrieval_mode must be one of: graph, verbatim, hybrid.")
        if normalized_mode == "query" and not query.strip():
            raise ValidationFailure("query is required when mode='query'.")
        if normalized_mode != "query" and normalized_retrieval_mode != "graph":
            raise ValidationFailure("retrieval_mode is only supported when mode='query'.")

        replay_hits: list[ReplayHit] = []
        if normalized_mode == "prime":
            selected = self.prime_context(project=project, agent_id=agent_id, session_id=session_id)
            selected_nodes = selected.nodes[:max_nodes]
            selected_edges = selected.edges if include_edges else []
            summary = selected.summary
        elif normalized_mode == "query":
            selected = self.query(
                query=query,
                max_nodes=max_nodes,
                max_depth=max_depth,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                retrieval_mode=normalized_retrieval_mode,
            )
            selected_nodes = selected.nodes
            selected_edges = selected.edges if include_edges else []
            replay_hits = selected.replay_hits
            summary = build_query_summary(
                query=query,
                nodes=selected_nodes,
                edges=selected_edges,
                replay_hits=replay_hits,
                retrieval_mode=normalized_retrieval_mode,
            )
        else:
            with self._lock, self._session() as session:
                selected_nodes = [
                    node
                    for record in session.run(
                        "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n ORDER BY n.updated_at DESC, n.created_at DESC",
                        tenant_id=self.tenant_id,
                    )
                    for node in [self._node_from_props(record["n"])]
                    if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
                ]
                selected_edges = (
                    [
                        Edge(
                            id=record["id"],
                            source_id=record["source_id"],
                            target_id=record["target_id"],
                            relationship=record["relationship"],
                            weight=float(record["weight"]),
                            metadata=_decode_metadata(record["metadata"]),
                            created_at=_parse_datetime(record["created_at"]),
                        )
                        for record in session.run(
                            """
                        MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                        RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                               r.relationship AS relationship, r.weight AS weight,
                               r.metadata AS metadata, r.created_at AS created_at
                        ORDER BY r.created_at ASC
                        """,
                            tenant_id=self.tenant_id,
                        )
                    ]
                    if include_edges
                    else []
                )
            if include_edges:
                selected_ids = {node.id for node in selected_nodes}
                selected_edges = [
                    edge for edge in selected_edges if edge.source_id in selected_ids and edge.target_id in selected_ids
                ]
            summary = (
                f"Full graph export for tenant '{self.tenant_id}' with {len(selected_nodes)} nodes and "
                f"{len(selected_edges)} edges."
            )

        bundle = build_context_bundle(
            tenant_id=self.tenant_id,
            project=project,
            mode=normalized_mode,
            retrieval_mode=normalized_retrieval_mode if normalized_mode == "query" else "graph",
            audience=normalized_audience,
            query=query,
            summary=summary,
            nodes=selected_nodes,
            edges=selected_edges,
            replay_hits=replay_hits,
            stats=self.get_stats(),
        )
        return export_context_bundle_files(
            bundle,
            output_path=output_path,
            export_dir=self.export_dir,
            format=normalized_format,
            include_edges=include_edges,
            include_timestamps=include_timestamps,
            include_source_prompt=include_source_prompt,
        )

    def export_markdown_vault(
        self,
        *,
        root_path: str | Path,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> MarkdownVaultExportResult:
        root = Path(root_path).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        with self._lock, self._session() as session:
            selected_nodes = [
                node
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n ORDER BY n.updated_at DESC, n.created_at DESC",
                    tenant_id=self.tenant_id,
                )
                for node in [self._node_from_props(record["n"])]
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            ]
            selected_ids = {node.id for node in selected_nodes}
            selected_edges = [
                Edge(
                    id=record["id"],
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at ASC
                    """,
                    tenant_id=self.tenant_id,
                )
                if record["source_id"] in selected_ids and record["target_id"] in selected_ids
            ]
        node_by_id = {node.id: node for node in selected_nodes}
        files_written: list[str] = []
        for node in selected_nodes:
            project_dir = slugify(node.project or project or "default")
            node_type_dir = slugify(node.node_type.value)
            destination = root / project_dir / node_type_dir / vault_filename(node)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(render_node_document(node, selected_edges, node_by_id), encoding="utf-8")
            files_written.append(str(destination.relative_to(root)))
        return MarkdownVaultExportResult(
            root_path=str(root),
            tenant_id=self.tenant_id,
            project=project,
            node_count=len(selected_nodes),
            edge_count=len(selected_edges),
            files_written=files_written,
        )

    def import_markdown_vault(
        self,
        *,
        root_path: str | Path,
    ) -> MarkdownVaultImportResult:
        documents = iter_vault_documents(root_path)
        result = MarkdownVaultImportResult(root_path=str(Path(root_path).expanduser()), tenant_id=self.tenant_id)
        if not documents:
            return result

        nodes_by_id: dict[str, Node] = {}
        label_index: dict[str, Node] = {}
        with self._lock, self._session() as session:
            for record in session.run(
                "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                tenant_id=self.tenant_id,
            ):
                node = self._node_from_props(record["n"])
                nodes_by_id[node.id] = node
                label_index.setdefault(node.label.strip().lower(), node)

        imported_id_map: dict[str, str] = {}
        for document in documents:
            node_id = str(document.frontmatter.get("node_id", "")).strip()
            raw_type = str(document.frontmatter.get("node_type", "note") or "note")
            try:
                node_type = NodeType(raw_type)
            except ValueError:
                node_type = NodeType.NOTE
            if node_id in nodes_by_id:
                updated = self.update_node(
                    node_id=node_id,
                    label=document.label,
                    content=document.content.strip() or document.label,
                    tags=[str(tag) for tag in document.frontmatter.get("tags", []) or []],
                )
                nodes_by_id[node_id] = updated
                if node_id:
                    imported_id_map[node_id] = updated.id
                    nodes_by_id[node_id] = updated
                label_index[updated.label.strip().lower()] = updated
                result.nodes_updated += 1
            else:
                created = self.add_node(
                    label=document.label,
                    content=document.content.strip() or document.label,
                    node_type=node_type,
                    tags=[str(tag) for tag in document.frontmatter.get("tags", []) or []],
                    agent_id=str(document.frontmatter.get("agent_id", "") or ""),
                    project=str(document.frontmatter.get("project", "") or ""),
                    session_id=str(document.frontmatter.get("session_id", "") or ""),
                    evidence_records=evidence_from_lines(document.evidence_lines),
                    valid_from=self._parse_optional_datetime(document.frontmatter.get("valid_from")),
                    valid_to=self._parse_optional_datetime(document.frontmatter.get("valid_to")),
                ).node
                nodes_by_id[created.id] = created
                if node_id:
                    imported_id_map[node_id] = created.id
                    nodes_by_id[node_id] = created
                label_index[created.label.strip().lower()] = created
                result.nodes_created += 1

        for document in documents:
            source_lookup_id = str(document.frontmatter.get("node_id", "")).strip()
            source_node = nodes_by_id.get(imported_id_map.get(source_lookup_id, source_lookup_id))
            if source_node is None:
                result.conflicts.append(f"Missing source node for {document.path.name}.")
                continue
            for relation in document.relations:
                target_lookup_id = imported_id_map.get(relation.target_node_id, relation.target_node_id)
                target = nodes_by_id.get(target_lookup_id) if target_lookup_id else None
                if target is None and relation.target_label:
                    target = label_index.get(relation.target_label.strip().lower())
                if target is None and relation.target_label:
                    target = self.add_node(
                        label=relation.target_label,
                        content=f"Stub node imported from vault for {relation.target_label}.",
                        node_type=NodeType.NOTE,
                        tags=["stub", "vault-import"],
                        project=source_node.project,
                        agent_id=source_node.agent_id,
                        session_id=source_node.session_id,
                    ).node
                    nodes_by_id[target.id] = target
                    label_index[target.label.strip().lower()] = target
                    result.stub_nodes_created += 1
                if target is None:
                    result.conflicts.append(
                        f"Could not resolve relation target '{relation.target_label}' in {document.path.name}."
                    )
                    continue
                if relation.deleted:
                    if self._delete_edge_record(
                        source_id=source_node.id,
                        target_id=target.id,
                        relationship=relation.relationship,
                    ):
                        result.edges_deleted += 1
                    continue
                with self._lock, self._session() as session:
                    existing_edge = self._find_existing_edge(
                        session,
                        source_id=source_node.id,
                        target_id=target.id,
                        relationship=relation.relationship,
                    )
                if existing_edge is None:
                    self.add_edge(source_id=source_node.id, target_id=target.id, relationship=relation.relationship)
                    result.edges_created += 1
        return result

    def import_graph_backup(self, *, input_path: str | Path) -> ImportResult:
        source = Path(input_path).expanduser()
        snapshot = json.loads(source.read_text(encoding="utf-8"))

        with self._lock, self._session() as session:
            snapshot_tenant = str(snapshot.get("tenant_id") or self.tenant_id)
            result = ImportResult(
                input_path=str(source),
                tenant_id=self.tenant_id,
                schema_version=int(snapshot.get("schema_version", 1)),
            )
            for raw_node in snapshot.get("nodes", []):
                raw_node = {**raw_node, "tenant_id": raw_node.get("tenant_id") or snapshot_tenant}
                if raw_node["tenant_id"] != self.tenant_id:
                    raw_node["tenant_id"] = self.tenant_id
                if self._fetch_node(session, raw_node["id"]) is None:
                    self._insert_snapshot_node(session, raw_node)
                    result.nodes_created += 1
                else:
                    self._update_snapshot_node(session, raw_node)
                    result.nodes_updated += 1

            for raw_edge in snapshot.get("edges", []):
                raw_edge = {**raw_edge, "tenant_id": raw_edge.get("tenant_id") or snapshot_tenant}
                if raw_edge["tenant_id"] != self.tenant_id:
                    raw_edge["tenant_id"] = self.tenant_id
                if self._fetch_edge_by_id(session, raw_edge["id"]) is None:
                    self._insert_snapshot_edge(session, raw_edge)
                    result.edges_created += 1
                else:
                    self._update_snapshot_edge(session, raw_edge)
                    result.edges_updated += 1
        self.save_ui_state(
            positions=snapshot.get("ui", {}).get("positions", {}),
            zoom=snapshot.get("ui", {}).get("zoom", 1.0),
            viewport=snapshot.get("ui", {}).get("viewport", {"center_x": 0, "center_y": 0}),
            groups=snapshot.get("ui", {}).get("groups", []),
            collapsed_groups=snapshot.get("ui", {}).get("collapsed_groups", []),
            selected_nodes=snapshot.get("ui", {}).get("selected_nodes", []),
        )
        return result

    def validate_abhi(self, *, input_path: str | Path, passphrase: str = "") -> AbhiValidationResult:
        document = load_abhi_document(input_path, passphrase=passphrase)
        return validate_abhi_document(document, input_path=input_path)

    def inspect_abhi(self, *, input_path: str | Path, passphrase: str = "") -> AbhiInspectResult:
        document = load_abhi_document(input_path, passphrase=passphrase)
        return inspect_abhi_document(document, input_path=input_path)

    def diff_abhi(self, *, input_path_a: str | Path, input_path_b: str | Path) -> AbhiDiffResult:
        return diff_abhi_files(input_path_a=input_path_a, input_path_b=input_path_b)

    def query_abhi(
        self, *, input_path: str | Path, query_id: str = "", query_text: str = "", passphrase: str = ""
    ) -> AbhiQueryResult:
        return query_abhi_file(input_path=input_path, query_id=query_id, query_text=query_text, passphrase=passphrase)

    def load_abhi_chunks(
        self,
        *,
        input_path: str | Path,
        chunk_ids: list[str] | None = None,
        query_id: str = "",
        query_text: str = "",
        passphrase: str = "",
    ) -> AbhiChunkLoadResult:
        return load_abhi_chunk_file(
            input_path=input_path,
            chunk_ids=chunk_ids or [],
            query_id=query_id,
            query_text=query_text,
            passphrase=passphrase,
        )

    def merge_abhi(
        self,
        *,
        base_input_path: str | Path,
        left_input_path: str | Path,
        right_input_path: str | Path,
        output_path: str | Path,
        merge_strategy: str = "prefer_right",
    ) -> AbhiMergeResult:
        return merge_abhi_files(
            base_input_path=base_input_path,
            left_input_path=left_input_path,
            right_input_path=right_input_path,
            output_path=output_path,
            merge_strategy=merge_strategy,
        )

    def import_abhi(self, *, input_path: str | Path, passphrase: str = "") -> AbhiImportResult:
        source = Path(input_path).expanduser()
        document = load_abhi_document(source, passphrase=passphrase)
        validation = validate_abhi_document(document, input_path=source)
        if not validation.valid:
            raise ValidationFailure("Invalid .abhi file: " + "; ".join(validation.errors))
        executed_actions = dispatch_abhi_event(document, event_name="on_import", persist=False, input_path=source)
        snapshot = abhi_to_snapshot(document, fallback_tenant_id=self.tenant_id)

        with self._lock, self._session() as session:
            snapshot_tenant = str(snapshot.get("tenant_id") or self.tenant_id)
            result = AbhiImportResult(
                input_path=str(source),
                tenant_id=self.tenant_id,
                schema_version=int(snapshot.get("schema_version", 1)),
                abhi_spec_version=validation.abhi_spec_version or ABHI_SPEC_VERSION,
                hash_verified=True,
                embedding_count=validation.embedding_count,
                encrypted=bool(passphrase),
                encryption_algorithm=ABHI_ENCRYPTION_ALGORITHM if passphrase else "",
                executed_actions=executed_actions,
            )
            for raw_node in snapshot.get("nodes", []):
                raw_node = {**raw_node, "tenant_id": raw_node.get("tenant_id") or snapshot_tenant}
                if raw_node["tenant_id"] != self.tenant_id:
                    raw_node["tenant_id"] = self.tenant_id
                if self._fetch_node(session, raw_node["id"]) is None:
                    self._insert_snapshot_node(session, raw_node)
                    result.nodes_created += 1
                else:
                    self._update_snapshot_node(session, raw_node)
                    result.nodes_updated += 1

            for raw_edge in snapshot.get("edges", []):
                raw_edge = {**raw_edge, "tenant_id": raw_edge.get("tenant_id") or snapshot_tenant}
                if raw_edge["tenant_id"] != self.tenant_id:
                    raw_edge["tenant_id"] = self.tenant_id
                if self._fetch_edge_by_id(session, raw_edge["id"]) is None:
                    self._insert_snapshot_edge(session, raw_edge)
                    result.edges_created += 1
                else:
                    self._update_snapshot_edge(session, raw_edge)
                    result.edges_updated += 1
        self.save_ui_state(
            positions=snapshot.get("ui", {}).get("positions", {}),
            zoom=snapshot.get("ui", {}).get("zoom", 1.0),
            viewport=snapshot.get("ui", {}).get("viewport", {"center_x": 0, "center_y": 0}),
            groups=snapshot.get("ui", {}).get("groups", []),
            collapsed_groups=snapshot.get("ui", {}).get("collapsed_groups", []),
            selected_nodes=snapshot.get("ui", {}).get("selected_nodes", []),
        )
        return result

    def decompose_and_store(self, *, content: str, context: str = "") -> SubgraphResult:
        trimmed_content = content.strip()
        if not trimmed_content:
            raise ValueError("Content cannot be empty.")
        created_nodes: list[Node] = []
        created_ids: set[str] = set()
        context_node: Node | None = None
        if context.strip():
            context_result = self.add_node(
                label=infer_label(context),
                content=context.strip(),
                node_type=NodeType.CONCEPT,
                tags=["decomposition-context"],
                source_prompt=trimmed_content,
            )
            context_node = context_result.node
            created_nodes.append(context_node)
            created_ids.add(context_node.id)

        item_nodes: list[Node] = []
        for item in split_atomic_items(trimmed_content):
            store_result = self.add_node(
                label=infer_label(item),
                content=item,
                node_type=infer_node_type(item),
                tags=["decomposed"],
                source_prompt=context.strip() or trimmed_content,
            )
            node = store_result.node
            item_nodes.append(node)
            if node.id not in created_ids:
                created_nodes.append(node)
                created_ids.add(node.id)
            if context_node is not None:
                self.add_edge(
                    source_id=node.id,
                    target_id=context_node.id,
                    relationship=RelationType.PART_OF,
                    metadata={"origin": "decomposition"},
                )

        for index, node in enumerate(item_nodes):
            if index == 0:
                continue
            previous = item_nodes[index - 1]
            shared_tokens = tokenize_text(previous.content) & tokenize_text(node.content)
            if shared_tokens or previous.node_type == node.node_type:
                self.add_edge(
                    source_id=previous.id,
                    target_id=node.id,
                    relationship=infer_relationship(previous, node, shared_tokens=shared_tokens),
                    metadata={"origin": "decomposition"},
                )

        node_ids = [node.id for node in created_nodes]
        with self._lock, self._session() as session:
            edges = self._fetch_edges_for_nodes(session, node_ids)
            total_nodes = session.run(
                "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN count(n) AS count",
                tenant_id=self.tenant_id,
            ).single()["count"]
        return SubgraphResult(
            nodes=created_nodes,
            edges=edges,
            query=f"decomposition:{context.strip() or infer_label(trimmed_content)}",
            total_nodes_in_graph=int(total_nodes),
        )

    def get_node_history(self, *, node_id: str, max_depth: int = 2) -> NodeHistoryResult:
        node = self.get_node(node_id)
        related = self.get_related(node_id=node_id, max_depth=max_depth)
        related_nodes = [item for item in related.nodes if item.id != node_id]
        return NodeHistoryResult(node=node, related_nodes=related_nodes, edges=related.edges)

    def timeline(
        self,
        *,
        node_id: str = "",
        query: str = "",
        limit: int = 25,
        max_depth: int = 2,
        include_evidence: bool = True,
    ) -> TimelineResult:
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")
        if node_id.strip() and query.strip():
            raise ValueError("Provide either node_id or query, not both.")

        if node_id.strip():
            related = self.get_related(node_id=node_id, max_depth=max_depth)
            nodes = related.nodes
            edges = related.edges
            scope = f"node:{node_id.strip()}"
        elif query.strip():
            subgraph = self.query(query=query, max_nodes=max(limit, 10), max_depth=max_depth)
            nodes = subgraph.nodes
            edges = subgraph.edges
            scope = f"query:{query.strip()}"
        else:
            with self._lock, self._session() as session:
                nodes = self.list_recent_nodes(limit=max(limit, 10))
                edges = self._fetch_edges_for_nodes(session, [node.id for node in nodes])
            scope = "tenant"

        items = self._build_timeline_items(
            nodes=nodes,
            edges=edges,
            include_evidence=include_evidence,
            limit=limit,
        )
        return TimelineResult(scope=scope, items=items)

    def list_conflicts(
        self,
        *,
        include_resolved: bool = False,
        limit: int = 25,
    ) -> ConflictListResult:
        if limit < 1:
            raise ValueError("limit must be at least 1.")

        with self._lock, self._session() as session:
            edges = [
                Edge(
                    id=record["id"],
                    tenant_id=self.tenant_id,
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    WHERE r.relationship IN [$contradicts, $updates]
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    contradicts=RelationType.CONTRADICTS.value,
                    updates=RelationType.UPDATES.value,
                )
            ]
            entries = self._build_conflict_entries(
                session,
                edges=edges,
                include_resolved=include_resolved,
                limit=limit,
            )
        return ConflictListResult(conflicts=entries, include_resolved=include_resolved)

    def resolve_conflict(
        self,
        *,
        edge_id: str,
        resolution_note: str = "",
    ) -> ConflictEntry:
        with self._lock, self._session() as session:
            record = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $edge_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                edge_id=edge_id,
            ).single()
            if record is None:
                raise ValueError(f"Conflict edge not found: {edge_id}")
            edge = Edge(
                id=record["id"],
                tenant_id=self.tenant_id,
                source_id=record["source_id"],
                target_id=record["target_id"],
                relationship=record["relationship"],
                weight=float(record["weight"]),
                metadata=_decode_metadata(record["metadata"]),
                created_at=_parse_datetime(record["created_at"]),
            )
            if edge.relationship not in {RelationType.CONTRADICTS.value, RelationType.UPDATES.value}:
                raise ValueError("Only contradicts or updates edges can be resolved.")

            metadata = dict(edge.metadata)
            metadata["resolved"] = True
            metadata["resolved_at"] = utc_now().isoformat()
            if resolution_note.strip():
                metadata["resolution_note"] = resolution_note.strip()

            session.run(
                """
                MATCH ()-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $edge_id}]->()
                SET r.metadata = $metadata
                """,
                tenant_id=self.tenant_id,
                edge_id=edge_id,
                metadata=_encode_metadata(metadata),
            ).consume()
            updated_edge = Edge(
                id=edge.id,
                tenant_id=edge.tenant_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
                weight=edge.weight,
                metadata=metadata,
                created_at=edge.created_at,
            )
            entries = self._build_conflict_entries(
                session,
                edges=[updated_edge],
                include_resolved=True,
                limit=1,
            )
        if not entries:
            raise ValueError(f"Resolved conflict could not be loaded: {edge_id}")
        return entries[0]

    def observe_conversation(
        self,
        *,
        user_message: str,
        assistant_response: str,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> ObservationResult:
        transcript = f"user: {user_message.strip()}\nassistant: {assistant_response.strip()}".strip()
        observed_at = utc_now()
        candidates = extract_conversation_candidates(
            user_message=user_message,
            assistant_response=assistant_response,
        )

        result = ObservationResult()
        with self._lock, self._session() as session:
            next_turn_index = self._next_transcript_turn_index(session, session_id=session_id)
            turns = [
                ("user", user_message.strip(), next_turn_index),
                ("assistant", assistant_response.strip(), next_turn_index + 1),
            ]
            for role, text, turn_index in turns:
                if not text:
                    continue
                self._store_transcript_record(
                    session,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                    observed_at=observed_at,
                    turn_index=turn_index,
                    role=role,
                    transcript_text=text,
                )
        for candidate in candidates:
            candidate_tags = list(candidate.get("tags", []))
            speaker_tag = next((tag for tag in candidate_tags if str(tag).startswith("speaker:")), "")
            speaker = speaker_tag.split(":", 1)[1] if ":" in speaker_tag else "user"
            turn_index = next_turn_index if speaker == "user" else next_turn_index + 1
            evidence = build_observation_evidence(
                transcript=transcript,
                source_text=str(candidate["content"]),
                speaker=speaker,
                turn_index=turn_index,
                observed_at=observed_at,
                session_id=session_id,
            )
            store_result = self.add_node(
                label=str(candidate["label"]),
                content=str(candidate["content"]),
                node_type=candidate["node_type"],
                tags=candidate_tags,
                source_prompt=transcript,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                evidence_records=[evidence],
                valid_from=observed_at,
            )
            result.stored_nodes.append(store_result.node)
            if store_result.created:
                result.created_count += 1
            else:
                result.reused_count += 1
            for conflict in store_result.conflicts:
                if conflict.other_node_id not in {item.other_node_id for item in result.conflicts}:
                    result.conflicts.append(conflict)
        return result

    def graph_diff(self, *, since: str = "24h") -> GraphDiffResult:
        cutoff = parse_since_value(since).isoformat()
        with self._lock, self._session() as session:
            added_nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id})
                    WHERE n.created_at >= $cutoff
                    RETURN n
                    ORDER BY n.created_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    cutoff=cutoff,
                )
            ]
            updated_nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id})
                    WHERE n.updated_at >= $cutoff AND n.created_at < $cutoff
                    RETURN n
                    ORDER BY n.updated_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    cutoff=cutoff,
                )
            ]
            created_edges = [
                Edge(
                    id=record["id"],
                    tenant_id=self.tenant_id,
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    WHERE r.created_at >= $cutoff
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    cutoff=cutoff,
                )
            ]
        return GraphDiffResult(
            since=since,
            added_nodes=added_nodes,
            updated_nodes=updated_nodes,
            created_edges=created_edges,
            contradiction_edges=[edge for edge in created_edges if edge.relationship == RelationType.CONTRADICTS],
        )

    def prime_context(self, *, project: str = "", agent_id: str = "", session_id: str = "") -> PrimeContextResult:
        with self._lock, self._session() as session:
            total_nodes = int(
                session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN count(n) AS count",
                    tenant_id=self.tenant_id,
                ).single()["count"]
            )
            if total_nodes == 0:
                return PrimeContextResult(project=project, summary="No stored memory is available yet.")

            selected_ids: list[str] = []
            selected_ids.extend(
                self._most_connected_node_ids(
                    session,
                    limit=5,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                )
            )
            selected_ids.extend(
                node.id
                for node in self.list_recent_nodes(
                    limit=5,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                )
            )
            if project.strip():
                selected_ids.extend(
                    self._find_project_node_ids(
                        session,
                        project=project,
                        agent_id=agent_id,
                        session_id=session_id,
                        limit=8,
                    )
                )
            unique_ids = list(dict.fromkeys(selected_ids))
            nodes = [
                node
                for node in self._fetch_nodes_by_ids(session, unique_ids)
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            ]
            edges = self._fetch_edges_for_nodes(session, [node.id for node in nodes])

        summary = (
            f"Prime context for '{project}' with {len(nodes)} nodes selected from {total_nodes} total nodes."
            if project.strip()
            else f"Prime context with {len(nodes)} nodes selected from {total_nodes} total nodes."
        )
        return PrimeContextResult(
            project=project,
            summary=summary,
            nodes=nodes,
            edges=edges,
            total_nodes_in_graph=total_nodes,
        )

    def get_topics(self) -> TopicResult:
        with self._lock, self._session() as session:
            nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            if not nodes:
                return TopicResult(clusters=[], total_clusters=0)
            graph = self._load_graph(session).to_undirected()
            partition = self._build_topic_partition(graph, nodes)

        nodes_by_id = {node.id: node for node in nodes}
        clusters_by_id: dict[int, list[Node]] = {}
        for node_id, cluster_id in partition.items():
            clusters_by_id.setdefault(int(cluster_id), []).append(nodes_by_id[node_id])

        clusters: list[TopicCluster] = []
        for cluster_id, cluster_nodes in sorted(
            clusters_by_id.items(),
            key=lambda item: (-len(item[1]), item[0]),
        ):
            label, top_tags = summarize_topic(cluster_nodes)
            ordered_nodes = sorted(
                cluster_nodes,
                key=lambda node: (-node.access_count, -node.updated_at.timestamp(), node.label.lower()),
            )
            clusters.append(
                TopicCluster(
                    cluster_id=cluster_id,
                    label=label,
                    node_count=len(cluster_nodes),
                    top_tags=top_tags,
                    nodes=ordered_nodes,
                )
            )
        return TopicResult(clusters=clusters, total_clusters=len(clusters))

    def close(self) -> None:
        if self._owns_driver:
            self._driver.close()

    def _require_node(self, session: Any, node_id: str) -> None:
        if self._fetch_node(session, node_id) is None:
            raise ValueError(f"Node not found: {node_id}")

    def _fetch_node(self, session: Any, node_id: str) -> Node | None:
        record = session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $tenant_id, id: $id})
            RETURN n
            """,
            tenant_id=self.tenant_id,
            id=node_id,
        ).single()
        if record is None:
            return None
        return self._node_from_props(record["n"])

    def _node_create_params(self, *, node: Node, embedding: np.ndarray) -> dict[str, Any]:
        return {
            "id": node.id,
            "tenant_id": node.tenant_id,
            "agent_id": node.agent_id,
            "project": node.project,
            "session_id": node.session_id,
            "label": node.label,
            "content": node.content,
            "node_type": node.node_type.value,
            "tags": node.tags,
            "embedding": embedding.astype(np.float32).tolist(),
            "source_prompt": node.source_prompt,
            "evidence_records": _encode_evidence_records(node.evidence_records),
            "valid_from": node.valid_from.isoformat() if node.valid_from is not None else None,
            "valid_to": node.valid_to.isoformat() if node.valid_to is not None else None,
            "created_at": node.created_at.isoformat(),
            "updated_at": node.updated_at.isoformat(),
            "access_count": node.access_count,
        }

    def _node_from_props(self, props: Any) -> Node:
        return Node(
            id=props["id"],
            tenant_id=props.get("tenant_id") or self.tenant_id,
            agent_id=props.get("agent_id") or "",
            project=props.get("project") or "",
            session_id=props.get("session_id") or "",
            label=props["label"],
            content=props["content"],
            node_type=NodeType(props["node_type"]),
            tags=list(props.get("tags") or []),
            source_prompt=props.get("source_prompt") or "",
            evidence_records=_decode_evidence_records(props.get("evidence_records")),
            valid_from=_parse_datetime(props["valid_from"]) if props.get("valid_from") else None,
            valid_to=_parse_datetime(props["valid_to"]) if props.get("valid_to") else None,
            created_at=_parse_datetime(props["created_at"]),
            updated_at=_parse_datetime(props["updated_at"]),
            access_count=int(props.get("access_count") or 0),
        )

    def _transcript_from_props(self, props: Any) -> TranscriptRecord:
        return TranscriptRecord(
            id=props["id"],
            tenant_id=props.get("tenant_id") or self.tenant_id,
            agent_id=props.get("agent_id") or "",
            project=props.get("project") or "",
            session_id=props.get("session_id") or "",
            observed_at=_parse_datetime(props["observed_at"]),
            turn_index=int(props.get("turn_index") or 0),
            role=props.get("role") or "",
            transcript_text=props["transcript_text"],
            metadata=_decode_metadata(props.get("metadata")),
        )

    def _transcript_scope_matches(
        self,
        record: TranscriptRecord,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> bool:
        normalized_agent = agent_id.strip().lower()
        normalized_project = project.strip().lower()
        normalized_session = session_id.strip().lower()
        if normalized_agent and record.agent_id.strip().lower() != normalized_agent:
            return False
        if normalized_project and record.project.strip().lower() != normalized_project:
            return False
        return not (normalized_session and record.session_id.strip().lower() != normalized_session)

    def list_transcript_records(
        self,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        limit: int = 200,
    ) -> list[TranscriptRecord]:
        filters = ["t.tenant_id = $tenant_id"]
        params: dict[str, Any] = {"tenant_id": self.tenant_id, "limit": max(1, int(limit))}
        if project.strip():
            filters.append("t.project = $project")
            params["project"] = project.strip()
        if session_id.strip():
            filters.append("t.session_id = $session_id")
            params["session_id"] = session_id.strip()
        elif agent_id.strip():
            filters.append("t.agent_id = $agent_id")
            params["agent_id"] = agent_id.strip()
        with self._lock, self._session() as session:
            records = session.run(
                f"""
                MATCH (t:MemoryTranscript)
                WHERE {" AND ".join(filters)}
                RETURN t
                ORDER BY t.observed_at ASC, t.turn_index ASC
                LIMIT $limit
                """,
                **params,
            )
            return [self._transcript_from_props(record["t"]) for record in records]

    def search_transcript_records(
        self,
        *,
        query: str,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        limit: int = 25,
    ) -> list[ReplayHit]:
        query_text = query.strip()
        if not query_text:
            return []
        return self._query_replay_hits(
            query=self._expand_query_aliases(query_text),
            max_hits=max(1, int(limit)),
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )

    def _next_transcript_turn_index(self, session: Any, *, session_id: str) -> int:
        record = session.run(
            """
            MATCH (t:MemoryTranscript {tenant_id: $tenant_id, session_id: $session_id})
            RETURN COALESCE(max(t.turn_index), -1) AS max_turn_index
            """,
            tenant_id=self.tenant_id,
            session_id=session_id,
        ).single()
        return int(record["max_turn_index"] or -1) + 1

    def _store_transcript_record(
        self,
        session: Any,
        *,
        agent_id: str,
        project: str,
        session_id: str,
        observed_at: datetime,
        turn_index: int,
        role: str,
        transcript_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> TranscriptRecord:
        record = TranscriptRecord(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            observed_at=observed_at,
            turn_index=turn_index,
            role=role,
            transcript_text=transcript_text,
            metadata=metadata or {},
        )
        session.run(
            """
            CREATE (t:MemoryTranscript {
                id: $id,
                tenant_id: $tenant_id,
                agent_id: $agent_id,
                project: $project,
                session_id: $session_id,
                observed_at: $observed_at,
                turn_index: $turn_index,
                role: $role,
                transcript_text: $transcript_text,
                embedding: $embedding,
                metadata: $metadata
            })
            """,
            id=record.id,
            tenant_id=record.tenant_id,
            agent_id=record.agent_id,
            project=record.project,
            session_id=record.session_id,
            observed_at=record.observed_at.isoformat(),
            turn_index=record.turn_index,
            role=record.role,
            transcript_text=record.transcript_text,
            embedding=self.embedding_model.embed(record.transcript_text).astype(np.float32).tolist(),
            metadata=_encode_metadata(record.metadata),
        ).consume()
        return record

    def _parse_optional_datetime(self, raw: Any) -> datetime | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        try:
            return _parse_datetime(str(raw))
        except ValueError:
            return None

    def _find_duplicate_node(
        self,
        *,
        existing_nodes: list[Node],
        node: Node,
        embedding: np.ndarray,
    ) -> tuple[Node, str, float | None] | None:
        normalized_label = normalize_text(node.label)
        normalized_content = normalize_text(node.content)
        type_threshold = type_aware_dedup_threshold(
            node.node_type,
            default=self.dedup_similarity_threshold,
        )
        best_match: tuple[Node, float] | None = None

        for existing_node in existing_nodes:
            if not _scope_matches(
                existing_node,
                agent_id=node.agent_id,
                project=node.project,
                session_id=node.session_id,
            ):
                continue
            if not compatible_node_types(node.node_type, existing_node.node_type):
                continue
            existing_label = normalize_text(existing_node.label)
            existing_content = normalize_text(existing_node.content)

            node_entity = extract_choice_entity(node.content)
            existing_entity = extract_choice_entity(existing_node.content)
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[1] == existing_entity[1]
                and node_entity[0] != existing_entity[0]
                and not describes_rejected_or_limited_option(node.content)
                and not describes_rejected_or_limited_option(existing_node.content)
            ):
                continue
            if contains_conflicting_numbers(node.content, existing_node.content) and (
                node_entity is None or existing_entity is None or node_entity[0] == existing_entity[0]
            ):
                continue
            if contains_conflicting_months(node.content, existing_node.content):
                continue

            if normalized_content == existing_content:
                return existing_node, "exact_content", 1.0
            if len(normalized_content) >= 10 and len(existing_content) >= 10:
                if normalized_content in existing_content or existing_content in normalized_content:
                    return existing_node, "content_substring", 0.98

            existing_embedding = self.embedding_model.embed(existing_node.content)
            similarity = self.embedding_model.cosine_similarity(embedding, existing_embedding)
            label_score = label_similarity(node.label, existing_node.label)
            acronym_match = is_acronym_match(node.label, existing_node.label)
            if normalized_label == existing_label and similarity >= self.dedup_same_label_threshold:
                return existing_node, "same_label_high_similarity", similarity
            if acronym_match and similarity >= max(self.dedup_same_label_threshold - 0.25, 0.55):
                return existing_node, "acronym_entity_match", similarity
            if label_score >= 0.92 and similarity >= max(self.dedup_same_label_threshold - 0.2, 0.6):
                return existing_node, "label_entity_match", similarity
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[0] == existing_entity[0]
                and similarity >= 0.60
            ):
                return existing_node, "same_entity_merge", similarity

            jaccard = content_token_jaccard(node.content, existing_node.content)
            boosted_threshold = max(type_threshold - 0.05, 0.70)
            if jaccard >= 0.35 and similarity >= boosted_threshold:
                return existing_node, "jaccard_boosted_similarity", similarity
            if node_entity is None and existing_entity is None:
                paraphrase_score = paraphrase_dedup_score(
                    semantic_similarity=similarity,
                    lexical_overlap=jaccard,
                )
                paraphrase_threshold = max(type_threshold - 0.10, 0.72)
                if paraphrase_score >= paraphrase_threshold:
                    return existing_node, "entityless_paraphrase", paraphrase_score

            concept_overlap = canonical_concept_overlap(node.content, existing_node.content)
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[0] == existing_entity[0]
                and concept_overlap >= 0.30
            ):
                return existing_node, "same_entity_concept_overlap", concept_overlap
            if concept_overlap >= 0.50 and similarity >= 0.35:
                return existing_node, "canonical_concept_overlap", concept_overlap

            if similarity >= self.dedup_similarity_threshold:
                if best_match is None or similarity > best_match[1]:
                    best_match = (existing_node, similarity)

        if best_match is None:
            return None
        return best_match[0], "high_similarity", best_match[1]

    def _merge_duplicate_node(self, session: Any, *, existing_node: Node, incoming_node: Node) -> Node:
        merged_tags = list(dict.fromkeys([*existing_node.tags, *incoming_node.tags]))
        updated_source_prompt = existing_node.source_prompt or incoming_node.source_prompt
        merged_evidence = merge_evidence_records(existing_node.evidence_records, incoming_node.evidence_records)
        merged_valid_from, merged_valid_to = merge_validity_windows(
            existing_node.valid_from,
            incoming_node.valid_from,
            existing_node.valid_to,
            incoming_node.valid_to,
        )
        updated_at = utc_now()
        session.run(
            """
            MATCH (n:MemoryNode {id: $id})
            WHERE n.tenant_id = $tenant_id
            SET n.tags = $tags,
                n.source_prompt = $source_prompt,
                n.evidence_records = $evidence_records,
                n.valid_from = $valid_from,
                n.valid_to = $valid_to,
                n.updated_at = $updated_at
            """,
            id=existing_node.id,
            tenant_id=self.tenant_id,
            tags=merged_tags,
            source_prompt=updated_source_prompt,
            evidence_records=_encode_evidence_records(merged_evidence),
            valid_from=merged_valid_from.isoformat() if merged_valid_from is not None else None,
            valid_to=merged_valid_to.isoformat() if merged_valid_to is not None else None,
            updated_at=updated_at.isoformat(),
        ).consume()
        return Node(
            id=existing_node.id,
            tenant_id=existing_node.tenant_id,
            label=existing_node.label,
            content=existing_node.content,
            node_type=existing_node.node_type,
            tags=merged_tags,
            source_prompt=updated_source_prompt,
            evidence_records=merged_evidence,
            valid_from=merged_valid_from,
            valid_to=merged_valid_to,
            created_at=existing_node.created_at,
            updated_at=updated_at,
            access_count=existing_node.access_count,
        )

    def _register_conflicts(self, session: Any, node: Node) -> list[ConflictRecord]:
        if node.node_type not in {NodeType.PREFERENCE, NodeType.DECISION}:
            return []
        existing_nodes = [
            self._node_from_props(record["n"])
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                WHERE n.id <> $node_id
                RETURN n
                """,
                tenant_id=self.tenant_id,
                node_id=node.id,
            )
        ]
        conflicts: list[ConflictRecord] = []
        for existing_node in existing_nodes:
            reason = detect_conflict_reason(existing_node, node)
            if reason is None:
                continue
            if (
                self._find_existing_edge(
                    session,
                    source_id=node.id,
                    target_id=existing_node.id,
                    relationship=RelationType.CONTRADICTS,
                )
                is None
            ):
                edge = Edge(
                    tenant_id=self.tenant_id,
                    source_id=node.id,
                    target_id=existing_node.id,
                    relationship=RelationType.CONTRADICTS,
                    metadata={"origin": "auto-conflict", "reason": reason},
                )
                session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
                    MATCH (target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                    CREATE (source)-[:MEMORY_EDGE {
                        id: $id,
                        tenant_id: $tenant_id,
                        relationship: $relationship,
                        weight: $weight,
                        metadata: $metadata,
                        created_at: $created_at
                    }]->(target)
                    """,
                    id=edge.id,
                    tenant_id=self.tenant_id,
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    relationship=edge.relationship,
                    weight=edge.weight,
                    metadata=_encode_metadata(edge.metadata),
                    created_at=edge.created_at.isoformat(),
                ).consume()
            conflicts.append(
                ConflictRecord(
                    other_node_id=existing_node.id,
                    other_node_label=existing_node.label,
                    reason=reason,
                )
            )
        return conflicts

    def _load_graph(self, session: Any) -> nx.DiGraph:
        graph = nx.DiGraph()
        for record in session.run(
            "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n.id AS id",
            tenant_id=self.tenant_id,
        ):
            graph.add_node(record["id"])
        for record in session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id})-[:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
            RETURN source.id AS source_id, target.id AS target_id
            """,
            tenant_id=self.tenant_id,
        ):
            graph.add_edge(record["source_id"], record["target_id"])
        return graph

    def _fetch_nodes_by_ids(self, session: Any, node_ids: list[str]) -> list[Node]:
        if not node_ids:
            return []
        rows = {
            record["n"]["id"]: self._node_from_props(record["n"])
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                WHERE n.id IN $node_ids
                RETURN n
                """,
                tenant_id=self.tenant_id,
                node_ids=node_ids,
            )
        }
        return [rows[node_id] for node_id in node_ids if node_id in rows]

    def _build_timeline_items(
        self,
        *,
        nodes: list[Node],
        edges: list[Edge],
        include_evidence: bool,
        limit: int,
    ) -> list[ContextTimelineItem]:
        items: list[ContextTimelineItem] = []
        for node in nodes:
            items.append(
                ContextTimelineItem(
                    kind="node_created",
                    timestamp=node.created_at,
                    label=node.label,
                    summary=node.content,
                    node_id=node.id,
                )
            )
            if node.updated_at != node.created_at:
                items.append(
                    ContextTimelineItem(
                        kind="node_updated",
                        timestamp=node.updated_at,
                        label=node.label,
                        summary=node.content,
                        node_id=node.id,
                    )
                )
            if include_evidence:
                for record in node.evidence_records:
                    items.append(
                        ContextTimelineItem(
                            kind="evidence",
                            timestamp=record.observed_at,
                            label=node.label,
                            summary=f"{record.source_role or 'unknown'} turn {record.turn_index}: {record.source_text or node.content}",
                            node_id=node.id,
                        )
                    )
        node_by_id = {node.id: node for node in nodes}
        for edge in edges:
            source_label = node_by_id.get(edge.source_id).label if edge.source_id in node_by_id else edge.source_id[:8]
            target_label = node_by_id.get(edge.target_id).label if edge.target_id in node_by_id else edge.target_id[:8]
            items.append(
                ContextTimelineItem(
                    kind=f"edge_{edge.relationship}",
                    timestamp=edge.created_at,
                    label=f"{source_label} -> {target_label}",
                    summary=edge.relationship,
                    edge_id=edge.id,
                )
            )
        return sorted(
            items,
            key=lambda item: (item.timestamp, item.kind, item.label),
            reverse=True,
        )[:limit]

    def _build_conflict_entries(
        self,
        session: Any,
        *,
        edges: list[Edge],
        include_resolved: bool,
        limit: int,
    ) -> list[ConflictEntry]:
        node_ids = list(dict.fromkeys([edge.source_id for edge in edges] + [edge.target_id for edge in edges]))
        nodes_by_id = {node.id: node for node in self._fetch_nodes_by_ids(session, node_ids)}
        entries: list[ConflictEntry] = []
        for edge in edges:
            resolved, resolution_note, resolved_at = self._conflict_resolution_state(edge)
            if resolved and not include_resolved:
                continue
            source_node = nodes_by_id.get(edge.source_id)
            target_node = nodes_by_id.get(edge.target_id)
            if source_node is None or target_node is None:
                continue
            entries.append(
                ConflictEntry(
                    edge=edge,
                    source_node=source_node,
                    target_node=target_node,
                    resolved=resolved,
                    resolution_note=resolution_note,
                    resolved_at=resolved_at,
                )
            )
            if len(entries) >= limit:
                break
        return entries

    def _conflict_resolution_state(self, edge: Edge) -> tuple[bool, str, datetime | None]:
        metadata = edge.metadata or {}
        resolved = bool(metadata.get("resolved"))
        resolution_note = str(metadata.get("resolution_note", "") or "")
        resolved_at_raw = metadata.get("resolved_at")
        resolved_at = _parse_datetime(resolved_at_raw) if resolved_at_raw else None
        return resolved, resolution_note, resolved_at

    def _temporal_sort_value(self, node: Node, hints: Any) -> float:
        if hints.recency_mode == "latest":
            return -node.updated_at.timestamp()
        if hints.recency_mode == "oldest":
            return node.created_at.timestamp()
        return -node.updated_at.timestamp()

    def _seed_temporal_order(self, node: Node, hints: Any) -> float:
        if hints.recency_mode == "latest":
            return -node.updated_at.timestamp()
        if hints.recency_mode == "oldest":
            return node.created_at.timestamp()
        return 0.0

    def _sort_scored_nodes(
        self,
        candidate_nodes: list[Node],
        *,
        temporal_hints: Any,
        similarity_by_id: dict[str, float],
        lexical_by_id: dict[str, float],
        degree_by_id: dict[str, int],
        max_access: int,
        max_degree: int,
        max_depth: int,
        expanded_depths: dict[str, int],
    ) -> list[Node]:
        def combined_score(node: Node) -> float:
            return score_node(
                node=node,
                semantic_similarity=similarity_by_id.get(node.id, 0.0),
                lexical_score=lexical_by_id.get(node.id, 0.0),
                max_access=max_access,
                degree_score=(degree_by_id.get(node.id, 0) / max_degree if max_degree > 0 else 0.0),
                depth=expanded_depths.get(node.id, max_depth + 1),
            ) + temporal_score_adjustment(node, temporal_hints)

        if temporal_hints.recency_mode == "latest":
            return sorted(
                candidate_nodes,
                key=lambda node: (-node.updated_at.timestamp(), -combined_score(node), node.label.lower()),
            )
        if temporal_hints.recency_mode == "oldest":
            return sorted(
                candidate_nodes,
                key=lambda node: (node.created_at.timestamp(), -combined_score(node), node.label.lower()),
            )
        return sorted(
            candidate_nodes,
            key=lambda node: (-combined_score(node), -node.updated_at.timestamp(), node.label.lower()),
        )

    def _expand_node_depths(self, graph: nx.DiGraph, seed_ids: list[str], max_depth: int) -> dict[str, int]:
        ordered: dict[str, int] = {}
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque((seed_id, 0) for seed_id in seed_ids)

        while queue:
            node_id, depth = queue.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            ordered[node_id] = depth
            if depth >= max_depth:
                continue
            neighbors = list(graph.predecessors(node_id)) + list(graph.successors(node_id))
            for neighbor in neighbors:
                if neighbor not in seen:
                    queue.append((neighbor, depth + 1))
        return ordered

    def _fetch_edges_for_nodes(self, session: Any, node_ids: list[str]) -> list[Edge]:
        if not node_ids:
            return []
        return [
            Edge(
                id=record["id"],
                tenant_id=self.tenant_id,
                source_id=record["source_id"],
                target_id=record["target_id"],
                relationship=record["relationship"],
                weight=float(record["weight"]),
                metadata=_decode_metadata(record["metadata"]),
                created_at=_parse_datetime(record["created_at"]),
            )
            for record in session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                WHERE source.id IN $node_ids AND target.id IN $node_ids
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                ORDER BY r.created_at ASC
                """,
                tenant_id=self.tenant_id,
                node_ids=node_ids,
            )
        ]

    def _increment_access_counts(self, session: Any, node_ids: list[str]) -> None:
        if not node_ids:
            return
        session.run(
            """
            UNWIND $node_ids AS node_id
            MATCH (n:MemoryNode {tenant_id: $tenant_id, id: node_id})
            SET n.access_count = coalesce(n.access_count, 0) + 1
            """,
            tenant_id=self.tenant_id,
            node_ids=node_ids,
        ).consume()

    def _find_existing_edge(
        self,
        session: Any,
        *,
        source_id: str,
        target_id: str,
        relationship: str | RelationType,
    ) -> Edge | None:
        record = session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, relationship: $relationship}]->(target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
            RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                   r.relationship AS relationship, r.weight AS weight, r.metadata AS metadata, r.created_at AS created_at
            LIMIT 1
            """,
            tenant_id=self.tenant_id,
            source_id=source_id,
            target_id=target_id,
            relationship=normalize_relationship(relationship),
        ).single()
        if record is None:
            return None
        return Edge(
            id=record["id"],
            tenant_id=self.tenant_id,
            source_id=record["source_id"],
            target_id=record["target_id"],
            relationship=record["relationship"],
            weight=float(record["weight"]),
            metadata=_decode_metadata(record["metadata"]),
            created_at=_parse_datetime(record["created_at"]),
        )

    def _delete_edge_record(
        self,
        *,
        source_id: str,
        target_id: str,
        relationship: str,
    ) -> bool:
        with self._lock, self._session() as session:
            summary = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, relationship: $relationship}]->(target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                DELETE r
                """,
                tenant_id=self.tenant_id,
                source_id=source_id,
                target_id=target_id,
                relationship=normalize_relationship(relationship),
            ).consume()
        return int(summary.counters.relationships_deleted or 0) > 0

    def _most_connected_node_ids(
        self,
        session: Any,
        *,
        limit: int,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> list[str]:
        selected: list[str] = []
        for record in session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $tenant_id})
            OPTIONAL MATCH (n)-[r:MEMORY_EDGE {tenant_id: $tenant_id}]-()
            WITH n, count(r) AS connection_count
            RETURN n, connection_count
            ORDER BY connection_count DESC, n.updated_at DESC
            """,
            tenant_id=self.tenant_id,
        ):
            node = self._node_from_props(record["n"])
            if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                continue
            selected.append(node.id)
            if len(selected) >= limit:
                break
        return selected

    def _find_project_node_ids(
        self,
        session: Any,
        *,
        project: str,
        agent_id: str = "",
        session_id: str = "",
        limit: int,
    ) -> list[str]:
        project_lower = project.strip().lower()
        scored: list[tuple[str, float, float]] = []
        for record in session.run(
            "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
            tenant_id=self.tenant_id,
        ):
            node = self._node_from_props(record["n"])
            if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                continue
            tag_match = 1.0 if any(project_lower == tag.lower() for tag in node.tags) else 0.0
            lexical = lexical_overlap(project, node.label, node.content)
            score = max(tag_match, lexical)
            if score <= 0.0:
                continue
            scored.append((node.id, score, node.updated_at.timestamp()))
        scored.sort(key=lambda item: (-item[1], -item[2]))
        return [node_id for node_id, _, _ in scored[:limit]]

    def _build_topic_partition(self, graph: nx.Graph, nodes: list[Node]) -> dict[str, int]:
        if graph.number_of_edges() == 0:
            return {node.id: index for index, node in enumerate(nodes)}
        try:
            import community  # type: ignore[import-not-found]

            return community.best_partition(graph)
        except ImportError:  # pragma: no cover
            communities = nx.algorithms.community.greedy_modularity_communities(graph)
            partition: dict[str, int] = {}
            for cluster_id, members in enumerate(communities):
                for member in members:
                    partition[str(member)] = cluster_id
            return partition

    def _fetch_edge_by_id(self, session: Any, edge_id: str) -> dict[str, Any] | None:
        return session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(target:MemoryNode {tenant_id: $tenant_id})
            RETURN r.id AS id
            """,
            tenant_id=self.tenant_id,
            id=edge_id,
        ).single()

    def _build_backup_snapshot(self, session: Any, *, include_embeddings: bool = False) -> dict[str, Any]:
        nodes = [
            {
                "id": props["id"],
                "tenant_id": props.get("tenant_id") or self.tenant_id,
                "agent_id": props.get("agent_id") or "",
                "project": props.get("project") or "",
                "session_id": props.get("session_id") or "",
                "context_window_id": props.get("context_window_id"),
                "label": props["label"],
                "content": props["content"],
                "node_type": props["node_type"],
                "tags": list(props.get("tags") or []),
                "source_prompt": props.get("source_prompt") or "",
                "metadata": _decode_metadata(props.get("metadata")),
                "evidence_records": [
                    record.model_dump(mode="json") for record in _decode_evidence_records(props.get("evidence_records"))
                ],
                "valid_from": props.get("valid_from"),
                "valid_to": props.get("valid_to"),
                "created_at": props["created_at"],
                "updated_at": props["updated_at"],
                "access_count": int(props.get("access_count") or 0),
                **(
                    {
                        "embedding": base64.b64encode(
                            np.array(props.get("embedding") or [], dtype=np.float32).astype(np.float32).tobytes()
                        ).decode("ascii")
                    }
                    if include_embeddings and props.get("embedding")
                    else {}
                ),
            }
            for props in (
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n ORDER BY n.created_at ASC",
                    tenant_id=self.tenant_id,
                )
            )
        ]
        edges = [
            {
                "id": record["id"],
                "tenant_id": self.tenant_id,
                "source_id": record["source_id"],
                "target_id": record["target_id"],
                "relationship": record["relationship"],
                "weight": float(record["weight"]),
                "metadata": _decode_metadata(record["metadata"]),
                "created_at": record["created_at"],
            }
            for record in session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                ORDER BY r.created_at ASC
                """,
                tenant_id=self.tenant_id,
            )
        ]
        snapshot = {"schema_version": SCHEMA_VERSION, "tenant_id": self.tenant_id, "nodes": nodes, "edges": edges}
        if include_embeddings:
            snapshot["embeddings"] = {node["id"]: node["embedding"] for node in nodes if node.get("embedding")}
            for node in nodes:
                node.pop("embedding", None)
        return snapshot

    def _insert_snapshot_node(self, session: Any, raw_node: dict[str, Any]) -> None:
        embedding_bytes = raw_node.get("embedding")
        embedding = (
            np.frombuffer(embedding_bytes, dtype=np.float32).astype(np.float32).tolist()
            if isinstance(embedding_bytes, bytes)
            else self.embedding_model.embed(raw_node["content"]).astype(np.float32).tolist()
        )
        session.run(
            """
            CREATE (n:MemoryNode {
                id: $id, tenant_id: $tenant_id, label: $label, content: $content, node_type: $node_type,
                tags: $tags, embedding: $embedding, source_prompt: $source_prompt,
                evidence_records: $evidence_records, valid_from: $valid_from, valid_to: $valid_to,
                created_at: $created_at, updated_at: $updated_at, access_count: $access_count
            })
            """,
            id=raw_node["id"],
            tenant_id=raw_node.get("tenant_id", self.tenant_id),
            label=raw_node["label"],
            content=raw_node["content"],
            node_type=raw_node["node_type"],
            tags=raw_node.get("tags", []),
            embedding=embedding,
            source_prompt=raw_node.get("source_prompt", ""),
            evidence_records=_encode_evidence_records(
                [EvidenceRecord.model_validate(item) for item in raw_node.get("evidence_records", [])]
            ),
            valid_from=raw_node.get("valid_from"),
            valid_to=raw_node.get("valid_to"),
            created_at=raw_node["created_at"],
            updated_at=raw_node["updated_at"],
            access_count=int(raw_node.get("access_count", 0)),
        ).consume()

    def _update_snapshot_node(self, session: Any, raw_node: dict[str, Any]) -> None:
        embedding_bytes = raw_node.get("embedding")
        embedding = (
            np.frombuffer(embedding_bytes, dtype=np.float32).astype(np.float32).tolist()
            if isinstance(embedding_bytes, bytes)
            else self.embedding_model.embed(raw_node["content"]).astype(np.float32).tolist()
        )
        session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $existing_tenant_id, id: $id})
            SET n.tenant_id = $tenant_id,
                n.label = $label,
                n.content = $content,
                n.node_type = $node_type,
                n.tags = $tags,
                n.embedding = $embedding,
                n.source_prompt = $source_prompt,
                n.evidence_records = $evidence_records,
                n.valid_from = $valid_from,
                n.valid_to = $valid_to,
                n.created_at = $created_at,
                n.updated_at = $updated_at,
                n.access_count = $access_count
            """,
            id=raw_node["id"],
            existing_tenant_id=self.tenant_id,
            tenant_id=raw_node.get("tenant_id", self.tenant_id),
            label=raw_node["label"],
            content=raw_node["content"],
            node_type=raw_node["node_type"],
            tags=raw_node.get("tags", []),
            embedding=embedding,
            source_prompt=raw_node.get("source_prompt", ""),
            evidence_records=_encode_evidence_records(
                [EvidenceRecord.model_validate(item) for item in raw_node.get("evidence_records", [])]
            ),
            valid_from=raw_node.get("valid_from"),
            valid_to=raw_node.get("valid_to"),
            created_at=raw_node["created_at"],
            updated_at=raw_node["updated_at"],
            access_count=int(raw_node.get("access_count", 0)),
        ).consume()

    def _insert_snapshot_edge(self, session: Any, raw_edge: dict[str, Any]) -> None:
        self._require_node(session, raw_edge["source_id"])
        self._require_node(session, raw_edge["target_id"])
        session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
            MATCH (target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
            CREATE (source)-[:MEMORY_EDGE {
                id: $id, tenant_id: $tenant_id, relationship: $relationship, weight: $weight,
                metadata: $metadata, created_at: $created_at
            }]->(target)
            """,
            id=raw_edge["id"],
            tenant_id=raw_edge.get("tenant_id", self.tenant_id),
            source_id=raw_edge["source_id"],
            target_id=raw_edge["target_id"],
            relationship=raw_edge["relationship"],
            weight=float(raw_edge.get("weight", 1.0)),
            metadata=_encode_metadata(raw_edge.get("metadata")),
            created_at=raw_edge["created_at"],
        ).consume()

    def _update_snapshot_edge(self, session: Any, raw_edge: dict[str, Any]) -> None:
        self._require_node(session, raw_edge["source_id"])
        self._require_node(session, raw_edge["target_id"])
        session.run(
            """
            MATCH ()-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->()
            DELETE r
            """,
            tenant_id=self.tenant_id,
            id=raw_edge["id"],
        ).consume()
        self._insert_snapshot_edge(session, raw_edge)
