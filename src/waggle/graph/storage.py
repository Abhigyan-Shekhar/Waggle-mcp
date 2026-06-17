from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from . import MemoryGraph

from waggle.auth import api_key_prefix, generate_api_key, hash_api_key, verify_api_key
from waggle.errors import AuthenticationError, ValidationFailure
from waggle.locks import ProcessLock
from waggle.models import (
    ApiKeyCreateResult,
    ApiKeyRecord,
    AuditEventRecord,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
    TenantRecord,
    utc_now,
)

from .base import (
    SCHEMA_VERSION,
    MemoryGraphBase,
    _decode_metadata,
    _parse_datetime,
)

LOGGER = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    api_key_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    prefix TEXT DEFAULT '',
    name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    expires_at TEXT DEFAULT NULL,
    revoked_at TEXT DEFAULT NULL,
    last_used_at TEXT DEFAULT NULL,
    created_by TEXT DEFAULT '',
    scopes TEXT DEFAULT '["graph:read","graph:write","admin:read","admin:write"]',
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    agent_id TEXT DEFAULT '',
    project TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    context_window_id TEXT DEFAULT NULL,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK(
        node_type IN ('fact', 'entity', 'concept', 'preference', 'decision', 'question', 'note')
    ),
    tags TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    embedding BLOB,
    embedding_model_id TEXT DEFAULT '',
    embedding_dim INTEGER DEFAULT 0,
    source_prompt TEXT DEFAULT '',
    source_turn_pair_id TEXT DEFAULT '',
    evidence_records TEXT DEFAULT '[]',
    valid_from TEXT DEFAULT NULL,
    valid_to TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    access_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, name)
);

CREATE TABLE IF NOT EXISTS context_windows (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    repo_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed', 'archived')),
    node_count INTEGER DEFAULT 0,
    embedding BLOB,
    embedding_stale INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT DEFAULT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id) ON DELETE CASCADE,
    UNIQUE(tenant_id, repo_id, session_id)
);

CREATE TABLE IF NOT EXISTS context_window_edges (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    source_window_id TEXT NOT NULL,
    target_window_id TEXT NOT NULL,
    edge_type TEXT NOT NULL CHECK(edge_type IN (
        'entity_overlap',
        'supersedes',
        'temporal_sequence',
        'continuation',
        'shared_scope'
    )),
    shared_entities TEXT DEFAULT '[]',
    weight REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_window_id) REFERENCES context_windows(id) ON DELETE CASCADE,
    FOREIGN KEY (target_window_id) REFERENCES context_windows(id) ON DELETE CASCADE,
    UNIQUE(tenant_id, source_window_id, target_window_id, edge_type, shared_entities)
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transcript_records (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    agent_id TEXT DEFAULT '',
    project TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    observed_at TEXT NOT NULL,
    turn_index INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT '',
    transcript_text TEXT NOT NULL,
    embedding BLOB,
    embedding_model_id TEXT DEFAULT '',
    embedding_dim INTEGER DEFAULT 0,
    content_hash TEXT DEFAULT '',
    turn_pair_id TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    message_identity TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS graph_ui_state (
    tenant_id TEXT NOT NULL DEFAULT 'local-default',
    agent_id TEXT DEFAULT '',
    project TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    positions TEXT DEFAULT '{}',
    zoom REAL DEFAULT 1.0,
    viewport TEXT DEFAULT '{}',
    groups_json TEXT DEFAULT '[]',
    collapsed_groups TEXT DEFAULT '[]',
    selected_nodes TEXT DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, project, session_id)
);

