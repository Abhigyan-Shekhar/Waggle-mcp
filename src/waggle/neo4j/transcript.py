from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import numpy as np

from waggle.auth import api_key_prefix, generate_api_key, hash_api_key, verify_api_key
from waggle.errors import AuthenticationError, ValidationFailure
from waggle.evidence import build_observation_evidence
from waggle.graph.base import (
    MemoryGraphBase,
    _decode_metadata,
    _encode_metadata,
    _parse_datetime,
)
from waggle.intelligence import (
    extract_conversation_candidates,
)
from waggle.models import (
    ApiKeyCreateResult,
    ApiKeyRecord,
    AuditEventRecord,
    ConflictEntry,
    ConflictListResult,
    ConnectedNodeStat,
    Edge,
    GraphStats,
    NodeType,
    ObservationResult,
    RecentNodeStat,
    RelationType,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
    TranscriptRecord,
    utc_now,
)


class Neo4jTranscriptMixin(MemoryGraphBase):
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
            session.run(delete_query, ids=ids, tenant_id=self.tenant_id).consume()
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
                metadata=_encode_metadata(event.metadata),
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
                metadata=_decode_metadata(props.get("metadata")),
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
                        MATCH ()-[r:CONTEXT_WINDOW_EDGE {tenant_id: $tenant_id}]->()
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
                        MATCH ()-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->()
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
                        MATCH (n:MemoryNode {tenant_id: $tenant_id})
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
                        MATCH (t:MemoryTranscript {tenant_id: $tenant_id})
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
                        MATCH (w:ContextWindow {tenant_id: $tenant_id})
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