CREATE TABLE IF NOT EXISTS retention_policy (
    tenant_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    retention_days INTEGER NOT NULL DEFAULT 90,
    prune_interval_hours INTEGER NOT NULL DEFAULT 24,
    last_pruned_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS retention_prune_runs (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT DEFAULT NULL,
    deleted_nodes INTEGER NOT NULL DEFAULT 0,
    deleted_edges INTEGER NOT NULL DEFAULT 0,
    deleted_transcripts INTEGER NOT NULL DEFAULT 0,
    deleted_context_windows INTEGER NOT NULL DEFAULT 0,
    deleted_context_window_edges INTEGER NOT NULL DEFAULT 0,
    deleted_exports INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_id TEXT DEFAULT '',
    api_key_id TEXT DEFAULT '',
    resource_type TEXT DEFAULT '',
    resource_id TEXT DEFAULT '',
    action TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success',
    ip_address TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
);
"""


INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_created ON nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_nodes_tenant_type ON nodes(tenant_id, node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_tenant_updated ON nodes(tenant_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_nodes_context_window ON nodes(context_window_id);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_relationship ON edges(relationship);
CREATE INDEX IF NOT EXISTS idx_edges_tenant_relationship ON edges(tenant_id, relationship);
CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_observed ON transcript_records(tenant_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_session_turn ON transcript_records(tenant_id, session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_content_hash ON transcript_records(tenant_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_turn_pair ON transcript_records(tenant_id, turn_pair_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_project ON transcript_records(tenant_id, project);
CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_agent ON transcript_records(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_source_turn_pair ON nodes(tenant_id, source_turn_pair_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_repos_tenant_name ON repos(tenant_id, name);
CREATE INDEX IF NOT EXISTS idx_context_windows_repo ON context_windows(repo_id);
CREATE INDEX IF NOT EXISTS idx_context_windows_session ON context_windows(session_id);
CREATE INDEX IF NOT EXISTS idx_context_windows_status ON context_windows(status);
CREATE INDEX IF NOT EXISTS idx_cw_edges_source ON context_window_edges(source_window_id);
CREATE INDEX IF NOT EXISTS idx_cw_edges_target ON context_window_edges(target_window_id);
CREATE INDEX IF NOT EXISTS idx_cw_edges_type ON context_window_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_graph_ui_scope ON graph_ui_state(tenant_id, project, agent_id, session_id);
CREATE INDEX IF NOT EXISTS idx_retention_runs_tenant_started ON retention_prune_runs(tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_created ON audit_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_type ON audit_events(tenant_id, event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_actor ON audit_events(tenant_id, actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_resource ON audit_events(tenant_id, resource_id);
"""


class StorageMixin(MemoryGraphBase):
    """Mixin class for MemoryGraph handling database, tenants, API keys, retention, and audit events."""

    def _initialize_database(self) -> None:
        """Initialize the database schema, migrations, and WAL mode.

        Performs one-time setup including:
        1. Bootstrap WAL mode if database exists in rollback mode
        2. Create schema if new
        3. Run legacy migrations
        4. Create indexes
        5. Ensure tenant record exists

        Uses ProcessLock to protect multi-statement migration from concurrent access.
        """
        # Wrap migration in cross-process lock to prevent concurrent schema modifications
        lock_path = str(self.db_path) + ".lock"
        with ProcessLock(lock_path), self._lock:
            self._validate_existing_database_integrity()

            with self._connect() as connection:
                # Bootstrap WAL: if db file exists but is in rollback mode, migrate it
                try:
                    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                    if journal_mode.upper() != "WAL":
                        LOGGER.info(
                            "Migrating database %s from %s to WAL mode",
                            self.db_path,
                            journal_mode,
                        )
                        connection.execute("PRAGMA journal_mode=WAL")
                except Exception as e:
                    LOGGER.warning("Could not verify journal mode: %s", e)

                # Initialize schema
                connection.executescript(SCHEMA_SQL)
                self._migrate_legacy_schema(connection)
                connection.executescript(INDEX_SQL)

                # Ensure tenant record
                created_at = utc_now().isoformat()
                connection.execute(
                    """
                        INSERT INTO tenants (tenant_id, name, status, created_at)
                        VALUES (?, '', 'active', ?)
                        ON CONFLICT(tenant_id) DO NOTHING
                        """,
                    (self.tenant_id, created_at),
                )

    def _validate_existing_database_integrity(self) -> None:
        """Fail before mutating an existing database that SQLite cannot recover."""
        import contextlib

        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return

        db_uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        try:
            with contextlib.closing(sqlite3.connect(db_uri, uri=True)) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValidationFailure(
                "Waggle memory database failed SQLite integrity validation before startup. "
                f"Refusing to mutate existing DB at {self.db_path}. Restore from backup or move the file aside."
            ) from exc

        if result is None or result[0] != "ok":
            details = result[0] if result else "no integrity result"
            raise ValidationFailure(
                "Waggle memory database failed SQLite integrity validation before startup. "
                f"Refusing to mutate existing DB at {self.db_path}: {details}. "
                "Restore from backup or move the file aside."
            )

    def for_tenant(self, tenant_id: str) -> MemoryGraph:
        clone = object.__new__(self.__class__)
        clone.db_path = self.db_path
        clone.embedding_model = self.embedding_model
        clone.tenant_id = tenant_id.strip() or "local-default"
        clone.dedup_similarity_threshold = self.dedup_similarity_threshold
        clone.dedup_same_label_threshold = self.dedup_same_label_threshold
        clone.enable_dedup = self.enable_dedup
        clone.recency_half_life_days = self.recency_half_life_days
        clone.tiered_retrieval = self.tiered_retrieval
        clone.tiered_retrieval_top_k_windows = self.tiered_retrieval_top_k_windows
        clone.hybrid_retrieval_config = self.hybrid_retrieval_config
        clone.export_dir = self.export_dir
        clone.api_key_environment = self.api_key_environment
        clone._lock = self._lock
        clone._pool = self._pool
        clone._owns_pool = False

        clone._pool_owner = self
        clone.ensure_tenant(clone.tenant_id)
        return clone

    def ensure_tenant(self, tenant_id: str, name: str = "") -> TenantRecord:
        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id:
            raise ValidationFailure("Tenant ID cannot be empty.")
        created_at = utc_now().isoformat()
        with self._lock, self._pool.checkout() as connection:
            connection.execute(
                """
                INSERT INTO tenants (tenant_id, name, status, created_at)
                VALUES (?, ?, 'active', ?)
                ON CONFLICT(tenant_id) DO UPDATE SET name = CASE WHEN excluded.name != '' THEN excluded.name ELSE tenants.name END
                """,
                (normalized_tenant_id, name.strip(), created_at),
            )
            row = connection.execute(
                "SELECT tenant_id, name, status, created_at FROM tenants WHERE tenant_id = ?",
                (normalized_tenant_id,),
            ).fetchone()
        return TenantRecord(
            tenant_id=row["tenant_id"],
            name=row["name"] or "",
            status=row["status"],
            created_at=_parse_datetime(row["created_at"]),
        )

    @staticmethod
    def _normalize_ui_scope(*, project: str = "", agent_id: str = "", session_id: str = "") -> tuple[str, str, str]:
        return (project.strip(), agent_id.strip(), session_id.strip())

    def get_ui_state(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        normalized_project, normalized_agent, normalized_session = self._normalize_ui_scope(
            project=project, agent_id=agent_id, session_id=session_id
        )
        with self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                """
                SELECT positions, zoom, viewport, groups_json, collapsed_groups, selected_nodes
                FROM graph_ui_state
                WHERE tenant_id = ? AND project = ? AND agent_id = ? AND session_id = ?
                """,
                (self.tenant_id, normalized_project, normalized_agent, normalized_session),
            ).fetchone()
        if row is None:
            return {
                "positions": {},
                "zoom": 1.0,
                "viewport": {"center_x": 0, "center_y": 0},
                "groups": [],
                "collapsed_groups": [],
                "selected_nodes": [],
            }
        return {
            "positions": json.loads(row["positions"] or "{}"),
            "zoom": float(row["zoom"] if row["zoom"] is not None else 1.0),
            "viewport": json.loads(row["viewport"] or "{}") or {"center_x": 0, "center_y": 0},
            "groups": json.loads(row["groups_json"] or "[]"),
            "collapsed_groups": json.loads(row["collapsed_groups"] or "[]"),
            "selected_nodes": json.loads(row["selected_nodes"] or "[]"),
        }

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
        normalized_project, normalized_agent, normalized_session = self._normalize_ui_scope(
            project=project, agent_id=agent_id, session_id=session_id
        )
        lock_path = f"{self.db_path}.lock"
        with ProcessLock(lock_path), self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                """
                SELECT positions, zoom, viewport, groups_json, collapsed_groups, selected_nodes
                FROM graph_ui_state
                WHERE tenant_id = ? AND project = ? AND agent_id = ? AND session_id = ?
                """,
                (self.tenant_id, normalized_project, normalized_agent, normalized_session),
            ).fetchone()
            if row is None:
                current = {
                    "positions": {},
                    "zoom": 1.0,
                    "viewport": {"center_x": 0, "center_y": 0},
                    "groups": [],
                    "collapsed_groups": [],
                    "selected_nodes": [],
                }
            else:
                current = {
                    "positions": json.loads(row["positions"] or "{}"),
                    "zoom": float(row["zoom"] if row["zoom"] is not None else 1.0),
                    "viewport": json.loads(row["viewport"] or "{}") or {"center_x": 0, "center_y": 0},
                    "groups": json.loads(row["groups_json"] or "[]"),
                    "collapsed_groups": json.loads(row["collapsed_groups"] or "[]"),
                    "selected_nodes": json.loads(row["selected_nodes"] or "[]"),
                }

            merged = {
                "positions": positions if positions is not None else current["positions"],
                "zoom": float(zoom if zoom is not None else current["zoom"]),
                "viewport": viewport if viewport is not None else current["viewport"],
                "groups": groups if groups is not None else current["groups"],
                "collapsed_groups": collapsed_groups if collapsed_groups is not None else current["collapsed_groups"],
                "selected_nodes": selected_nodes if selected_nodes is not None else current["selected_nodes"],
            }
            connection.execute(
                """
                INSERT INTO graph_ui_state (
                    tenant_id, agent_id, project, session_id,
                    positions, zoom, viewport, groups_json, collapsed_groups, selected_nodes, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, agent_id, project, session_id)
                DO UPDATE SET
                    positions = excluded.positions,
                    zoom = excluded.zoom,
                    viewport = excluded.viewport,
                    groups_json = excluded.groups_json,
                    collapsed_groups = excluded.collapsed_groups,
                    selected_nodes = excluded.selected_nodes,
                    updated_at = excluded.updated_at
                """,
                (
                    self.tenant_id,
                    normalized_agent,
                    normalized_project,
                    normalized_session,
                    json.dumps(merged["positions"], sort_keys=True),
                    merged["zoom"],
                    json.dumps(merged["viewport"], sort_keys=True),
                    json.dumps(merged["groups"], sort_keys=True),
                    json.dumps(merged["collapsed_groups"], sort_keys=True),
                    json.dumps(merged["selected_nodes"], sort_keys=True),
                    utc_now().isoformat(),
                ),
            )
        return merged

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
        with self._lock, self._pool.checkout() as connection:
            connection.execute(
                """
                INSERT INTO api_keys (api_key_id, tenant_id, key_hash, prefix, name, status, created_at, expires_at, revoked_at, last_used_at, created_by, scopes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.api_key_id,
                    record.tenant_id,
                    record.key_hash,
                    record.prefix,
                    record.name,
                    record.status,
                    record.created_at.isoformat(),
                    record.expires_at.isoformat() if record.expires_at else None,
                    None,
                    None,
                    record.created_by,
                    json.dumps(record.scopes),
                ),
            )
        return ApiKeyCreateResult(record=record, raw_api_key=raw_api_key)

    def list_api_keys(self, tenant_id: str) -> list[ApiKeyRecord]:
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT api_key_id, tenant_id, key_hash, prefix, name, status, created_at, expires_at, revoked_at, last_used_at, created_by, scopes
                FROM api_keys
                WHERE tenant_id = ?
                ORDER BY created_at DESC
                """,
                (tenant_id,),
            ).fetchall()
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
                scopes=json.loads(row["scopes"] or "[]"),
            )
            for row in rows
        ]

    def revoke_api_key(self, api_key_id: str) -> None:
        with self._lock, self._pool.checkout() as connection:
            connection.execute(
                "UPDATE api_keys SET status = 'revoked', revoked_at = ? WHERE tenant_id = ? AND api_key_id = ?",
                (utc_now().isoformat(), self.tenant_id, api_key_id),
            )

    def get_retention_policy(
        self,
        *,
        default_enabled: bool = False,
        default_retention_days: int = 90,
        default_prune_interval_hours: int = 24,
    ) -> RetentionPolicyRecord:
        now = utc_now()
        with self._lock, self._pool.checkout() as connection:
            connection.execute(
                """
                INSERT INTO retention_policy (
                    tenant_id, enabled, retention_days, prune_interval_hours, last_pruned_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(tenant_id) DO NOTHING
                """,
                (
                    self.tenant_id,
                    1 if default_enabled else 0,
                    int(default_retention_days),
                    int(default_prune_interval_hours),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT tenant_id, enabled, retention_days, prune_interval_hours, last_pruned_at, created_at, updated_at
                FROM retention_policy
                WHERE tenant_id = ?
                """,
                (self.tenant_id,),
            ).fetchone()
        return RetentionPolicyRecord(
            tenant_id=row["tenant_id"],
            enabled=bool(row["enabled"]),
            retention_days=int(row["retention_days"]),
            prune_interval_hours=int(row["prune_interval_hours"]),
            last_pruned_at=_parse_datetime(row["last_pruned_at"]) if row["last_pruned_at"] else None,
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
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
        with self._lock, self._pool.checkout() as connection:
            connection.execute(
                """
                UPDATE retention_policy
                SET enabled = ?, retention_days = ?, prune_interval_hours = ?, updated_at = ?
                WHERE tenant_id = ?
                """,
                (
                    1 if next_enabled else 0,
                    next_retention_days,
                    next_prune_interval_hours,
                    updated_at.isoformat(),
                    self.tenant_id,
                ),
            )
        return self.get_retention_policy(
            default_enabled=default_enabled,
            default_retention_days=default_retention_days,
            default_prune_interval_hours=default_prune_interval_hours,
        )

    def list_retention_runs(self, *, limit: int = 20) -> list[RetentionPruneRunRecord]:
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT run_id, tenant_id, status, cutoff, started_at, completed_at,
                       deleted_nodes, deleted_edges, deleted_transcripts, deleted_context_windows,
                       deleted_context_window_edges, deleted_exports, duration_ms, error_message
                FROM retention_prune_runs
                WHERE tenant_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (self.tenant_id, max(1, int(limit))),
            ).fetchall()
        return [
            RetentionPruneRunRecord(
                run_id=row["run_id"],
                tenant_id=row["tenant_id"],
                status=row["status"],
                cutoff=_parse_datetime(row["cutoff"]),
                started_at=_parse_datetime(row["started_at"]),
                completed_at=_parse_datetime(row["completed_at"]) if row["completed_at"] else None,
                deleted_nodes=int(row["deleted_nodes"] or 0),
                deleted_edges=int(row["deleted_edges"] or 0),
                deleted_transcripts=int(row["deleted_transcripts"] or 0),
                deleted_context_windows=int(row["deleted_context_windows"] or 0),
                deleted_context_window_edges=int(row["deleted_context_window_edges"] or 0),
                deleted_exports=int(row["deleted_exports"] or 0),
                duration_ms=int(row["duration_ms"] or 0),
                error_message=row["error_message"] or "",
            )
            for row in rows
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
            run.duration_ms = 0
            self._store_retention_run(run)
            return run

        batch_limit = max(1, int(batch_size))
        try:
            with self._lock, self._pool.checkout() as connection:
                run.deleted_context_window_edges = self._prune_table_by_ids(
                    connection,
                    select_sql="""
                        SELECT id FROM context_window_edges
                        WHERE tenant_id = ? AND created_at < ?
                        LIMIT ?
                    """,
                    delete_sql="DELETE FROM context_window_edges WHERE id IN ({placeholders})",
                    params=(self.tenant_id, cutoff.isoformat()),
                    batch_limit=batch_limit,
                )
                run.deleted_edges = self._prune_table_by_ids(
                    connection,
                    select_sql="""
                        SELECT id FROM edges
                        WHERE tenant_id = ? AND created_at < ?
                        LIMIT ?
                    """,
                    delete_sql="DELETE FROM edges WHERE id IN ({placeholders})",
                    params=(self.tenant_id, cutoff.isoformat()),
                    batch_limit=batch_limit,
                )
                run.deleted_nodes = self._prune_table_by_ids(
                    connection,
                    select_sql="""
                        SELECT id FROM nodes
                        WHERE tenant_id = ? AND created_at < ?
                        LIMIT ?
                    """,
                    delete_sql="DELETE FROM nodes WHERE id IN ({placeholders})",
                    params=(self.tenant_id, cutoff.isoformat()),
                    batch_limit=batch_limit,
                )
                run.deleted_transcripts = self._prune_table_by_ids(
                    connection,
                    select_sql="""
                        SELECT id FROM transcript_records
                        WHERE tenant_id = ? AND observed_at < ?
                        LIMIT ?
                    """,
                    delete_sql="DELETE FROM transcript_records WHERE id IN ({placeholders})",
                    params=(self.tenant_id, cutoff.isoformat()),
                    batch_limit=batch_limit,
                )
                run.deleted_context_windows = self._prune_table_by_ids(
                    connection,
                    select_sql="""
                        SELECT id FROM context_windows
                        WHERE tenant_id = ? AND created_at < ?
                        LIMIT ?
                    """,
                    delete_sql="DELETE FROM context_windows WHERE id IN ({placeholders})",
                    params=(self.tenant_id, cutoff.isoformat()),
                    batch_limit=batch_limit,
                )
                run.deleted_exports = self._delete_old_export_files(cutoff=cutoff)
                completed_at = utc_now()
                run.completed_at = completed_at
                run.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
                connection.execute(
                    """
                    UPDATE retention_policy
                    SET last_pruned_at = ?, updated_at = ?
                    WHERE tenant_id = ?
                    """,
                    (completed_at.isoformat(), completed_at.isoformat(), self.tenant_id),
                )
                self._store_retention_run(run, connection=connection)
                self.emit_audit_event(
                    event_type="retention.prune.completed",
                    resource_type="retention_policy",
                    resource_id=self.tenant_id,
                    action="prune",
                    metadata={
                        "run_id": run.run_id,
                        "cutoff": run.cutoff.isoformat(),
                        "deleted_nodes": run.deleted_nodes,
                        "deleted_edges": run.deleted_edges,
                        "deleted_transcripts": run.deleted_transcripts,
                        "deleted_context_windows": run.deleted_context_windows,
                        "deleted_context_window_edges": run.deleted_context_window_edges,
                        "deleted_exports": run.deleted_exports,
                        "duration_ms": run.duration_ms,
                    },
                    connection=connection,
                )
        except Exception as exc:
            completed_at = utc_now()
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = completed_at
            run.duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
            self._store_retention_run(run)
            self.emit_audit_event(
                event_type="retention.prune.failed",
                resource_type="retention_policy",
                resource_id=self.tenant_id,
                action="prune",
                status="failed",
                metadata={"run_id": run.run_id, "cutoff": run.cutoff.isoformat(), "error_message": run.error_message},
            )
            raise
        return run

    def authenticate_api_key(self, raw_api_key: str) -> ApiKeyRecord:
        key_hash = hash_api_key(raw_api_key)
        with self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                """
                SELECT api_key_id, tenant_id, key_hash, prefix, name, status, created_at, expires_at, revoked_at, last_used_at, created_by, scopes
                FROM api_keys
                WHERE key_hash = ?
                LIMIT 1
                """,
                (key_hash,),
            ).fetchone()
            if row is None or not verify_api_key(raw_api_key, row["key_hash"]):
                raise AuthenticationError("Invalid API key.")
            if row["status"] != "active":
                raise AuthenticationError("Invalid API key.")
            expires_at = _parse_datetime(row["expires_at"]) if row["expires_at"] else None
            if expires_at is not None and expires_at <= utc_now():
                raise AuthenticationError("API key expired.")
            connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE api_key_id = ?",
                (utc_now().isoformat(), row["api_key_id"]),
            )
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
            scopes=json.loads(row["scopes"] or "[]"),
        )

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        api_key_columns = {row["name"] for row in connection.execute("PRAGMA table_info(api_keys)").fetchall()}
        node_columns = {row["name"] for row in connection.execute("PRAGMA table_info(nodes)").fetchall()}
        edge_columns = {row["name"] for row in connection.execute("PRAGMA table_info(edges)").fetchall()}
        if "prefix" not in api_key_columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN prefix TEXT DEFAULT ''")
            connection.execute("UPDATE api_keys SET prefix = substr(key_hash, 1, 16) WHERE prefix = ''")
        if "expires_at" not in api_key_columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN expires_at TEXT DEFAULT NULL")
        if "revoked_at" not in api_key_columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN revoked_at TEXT DEFAULT NULL")
        if "created_by" not in api_key_columns:
            connection.execute("ALTER TABLE api_keys ADD COLUMN created_by TEXT DEFAULT ''")
        if "scopes" not in api_key_columns:
            connection.execute(
                """ALTER TABLE api_keys ADD COLUMN scopes TEXT DEFAULT '["graph:read","graph:write","admin:read","admin:write"]'"""
            )
        if "tenant_id" not in node_columns:
            connection.execute(f"ALTER TABLE nodes ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{self.tenant_id}'")
            connection.execute("UPDATE nodes SET tenant_id = ? WHERE tenant_id = ''", (self.tenant_id,))
        if "evidence_records" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN evidence_records TEXT DEFAULT '[]'")
        if "metadata" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN metadata TEXT DEFAULT '{}'")
        if "valid_from" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN valid_from TEXT DEFAULT NULL")
        if "valid_to" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN valid_to TEXT DEFAULT NULL")
        if "agent_id" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN agent_id TEXT DEFAULT ''")
        if "project" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN project TEXT DEFAULT ''")
        if "session_id" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN session_id TEXT DEFAULT ''")
        if "context_window_id" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN context_window_id TEXT DEFAULT NULL")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_nodes_context_window ON nodes(context_window_id)")
        if "embedding_model_id" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN embedding_model_id TEXT DEFAULT ''")
        if "embedding_dim" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN embedding_dim INTEGER DEFAULT 0")
        if "source_turn_pair_id" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN source_turn_pair_id TEXT DEFAULT ''")
        if "aliases" not in node_columns:
            connection.execute("ALTER TABLE nodes ADD COLUMN aliases TEXT DEFAULT '[]'")
        if "tenant_id" not in edge_columns:
            connection.execute(f"ALTER TABLE edges ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{self.tenant_id}'")
            connection.execute("UPDATE edges SET tenant_id = ? WHERE tenant_id = ''", (self.tenant_id,))
        transcript_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(transcript_records)").fetchall()
        }
        if "message_identity" not in transcript_columns:
            connection.execute("ALTER TABLE transcript_records ADD COLUMN message_identity TEXT DEFAULT NULL")
        if "embedding_model_id" not in transcript_columns:
            connection.execute("ALTER TABLE transcript_records ADD COLUMN embedding_model_id TEXT DEFAULT ''")
        if "embedding_dim" not in transcript_columns:
            connection.execute("ALTER TABLE transcript_records ADD COLUMN embedding_dim INTEGER DEFAULT 0")
        if "content_hash" not in transcript_columns:
            connection.execute("ALTER TABLE transcript_records ADD COLUMN content_hash TEXT DEFAULT ''")
        if "turn_pair_id" not in transcript_columns:
            connection.execute("ALTER TABLE transcript_records ADD COLUMN turn_pair_id TEXT DEFAULT ''")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_ui_state (
                tenant_id TEXT NOT NULL DEFAULT 'local-default',
                agent_id TEXT DEFAULT '',
                project TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                positions TEXT DEFAULT '{}',
                zoom REAL DEFAULT 1.0,
                viewport TEXT DEFAULT '{}',
                groups_json TEXT DEFAULT '[]',
                collapsed_groups TEXT DEFAULT '[]',
                selected_nodes TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, agent_id, project, session_id)
            )
            """
        )
        # Always ensure the partial unique index exists (IF NOT EXISTS is safe for reruns).
        # Must be outside the if-block so new databases (where the column comes from CREATE TABLE)
        # also get the index, not just existing databases that went through ALTER TABLE.
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_identity
            ON transcript_records(tenant_id, session_id, message_identity)
            WHERE message_identity IS NOT NULL
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_content_hash ON transcript_records(tenant_id, content_hash)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcripts_tenant_turn_pair ON transcript_records(tenant_id, turn_pair_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_source_turn_pair ON nodes(tenant_id, source_turn_pair_id)"
        )

        self._backfill_transcript_storage(connection, batch_size=100)

        # Embedding checksum migration (issue #71): upgrade this tenant's
        # pre-checksum blobs to the canonical CRC-trailered form.
        if hasattr(self, "_migrate_embeddings_to_checksummed_conn"):
            upgraded = self._migrate_embeddings_to_checksummed_conn(connection)
            if upgraded["nodes"] or upgraded["transcript_records"] or upgraded["context_windows"]:
                LOGGER.info(
                    "Upgraded %d node, %d transcript and %d window embeddings to checksummed format for tenant %s",
                    upgraded["nodes"],
                    upgraded["transcript_records"],
                    upgraded["context_windows"],
                    self.tenant_id,
                )

        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version, applied_at)
            VALUES (?, ?)
            """,
            (SCHEMA_VERSION, utc_now().isoformat()),
        )

    def _prune_table_by_ids(
        self,
        connection: sqlite3.Connection,
        *,
        select_sql: str,
        delete_sql: str,
        params: tuple[Any, ...],
        batch_limit: int,
    ) -> int:
        deleted = 0
        while True:
            rows = connection.execute(select_sql, (*params, batch_limit)).fetchall()
            if not rows:
                return deleted
            ids = [row["id"] for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            connection.execute(delete_sql.format(placeholders=placeholders), ids)
            deleted += len(ids)

    def _store_retention_run(
        self,
        run: RetentionPruneRunRecord,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        connection_ctx = nullcontext(connection) if connection is not None else self._pool.checkout()
        with connection_ctx as active_connection:
            active_connection.execute(
                """
                INSERT OR REPLACE INTO retention_prune_runs (
                    run_id, tenant_id, status, cutoff, started_at, completed_at,
                    deleted_nodes, deleted_edges, deleted_transcripts, deleted_context_windows,
                    deleted_context_window_edges, deleted_exports, duration_ms, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.tenant_id,
                    run.status,
                    run.cutoff.isoformat(),
                    run.started_at.isoformat(),
                    run.completed_at.isoformat() if run.completed_at else None,
                    run.deleted_nodes,
                    run.deleted_edges,
                    run.deleted_transcripts,
                    run.deleted_context_windows,
                    run.deleted_context_window_edges,
                    run.deleted_exports,
                    run.duration_ms,
                    run.error_message,
                ),
            )

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
        connection: sqlite3.Connection | None = None,
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
        connection_ctx = nullcontext(connection) if connection is not None else self._pool.checkout()
        with connection_ctx as active_connection:
            active_connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, tenant_id, event_type, actor_type, actor_id, api_key_id,
                    resource_type, resource_id, action, status, ip_address, user_agent,
                    created_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.tenant_id,
                    event.event_type,
                    event.actor_type,
                    event.actor_id,
                    event.api_key_id,
                    event.resource_type,
                    event.resource_id,
                    event.action,
                    event.status,
                    event.ip_address,
                    event.user_agent,
                    event.created_at.isoformat(),
                    json.dumps(event.metadata),
                ),
            )
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
        predicates = ["tenant_id = ?"]
        values: list[Any] = [self.tenant_id]
        if event_type.strip():
            predicates.append("event_type = ?")
            values.append(event_type.strip())
        if actor_id.strip():
            predicates.append("actor_id = ?")
            values.append(actor_id.strip())
        if resource_id.strip():
            predicates.append("resource_id = ?")
            values.append(resource_id.strip())
        if resource_type.strip():
            predicates.append("resource_type = ?")
            values.append(resource_type.strip())
        if status.strip():
            predicates.append("status = ?")
            values.append(status.strip())
        query = f"""
            SELECT event_id, tenant_id, event_type, actor_type, actor_id, api_key_id,
                   resource_type, resource_id, action, status, ip_address, user_agent,
                   created_at, metadata
            FROM audit_events
            WHERE {" AND ".join(predicates)}
            ORDER BY created_at DESC
            LIMIT ?
        """
        values.append(max(1, int(limit)))
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [
            AuditEventRecord(
                event_id=row["event_id"],
                tenant_id=row["tenant_id"],
                event_type=row["event_type"],
                actor_type=row["actor_type"] or "system",
                actor_id=row["actor_id"] or "",
                api_key_id=row["api_key_id"] or "",
                resource_type=row["resource_type"] or "",
                resource_id=row["resource_id"] or "",
                action=row["action"] or "",
                status=row["status"] or "success",
                ip_address=row["ip_address"] or "",
                user_agent=row["user_agent"] or "",
                created_at=_parse_datetime(row["created_at"]),
                metadata=_decode_metadata(row["metadata"]),
            )
            for row in rows
        ]
