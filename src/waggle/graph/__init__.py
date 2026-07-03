from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
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
    validate_abhi_signature,
    write_abhi_document,
)
from waggle.auth import api_key_prefix, generate_api_key, hash_api_key, verify_api_key
from waggle.connection_pool import DEFAULT_POOL_SIZE, SQLiteConnectionPool
from waggle.context_bundle import build_context_bundle, build_query_summary, export_context_bundle_files
from waggle.embeddings import EmbeddingModel
from waggle.errors import AuthenticationError, ValidationFailure
from waggle.intelligence import (
    extract_conversation_candidates as extract_conversation_candidates,
)
from waggle.intelligence import (
    infer_label,
    infer_node_type,
    infer_relationship,
    normalize_text,
    parse_since_value,
    split_atomic_items,
    tokenize_text,
)
from waggle.locks import ProcessLock
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
    ContextBundleExportResult,
    ContextScopeResult,
    ContextWindow,
    ContextWindowEdge,
    Edge,
    EvidenceRecord,
    GraphDiffResult,
    ImportResult,
    MarkdownVaultExportResult,
    MarkdownVaultImportResult,
    Node,
    NodeHistoryResult,
    NodeType,
    PrimeContextResult,
    RelationType,
    ReplayHit,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
    SubgraphResult,
    TenantRecord,
    utc_now,
)
from waggle.retrieval.hybrid import HybridRetrievalConfig, HybridRetriever

from .base import (
    EMBEDDING_BLOB_MAGIC as EMBEDDING_BLOB_MAGIC,
)
from .base import (
    MemoryGraphBase,
    _decode_metadata,
    _encode_evidence_records,
    _encode_metadata,
    _filter_valid_nodes,
    _normalized_content_hash,
    _parse_datetime,
    _retrieval_session_scope,
    _scope_matches,
)
from .base import (
    decode_embedding_blob as decode_embedding_blob,
)
from .base import (
    encode_embedding_blob as encode_embedding_blob,
)
from .base import (
    ensure_encoded_embedding as ensure_encoded_embedding,
)
from .base import (
    is_checksummed_embedding as is_checksummed_embedding,
)
from .base import (
    recency_weight as recency_weight,
)
from .base import (
    score_node as score_node,
)
from .mutation import MutationMixin
from .transcript import TranscriptMixin
from .traversal import TraversalMixin

SCHEMA_VERSION = 7

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _NeutralTemporalHints:
    """Neutral temporal hints for operations without query-driven time intent."""

    recency_mode: str = "none"
    time_window_start = None
    time_window_end = None


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


def _decode_evidence_records(raw: Any) -> list[EvidenceRecord]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [EvidenceRecord.model_validate(item) for item in raw]
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(decoded, list):
            return []
        return [EvidenceRecord.model_validate(item) for item in decoded]
    return []


class _ReadWriteLock:
    """A pure-Python reader/writer lock implementation.

    Allows concurrent reads while serialising writes. Lock upgrades (acquiring a
    write lock while already holding a read lock on the same thread) are strictly
    prohibited to prevent self-deadlock and will raise a RuntimeError.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers: int = 0
        self._waiting_writers: int = 0
        self._write_owner: int | None = None
        self._write_depth: int = 0
        self._reader_threads: dict[int, int] = {}

    def __enter__(self) -> _ReadWriteLock:
        self._acquire_write()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._release_write()

    def _acquire_write(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._write_owner == tid:
                self._write_depth += 1
                return
            if self._reader_threads.get(tid, 0) > 0:
                raise RuntimeError(
                    "Cannot upgrade a read lock to a write lock on the same "
                    "thread. Acquire the write lock from the outset instead "
                    "of nesting it inside a read context."
                )
            self._waiting_writers += 1
            try:
                while self._readers > 0 or self._write_owner is not None:
                    self._cond.wait()
                self._write_owner = tid
                self._write_depth = 1
            finally:
                self._waiting_writers -= 1

    def _release_write(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._write_owner != tid:
                raise RuntimeError("Attempt to release a write lock not held by this thread.")
            self._write_depth -= 1
            if self._write_depth == 0:
                self._write_owner = None
                self._cond.notify_all()

    def read(self) -> contextmanager:
        return self._read_context()

    @contextmanager
    def _read_context(self):
        self._acquire_read()
        try:
            yield self
        finally:
            self._release_read()

    def _acquire_read(self):
        tid = threading.get_ident()
        with self._cond:
            if self._write_owner == tid:
                return
            is_reentrant = self._reader_threads.get(tid, 0) > 0
            while self._write_owner is not None or (self._waiting_writers > 0 and not is_reentrant):
                self._cond.wait()
            self._reader_threads[tid] = self._reader_threads.get(tid, 0) + 1
            self._readers += 1

    def _release_read(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._write_owner == tid:
                return
            if self._reader_threads.get(tid, 0) == 0:
                raise RuntimeError("Attempt to release a read lock not held by this thread.")
            self._reader_threads[tid] -= 1
            if self._reader_threads[tid] == 0:
                del self._reader_threads[tid]
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()


class MemoryGraph(TranscriptMixin, TraversalMixin, MutationMixin, MemoryGraphBase):
    """SQLite-backed graph memory with embedding-assisted retrieval."""

    def __init__(
        self,
        db_path: str | Path,
        embedding_model: EmbeddingModel,
        *,
        tenant_id: str = "local-default",
        dedup_similarity_threshold: float = 0.97,
        dedup_same_label_threshold: float = 0.9,
        enable_dedup: bool = True,
        recency_half_life_days: float = 30.0,
        tiered_retrieval: bool = False,
        tiered_retrieval_top_k_windows: int = 3,
        hybrid_retrieval_config: HybridRetrievalConfig | None = None,
        export_dir: str | Path | None = None,
        api_key_environment: str = "test",
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.embedding_model = embedding_model
        self.tenant_id = tenant_id.strip() or "local-default"
        self.dedup_similarity_threshold = dedup_similarity_threshold
        self.dedup_same_label_threshold = dedup_same_label_threshold
        self.enable_dedup = enable_dedup
        self.recency_half_life_days = recency_half_life_days
        self.tiered_retrieval = tiered_retrieval
        self.tiered_retrieval_top_k_windows = max(1, tiered_retrieval_top_k_windows)
        self.hybrid_retrieval_config = hybrid_retrieval_config or HybridRetrievalConfig(
            recency_half_life_days=recency_half_life_days
        )
        self.export_dir = Path(export_dir).expanduser() if export_dir is not None else self.db_path.parent / "exports"
        self.api_key_environment = api_key_environment
        # Change 5: reader-writer lock — concurrent reads, exclusive writes.
        # All existing `with self._lock` sites acquire the write (exclusive) lock.
        # Read-only paths can be migrated to `with self._lock.read()` to allow
        # concurrent access without changing the external API.
        self._lock = _ReadWriteLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()
        # Reuse a small pool of pre-configured connections instead of opening a
        # fresh one (and re-running every PRAGMA) on each operation. Created
        # after _initialize_database so the one-time WAL bootstrap/migration runs
        # on its own connection first. Pooled connections use
        # check_same_thread=False because graph ops may run on worker threads.
        self._pool = SQLiteConnectionPool(
            lambda: self._connect(check_same_thread=False),
            size=DEFAULT_POOL_SIZE,
        )
        # Only the graph that created the pool closes it; for_tenant clones share
        # it (like they share the lock) and must not close it out from under us.
        self._owns_pool = True

    def hybrid_retriever(self) -> HybridRetriever:
        return HybridRetriever(self, config=self.hybrid_retrieval_config)

    def close(self) -> None:
        """Close pooled SQLite connections owned by this graph.

        Safe to call multiple times. ``for_tenant`` clones share the owning
        graph's pool and intentionally do not close it.
        """
        pool = getattr(self, "_pool", None)
        if pool is not None and getattr(self, "_owns_pool", False):
            pool.close()

    def __enter__(self) -> MemoryGraph:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup; never raise from a finalizer.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive finalizer guard
            self.close()

    def _connect(self, timeout: float = 30.0, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Connect to the SQLite database with WAL mode and cross-process safety.

        Args:
            timeout: Connection timeout in seconds (default 30.0).
            check_same_thread: Passed through to ``sqlite3.connect``. Pooled
                connections set this to ``False`` so a connection created on one
                thread can be reused on another (the pool guarantees only one
                caller holds a given connection at a time).

        Returns:
            A configured sqlite3.Connection with WAL mode enabled.
        """
        connection = sqlite3.connect(str(self.db_path), timeout=timeout, check_same_thread=check_same_thread)
        connection.row_factory = sqlite3.Row

        # WAL mode: enables concurrent reads while maintaining single-writer safety
        connection.execute("PRAGMA journal_mode=WAL")

        # NORMAL: fsync at transaction end (vs FULL which fsyncs at each statement)
        # This balances durability with performance for multi-process access
        connection.execute("PRAGMA synchronous=NORMAL")

        # Increase busy_timeout for multi-process contention
        # 30 seconds is reasonable for cross-process locks
        connection.execute("PRAGMA busy_timeout=30000")

        # Enforce foreign key constraints
        connection.execute("PRAGMA foreign_keys=ON")

        # --- Performance tuning ---
        # Map up to 256 MB of the database file into the OS page cache.
        # Reads hit the page cache directly instead of going through read(2),
        # cutting latency by 10-25% for read-heavy workloads.
        connection.execute("PRAGMA mmap_size=268435456")

        # Keep temp tables (sort buffers, index scans) in memory instead of
        # spilling to a temp file on disk.  Safe because our temp tables are small.
        connection.execute("PRAGMA temp_store=MEMORY")

        # Allow SQLite to keep 32 MB of pages in its pager cache.
        # Negative value = number of KiB; -32000 = 32 MB.
        connection.execute("PRAGMA cache_size=-32000")

        return connection

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
        with ProcessLock(lock_path), self._lock, self._connect() as connection:
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

    def for_tenant(self, tenant_id: str) -> MemoryGraph:
        clone = object.__new__(MemoryGraph)
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
        current = self.get_ui_state(
            project=normalized_project,
            agent_id=normalized_agent,
            session_id=normalized_session,
        )
        merged = {
            "positions": positions if positions is not None else current["positions"],
            "zoom": float(zoom if zoom is not None else current["zoom"]),
            "viewport": viewport if viewport is not None else current["viewport"],
            "groups": groups if groups is not None else current["groups"],
            "collapsed_groups": collapsed_groups if collapsed_groups is not None else current["collapsed_groups"],
            "selected_nodes": selected_nodes if selected_nodes is not None else current["selected_nodes"],
        }
        with self._lock, self._pool.checkout() as connection:
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
                "UPDATE api_keys SET status = 'revoked', revoked_at = ? WHERE api_key_id = ?",
                (utc_now().isoformat(), api_key_id),
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
        # pre-checksum blobs to the canonical CRC-trailered form. It is scoped to
        # self.tenant_id and pre-filters to non-checksummed rows in SQL, so on a
        # multi-tenant DB each tenant migrates its own rows on its own open (a
        # global schema-version gate would have marked the whole DB migrated while
        # tenants that never opened stayed legacy), and a fully-migrated tenant
        # fetches nothing.
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

    def _current_embedding_model_id(self) -> str:
        model_id = getattr(self.embedding_model, "model_id", "").strip()
        if not model_id:
            model_name = str(getattr(self.embedding_model, "model_name", "") or "").strip()
            model_id = model_name or self.embedding_model.__class__.__name__
        if not model_id:
            raise ValueError("Embedding writes require a non-empty embedding_model_id.")
        return model_id

    def _embed_with_metadata(self, text: str) -> tuple[np.ndarray, str, int]:
        embedding = self.embedding_model.embed(text)
        dim = int(embedding.shape[0]) if getattr(embedding, "shape", None) else 0
        model_id = self._current_embedding_model_id()
        if dim <= 0:
            raise ValueError("Embedding writes require a positive embedding_dim.")
        return embedding, model_id, dim

    def _node_cosine_similarity(self, a: Node, b: Node) -> float | None:
        """Return cosine similarity between two nodes' stored embeddings.

        Fetches embeddings from the database.  Returns ``None`` if either
        node has no stored embedding (e.g. fast-mode or test stubs).
        """
        try:
            with self._lock, self._pool.checkout() as connection:
                row_a = connection.execute(
                    "SELECT embedding FROM nodes WHERE tenant_id = ? AND id = ?",
                    (self.tenant_id, a.id),
                ).fetchone()
                row_b = connection.execute(
                    "SELECT embedding FROM nodes WHERE tenant_id = ? AND id = ?",
                    (self.tenant_id, b.id),
                ).fetchone()
            if row_a is None or row_b is None:
                return None
            vec_a = self._decode_embedding(row_a["embedding"])
            vec_b = self._decode_embedding(row_b["embedding"])
            if vec_a is None or vec_b is None:
                return None
            return self.embedding_model.cosine_similarity(vec_a, vec_b)
        except Exception as exc:
            LOGGER.warning(
                "Failed to compute cosine similarity between nodes %s and %s: %s",
                a.id,
                b.id,
                exc,
            )
            return None

    def _backfill_transcript_storage(self, connection: sqlite3.Connection, *, batch_size: int = 100) -> None:
        pending_user_pairs: dict[str, str] = {}
        while True:
            rows = connection.execute(
                """
                SELECT id, session_id, turn_index, role, transcript_text, embedding, embedding_model_id, embedding_dim, content_hash, turn_pair_id
                FROM transcript_records
                WHERE tenant_id = ?
                  AND (
                    embedding IS NULL
                    OR embedding_model_id = ''
                    OR embedding_dim = 0
                    OR content_hash = ''
                    OR turn_pair_id = ''
                  )
                ORDER BY session_id ASC, turn_index ASC, id ASC
                LIMIT ?
                """,
                (self.tenant_id, batch_size),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                session_id = str(row["session_id"] or "")
                role = str(row["role"] or "")
                turn_pair_id = str(row["turn_pair_id"] or "").strip()
                if not turn_pair_id:
                    if role == "user":
                        turn_pair_id = str(uuid4())
                        pending_user_pairs[session_id] = turn_pair_id
                    elif role == "assistant" and pending_user_pairs.get(session_id):
                        turn_pair_id = pending_user_pairs.pop(session_id)
                    else:
                        turn_pair_id = str(uuid4())

                content_hash = str(row["content_hash"] or "").strip() or _normalized_content_hash(
                    row["transcript_text"]
                )
                if (
                    row["embedding"] is None
                    or not str(row["embedding_model_id"] or "").strip()
                    or int(row["embedding_dim"] or 0) <= 0
                ):
                    embedding, model_id, dim = self._embed_with_metadata(row["transcript_text"])
                    embedding_bytes = self._encode_embedding(embedding)
                elif self._decode_embedding(row["embedding"]) is None:
                    # Existing blob fails validation (CRC mismatch or damaged magic).
                    # Re-embed rather than wrap it — `_ensure_encoded_embedding` would
                    # otherwise launder a damaged checksummed blob into a "legacy" one.
                    embedding, model_id, dim = self._embed_with_metadata(row["transcript_text"])
                    embedding_bytes = self._encode_embedding(embedding)
                else:
                    model_id = str(row["embedding_model_id"] or "").strip()
                    dim = int(row["embedding_dim"] or 0)
                    embedding_bytes = self._ensure_encoded_embedding(row["embedding"])
                connection.execute(
                    """
                    UPDATE transcript_records
                    SET embedding = ?, embedding_model_id = ?, embedding_dim = ?, content_hash = ?, turn_pair_id = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (embedding_bytes, model_id, dim, content_hash, turn_pair_id, self.tenant_id, row["id"]),
                )

        node_rows = connection.execute(
            """
            SELECT id, content, embedding, embedding_model_id, embedding_dim
            FROM nodes
            WHERE tenant_id = ? AND (embedding_model_id = '' OR embedding_dim = 0)
            """,
            (self.tenant_id,),
        ).fetchall()
        for row in node_rows:
            if row["embedding"] is None:
                embedding, model_id, dim = self._embed_with_metadata(row["content"])
                embedding_bytes = self._encode_embedding(embedding)
            else:
                decoded = self._decode_embedding(row["embedding"])
                if decoded is None:
                    # Stored embedding is unreadable/corrupt — re-embed from content.
                    embedding, model_id, dim = self._embed_with_metadata(row["content"])
                    embedding_bytes = self._encode_embedding(embedding)
                else:
                    model_id = self._current_embedding_model_id()
                    dim = len(decoded)
                    embedding_bytes = self._ensure_encoded_embedding(row["embedding"])
            connection.execute(
                """
                UPDATE nodes
                SET embedding = ?, embedding_model_id = ?, embedding_dim = ?
                WHERE tenant_id = ? AND id = ?
                """,
                (embedding_bytes, model_id, dim, self.tenant_id, row["id"]),
            )

    def get_embedding_store_health(self) -> dict[str, Any]:
        with self._lock, self._pool.checkout() as connection:
            transcript_rows = connection.execute(
                """
                SELECT embedding_model_id, COUNT(*) AS count
                FROM transcript_records
                WHERE tenant_id = ? AND embedding_model_id != ''
                GROUP BY embedding_model_id
                ORDER BY embedding_model_id
                """,
                (self.tenant_id,),
            ).fetchall()
            node_rows = connection.execute(
                """
                SELECT embedding_model_id, COUNT(*) AS count
                FROM nodes
                WHERE tenant_id = ? AND embedding_model_id != ''
                GROUP BY embedding_model_id
                ORDER BY embedding_model_id
                """,
                (self.tenant_id,),
            ).fetchall()
            transcript_stale = int(
                connection.execute(
                    """
                SELECT COUNT(*)
                FROM transcript_records
                WHERE tenant_id = ?
                  AND (
                    embedding IS NULL
                    OR embedding_model_id = ''
                    OR embedding_dim = 0
                    OR embedding_model_id != ?
                  )
                """,
                    (self.tenant_id, self._current_embedding_model_id()),
                ).fetchone()[0]
            )
            node_stale = int(
                connection.execute(
                    """
                SELECT COUNT(*)
                FROM nodes
                WHERE tenant_id = ?
                  AND (
                    embedding IS NULL
                    OR embedding_model_id = ''
                    OR embedding_dim = 0
                    OR embedding_model_id != ?
                  )
                """,
                    (self.tenant_id, self._current_embedding_model_id()),
                ).fetchone()[0]
            )
            transcript_checksum = self._scan_embedding_integrity(connection, "transcript_records", self.tenant_id)
            node_checksum = self._scan_embedding_integrity(connection, "nodes", self.tenant_id)
            window_checksum = self._scan_embedding_integrity(connection, "context_windows", self.tenant_id)
            window_stale = int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_windows WHERE tenant_id = ? AND embedding_stale = 1",
                    (self.tenant_id,),
                ).fetchone()[0]
            )
        return {
            "current_model_id": self._current_embedding_model_id(),
            "transcript_model_counts": {str(row["embedding_model_id"]): int(row["count"]) for row in transcript_rows},
            "node_model_counts": {str(row["embedding_model_id"]): int(row["count"]) for row in node_rows},
            "transcript_stale_rows": transcript_stale,
            "node_stale_rows": node_stale,
            "window_stale_rows": window_stale,
            "mixed_models": (len(transcript_rows) > 1) or (len(node_rows) > 1),
            "transcript_checksum_failures": transcript_checksum["failures"],
            "node_checksum_failures": node_checksum["failures"],
            "window_checksum_failures": window_checksum["failures"],
            "transcript_legacy_rows": transcript_checksum["legacy"],
            "node_legacy_rows": node_checksum["legacy"],
            "window_legacy_rows": window_checksum["legacy"],
        }

    @staticmethod
    def _scan_embedding_integrity(connection: sqlite3.Connection, table: str, tenant_id: str) -> dict[str, int]:
        """Count checksum failures and un-checksummed (legacy) embedding rows for one tenant.

        A "failure" is detected corruption: a canonical blob whose CRC does not
        validate, or a blob with damaged magic that still carries a valid inner
        checksum (i.e. ``decode_embedding_blob`` rejects it). A "legacy" row is a
        genuine pre-checksum blob that decodes verbatim — never a failure. Scoped
        to ``tenant_id`` so multi-tenant databases do not leak other tenants' rows
        into a tenant-local health report.
        """
        failures = 0
        legacy = 0
        for (blob,) in connection.execute(
            f"SELECT embedding FROM {table} WHERE tenant_id = ? AND embedding IS NOT NULL",
            (tenant_id,),
        ):
            if not isinstance(blob, (bytes, bytearray)):
                continue
            blob = bytes(blob)
            decoded = decode_embedding_blob(blob)
            if is_checksummed_embedding(blob):
                if decoded is None:
                    failures += 1
            elif decoded is None:
                # Not checksummed by prefix, but decode rejects it: corruption
                # inside the magic bytes, not a clean legacy blob.
                failures += 1
            else:
                legacy += 1
        return {"failures": failures, "legacy": legacy}

    def migrate_embeddings_to_checksummed(self) -> dict[str, int]:
        """Upgrade this tenant's legacy (un-checksummed) embedding blobs in place (#71).

        Idempotent: rows already in the canonical checksummed form are skipped.
        Corrupt checksummed rows are left untouched here — :meth:`clear_corrupt_embeddings`
        plus a re-embed/recompute pass is the repair path for those. Returns per-table
        counts of rows upgraded.
        """
        with self._lock, self._pool.checkout() as connection:
            return self._migrate_embeddings_to_checksummed_conn(connection)

    def _migrate_embeddings_to_checksummed_conn(self, connection: sqlite3.Connection) -> dict[str, int]:
        upgraded = {"nodes": 0, "transcript_records": 0, "context_windows": 0}
        for table in ("nodes", "transcript_records", "context_windows"):
            # Scope to this tenant and pre-filter to non-checksummed blobs in SQL
            # (a checksummed blob always starts with EMBEDDING_BLOB_MAGIC), so a
            # multi-tenant DB only ever migrates the opening tenant's legacy rows
            # and a fully-migrated tenant fetches nothing.
            rows = connection.execute(
                f"SELECT id, embedding FROM {table} "
                "WHERE tenant_id = ? AND embedding IS NOT NULL AND substr(embedding, 1, 4) != ?",
                (self.tenant_id, EMBEDDING_BLOB_MAGIC),
            ).fetchall()
            for row in rows:
                blob = row["embedding"]
                if not isinstance(blob, (bytes, bytearray)):
                    continue
                blob = bytes(blob)
                if is_checksummed_embedding(blob) or decode_embedding_blob(blob) is None:
                    # Already canonical, or corrupt (e.g. damaged magic with an
                    # otherwise-valid trailer): never wrap a corrupt blob into a
                    # valid-looking checksummed one — clear/doctor repairs those.
                    continue
                # Guard on the exact blob observed during the scan: if another
                # process rewrote this row between fetch and update, the WHERE
                # fails to match and we neither clobber the new value nor count it.
                cursor = connection.execute(
                    f"UPDATE {table} SET embedding = ? WHERE tenant_id = ? AND id = ? AND embedding = ?",
                    (encode_embedding_blob(blob), self.tenant_id, row["id"], blob),
                )
                if cursor.rowcount and cursor.rowcount > 0:
                    upgraded[table] += 1
        return upgraded

    def clear_corrupt_embeddings(self) -> dict[str, int]:
        """Null out this tenant's embeddings whose checksum fails so they can be rebuilt.

        For ``nodes`` and ``transcript_records`` the row is set to ``embedding IS NULL``
        and repopulated by :meth:`reembed_stale_embeddings`. For ``context_windows`` the
        cached vector is dropped and the row is flagged ``embedding_stale = 1`` so it is
        recomputed by :meth:`recompute_stale_window_embeddings` or lazily on the next
        :meth:`get_window_embedding`. Returns per-table counts of rows cleared.
        """
        cleared = {"nodes": 0, "transcript_records": 0, "context_windows": 0}
        with self._lock, self._pool.checkout() as connection:
            for table in ("nodes", "transcript_records"):
                rows = connection.execute(
                    f"SELECT id, embedding FROM {table} WHERE tenant_id = ? AND embedding IS NOT NULL",
                    (self.tenant_id,),
                ).fetchall()
                for row in rows:
                    blob = row["embedding"]
                    if not isinstance(blob, (bytes, bytearray)):
                        continue
                    blob = bytes(blob)
                    # Corruption is "decode rejects a non-empty blob": a canonical
                    # blob with a bad CRC, or one with damaged magic. A genuine
                    # legacy blob decodes verbatim (non-None) and is left untouched.
                    if decode_embedding_blob(blob) is None:
                        cursor = connection.execute(
                            f"UPDATE {table} SET embedding = NULL, embedding_model_id = '', embedding_dim = 0 "
                            "WHERE tenant_id = ? AND id = ? AND embedding = ?",
                            (self.tenant_id, row["id"], blob),
                        )
                        if cursor.rowcount and cursor.rowcount > 0:
                            cleared[table] += 1
            window_rows = connection.execute(
                "SELECT id, embedding FROM context_windows WHERE tenant_id = ? AND embedding IS NOT NULL",
                (self.tenant_id,),
            ).fetchall()
            for row in window_rows:
                blob = row["embedding"]
                if not isinstance(blob, (bytes, bytearray)):
                    continue
                blob = bytes(blob)
                if decode_embedding_blob(blob) is None:
                    cursor = connection.execute(
                        "UPDATE context_windows SET embedding = NULL, embedding_stale = 1, updated_at = ? "
                        "WHERE tenant_id = ? AND id = ? AND embedding = ?",
                        (utc_now().isoformat(), self.tenant_id, row["id"], blob),
                    )
                    if cursor.rowcount and cursor.rowcount > 0:
                        cleared["context_windows"] += 1
        return cleared

    def recompute_stale_window_embeddings(self) -> int:
        """Recompute embeddings for every window flagged ``embedding_stale = 1``.

        Mirrors :meth:`reembed_stale_embeddings` for context windows (which have no
        ``embedding_model_id`` column and are rebuilt from their member nodes via
        :meth:`compute_window_embedding`). Returns the number of windows recomputed.
        """
        with self._lock, self._pool.checkout() as connection:
            window_rows = connection.execute(
                "SELECT id, updated_at FROM context_windows WHERE tenant_id = ? AND embedding_stale = 1",
                (self.tenant_id,),
            ).fetchall()
        recomputed = 0
        for window_row in window_rows:
            window_id = window_row["id"]
            observed_updated_at = window_row["updated_at"]
            embedding = self.compute_window_embedding(window_id)
            if embedding is None:
                continue
            # The lock was released during compute. Only clear the stale flag if the
            # row is still the one we read (every stale-marking path bumps
            # updated_at): if membership changed and re-staled it meanwhile, the
            # guard misses and we leave it stale rather than save an outdated vector.
            with self._lock, self._pool.checkout() as connection:
                saved = self._save_window_embedding(
                    connection, window_id, embedding, expected_updated_at=observed_updated_at
                )
            if saved:
                recomputed += 1
        return recomputed

    def reembed_stale_embeddings(self, *, batch_size: int = 100) -> dict[str, int]:
        """Re-embed stale transcript records and nodes in batch.

        This is a multi-statement batch operation that updates many rows.
        Uses ProcessLock to protect from concurrent updates across processes.
        """
        transcript_updated = 0
        node_updated = 0
        current_model_id = self._current_embedding_model_id()

        lock_path = str(self.db_path) + ".lock"
        with ProcessLock(lock_path), self._lock, self._pool.checkout() as connection:
            while True:
                transcript_rows = connection.execute(
                    """
                        SELECT id, transcript_text
                        FROM transcript_records
                        WHERE tenant_id = ?
                          AND (
                            embedding IS NULL
                            OR embedding_model_id = ''
                            OR embedding_dim = 0
                            OR embedding_model_id != ?
                          )
                        ORDER BY observed_at ASC, turn_index ASC, id ASC
                        LIMIT ?
                        """,
                    (self.tenant_id, current_model_id, batch_size),
                ).fetchall()
                if not transcript_rows:
                    break

                texts = [row["transcript_text"] for row in transcript_rows]
                embeddings = None
                try:
                    embeddings = self.embedding_model.embed_batch(texts)
                except Exception:
                    embeddings = None

                if embeddings is not None and len(embeddings) != len(texts):
                    raise ValueError(f"embed_batch returned {len(embeddings)} vectors, expected {len(texts)}")
                if embeddings is None:
                    for row in transcript_rows:
                        embedding, model_id, dim = self._embed_with_metadata(row["transcript_text"])
                        connection.execute(
                            """
                            UPDATE transcript_records
                            SET embedding = ?, embedding_model_id = ?, embedding_dim = ?, content_hash = ?
                            WHERE tenant_id = ? AND id = ?
                            """,
                            (
                                self._encode_embedding(embedding),
                                model_id,
                                dim,
                                _normalized_content_hash(row["transcript_text"]),
                                self.tenant_id,
                                row["id"],
                            ),
                        )
                        transcript_updated += 1
                else:
                    model_id = self._current_embedding_model_id()
                    for row, embedding in zip(transcript_rows, embeddings, strict=True):
                        dim = int(embedding.shape[0])
                        if dim <= 0:
                            raise ValueError("Embedding writes require a positive embedding_dim.")
                        connection.execute(
                            """
                            UPDATE transcript_records
                            SET embedding = ?, embedding_model_id = ?, embedding_dim = ?, content_hash = ?
                            WHERE tenant_id = ? AND id = ?
                            """,
                            (
                                self._encode_embedding(embedding),
                                model_id,
                                dim,
                                _normalized_content_hash(row["transcript_text"]),
                                self.tenant_id,
                                row["id"],
                            ),
                        )
                        transcript_updated += 1

            while True:
                node_rows = connection.execute(
                    """
                        SELECT id, content
                        FROM nodes
                        WHERE tenant_id = ?
                          AND (
                            embedding IS NULL
                            OR embedding_model_id = ''
                            OR embedding_dim = 0
                            OR embedding_model_id != ?
                          )
                        ORDER BY updated_at ASC, id ASC
                        LIMIT ?
                        """,
                    (self.tenant_id, current_model_id, batch_size),
                ).fetchall()
                if not node_rows:
                    break

                texts = [row["content"] for row in node_rows]
                embeddings = None
                try:
                    embeddings = self.embedding_model.embed_batch(texts)
                    if embeddings is not None and len(embeddings) != len(texts):
                        raise ValueError(f"embed_batch returned {len(embeddings)} vectors, expected {len(texts)}")
                except (AttributeError, NotImplementedError):
                    embeddings = None

                if embeddings is None:
                    for row in node_rows:
                        embedding, model_id, dim = self._embed_with_metadata(row["content"])
                        connection.execute(
                            """
                            UPDATE nodes
                            SET embedding = ?, embedding_model_id = ?, embedding_dim = ?
                            WHERE tenant_id = ? AND id = ?
                            """,
                            (
                                self._encode_embedding(embedding),
                                model_id,
                                dim,
                                self.tenant_id,
                                row["id"],
                            ),
                        )
                        node_updated += 1
                else:
                    model_id = self._current_embedding_model_id()
                    for row, embedding in zip(node_rows, embeddings, strict=True):
                        dim = int(embedding.shape[0])
                        if dim <= 0:
                            raise ValueError("Embedding writes require a positive embedding_dim.")
                        connection.execute(
                            """
                            UPDATE nodes
                            SET embedding = ?, embedding_model_id = ?, embedding_dim = ?
                            WHERE tenant_id = ? AND id = ?
                            """,
                            (
                                self._encode_embedding(embedding),
                                model_id,
                                dim,
                                self.tenant_id,
                                row["id"],
                            ),
                        )
                        node_updated += 1

        return {"transcript_rows_updated": transcript_updated, "node_rows_updated": node_updated}

    def ensure_repo(self, project: str = "", connection: sqlite3.Connection | None = None) -> str:
        name = project.strip() or "default"
        repo_id = f"{self.tenant_id}:{slugify(name)}"
        now = utc_now().isoformat()

        def _ensure(active_connection: sqlite3.Connection) -> str:
            active_connection.execute(
                """
                INSERT INTO repos (id, tenant_id, name, description, created_at, updated_at)
                VALUES (?, ?, ?, '', ?, ?)
                ON CONFLICT(tenant_id, name) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (repo_id, self.tenant_id, name, now, now),
            )
            row = active_connection.execute(
                "SELECT id FROM repos WHERE tenant_id = ? AND name = ?",
                (self.tenant_id, name),
            ).fetchone()
            return str(row["id"])

        if connection is not None:
            return _ensure(connection)
        with self._lock, self._pool.checkout() as managed_connection:
            return _ensure(managed_connection)

    def ensure_context_window(
        self,
        session_id: str = "",
        repo_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        normalized_session = session_id.strip() or "default"
        resolved_repo_id = repo_id or self.ensure_repo("default", connection=connection)
        window_id = f"{resolved_repo_id}:{slugify(normalized_session)}"
        now = utc_now().isoformat()

        def _ensure(active_connection: sqlite3.Connection) -> str:
            active_connection.execute(
                """
                INSERT INTO context_windows (
                    id, tenant_id, repo_id, session_id, title, status, node_count,
                    embedding_stale, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, '', 'active', 0, 1, ?, ?)
                ON CONFLICT(tenant_id, repo_id, session_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (window_id, self.tenant_id, resolved_repo_id, normalized_session, now, now),
            )
            row = active_connection.execute(
                """
                SELECT id FROM context_windows
                WHERE tenant_id = ? AND repo_id = ? AND session_id = ?
                """,
                (self.tenant_id, resolved_repo_id, normalized_session),
            ).fetchone()
            return str(row["id"])

        if connection is not None:
            return _ensure(connection)
        with self._lock, self._pool.checkout() as managed_connection:
            return _ensure(managed_connection)

    def resolve_window_context(
        self,
        project: str | None = None,
        session_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[str, str]:
        repo_id = self.ensure_repo(project or "default", connection=connection)
        window_id = self.ensure_context_window(session_id or "default", repo_id, connection=connection)
        return repo_id, window_id

    def update_window_node_count(self, window_id: str) -> int:
        with self._lock, self._pool.checkout() as connection:
            count = self._update_window_node_count(connection, window_id)
        return count

    def mark_window_embedding_stale(self, window_id: str) -> None:
        with self._lock, self._pool.checkout() as connection:
            self._mark_window_embedding_stale(connection, window_id)

    def _update_window_node_count(self, connection: sqlite3.Connection, window_id: str) -> int:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE tenant_id = ? AND context_window_id = ?",
                (self.tenant_id, window_id),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE context_windows
            SET node_count = ?, updated_at = ?
            WHERE tenant_id = ? AND id = ?
            """,
            (count, utc_now().isoformat(), self.tenant_id, window_id),
        )
        return count

    def _mark_window_embedding_stale(self, connection: sqlite3.Connection, window_id: str) -> None:
        connection.execute(
            """
            UPDATE context_windows
            SET embedding_stale = 1, updated_at = ?
            WHERE tenant_id = ? AND id = ?
            """,
            (utc_now().isoformat(), self.tenant_id, window_id),
        )

    def get_context_window(self, window_id: str) -> ContextWindow:
        with self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                """
                SELECT id, tenant_id, repo_id, session_id, title, status, node_count,
                       embedding_stale, created_at, updated_at, closed_at
                FROM context_windows
                WHERE tenant_id = ? AND id = ?
                """,
                (self.tenant_id, window_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Context window not found: {window_id}")
        return self._row_to_context_window(row)

    def list_context_windows(
        self,
        *,
        project: str = "",
        status: str = "",
        limit: int = 20,
    ) -> list[ContextWindow]:
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        normalized_status = status.strip().lower()
        if normalized_status and normalized_status not in {"active", "closed", "archived"}:
            raise ValueError("status must be one of: active, closed, archived.")

        query = """
            SELECT cw.id, cw.tenant_id, cw.repo_id, cw.session_id, cw.title, cw.status, cw.node_count,
                   cw.embedding_stale, cw.created_at, cw.updated_at, cw.closed_at
            FROM context_windows cw
            JOIN repos r ON r.id = cw.repo_id
            WHERE cw.tenant_id = ?
        """
        params: list[Any] = [self.tenant_id]
        if project.strip():
            query += " AND r.name = ?"
            params.append(project.strip())
        if normalized_status:
            query += " AND cw.status = ?"
            params.append(normalized_status)
        query += " ORDER BY cw.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_context_window(row) for row in rows]

    def get_context_window_edges(self, window_id: str) -> list[ContextWindowEdge]:
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT id, tenant_id, source_window_id, target_window_id, edge_type,
                       shared_entities, weight, metadata, created_at
                FROM context_window_edges
                WHERE tenant_id = ?
                  AND (source_window_id = ? OR target_window_id = ?)
                ORDER BY created_at DESC
                """,
                (self.tenant_id, window_id, window_id),
            ).fetchall()
        return [self._row_to_context_window_edge(row) for row in rows]

    def close_context_window(self, window_id: str) -> ContextWindow:
        embedding = self.compute_window_embedding(window_id)
        with self._lock, self._pool.checkout() as connection:
            if embedding is not None:
                self._save_window_embedding(connection, window_id, embedding)
            self._update_window_node_count(connection, window_id)
            now = utc_now().isoformat()
            connection.execute(
                """
                UPDATE context_windows
                SET status = 'closed', closed_at = COALESCE(closed_at, ?), updated_at = ?
                WHERE tenant_id = ? AND id = ?
                """,
                (now, now, self.tenant_id, window_id),
            )
            row = connection.execute(
                """
                SELECT id, tenant_id, repo_id, session_id, title, status, node_count,
                       embedding_stale, created_at, updated_at, closed_at
                FROM context_windows
                WHERE tenant_id = ? AND id = ?
                """,
                (self.tenant_id, window_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Context window not found: {window_id}")
        window = self._row_to_context_window(row)
        self.derive_context_window_edges(window.id, window.repo_id)
        return window

    def get_nodes_without_window(self) -> list[Node]:
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type,
                       tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                       created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ? AND context_window_id IS NULL
                ORDER BY updated_at ASC
                """,
                (self.tenant_id,),
            ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def assign_nodes_to_window(self, node_ids: list[str], window_id: str) -> int:
        if not node_ids:
            return 0
        placeholders = ", ".join("?" for _ in node_ids)
        with self._lock, self._pool.checkout() as connection:
            cursor = connection.execute(
                f"""
                UPDATE nodes
                SET context_window_id = ?
                WHERE tenant_id = ? AND context_window_id IS NULL AND id IN ({placeholders})
                """,
                (window_id, self.tenant_id, *node_ids),
            )
            updated = int(cursor.rowcount or 0)
            self._update_window_node_count(connection, window_id)
            self._mark_window_embedding_stale(connection, window_id)
        return updated

    def list_repos(self) -> list[dict[str, Any]]:
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT id, tenant_id, name, description, created_at, updated_at
                FROM repos
                WHERE tenant_id = ?
                ORDER BY updated_at DESC
                """,
                (self.tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_repo_windows(
        self,
        repo_id: str,
        *,
        exclude: str | None = None,
        include_archived: bool = False,
    ) -> list[ContextWindow]:
        query = """
            SELECT id, tenant_id, repo_id, session_id, title, status, node_count,
                   embedding_stale, created_at, updated_at, closed_at
            FROM context_windows
            WHERE tenant_id = ? AND repo_id = ?
        """
        params: list[Any] = [self.tenant_id, repo_id]
        if exclude:
            query += " AND id != ?"
            params.append(exclude)
        if not include_archived:
            query += " AND status != 'archived'"
        query += " ORDER BY updated_at DESC"
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_context_window(row) for row in rows]

    def get_window_nodes(self, window_id: str, node_types: list[NodeType] | None = None) -> list[Node]:
        query = """
            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type,
                   tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                   created_at, updated_at, access_count, tenant_id
            FROM nodes
            WHERE tenant_id = ? AND context_window_id = ?
        """
        params: list[Any] = [self.tenant_id, window_id]
        if node_types:
            placeholders = ", ".join("?" for _ in node_types)
            query += f" AND node_type IN ({placeholders})"
            params.extend(node_type.value for node_type in node_types)
        query += " ORDER BY updated_at DESC"
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_node(row) for row in rows]

    def compute_window_embedding(self, window_id: str) -> np.ndarray | None:
        meaningful_types = [
            NodeType.DECISION,
            NodeType.FACT,
            NodeType.ENTITY,
            NodeType.PREFERENCE,
            NodeType.CONCEPT,
        ]
        nodes = self.get_window_nodes(window_id, node_types=meaningful_types)
        if not nodes:
            return None

        type_rank = {
            NodeType.DECISION: 0,
            NodeType.FACT: 1,
            NodeType.ENTITY: 2,
            NodeType.PREFERENCE: 3,
            NodeType.CONCEPT: 4,
        }
        nodes.sort(
            key=lambda node: (type_rank.get(node.node_type, 99), -node.updated_at.timestamp(), node.label.lower())
        )
        if len(nodes) > 100:
            LOGGER.warning(
                "context_window_embedding_truncated", extra={"window_id": window_id, "node_count": len(nodes)}
            )
            nodes = nodes[:100]
        window_text = " | ".join(f"{node.label}: {node.content}" for node in nodes)
        if not window_text.strip():
            return None
        return self.embedding_model.embed(window_text[:12000])

    def get_window_embedding(self, window_id: str) -> np.ndarray | None:
        observed_updated_at: str | None = None
        with self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                """
                SELECT embedding, embedding_stale, updated_at
                FROM context_windows
                WHERE tenant_id = ? AND id = ?
                """,
                (self.tenant_id, window_id),
            ).fetchone()
            if row is None:
                return None
            observed_updated_at = row["updated_at"]
            if row["embedding"] is not None and not bool(row["embedding_stale"]):
                decoded = self._decode_embedding(row["embedding"])
                if decoded is not None:
                    return decoded
                # Corrupt cached embedding (failed checksum): fall through to
                # recompute and re-save a fresh checksummed vector (issue #71).

        embedding = self.compute_window_embedding(window_id)
        if embedding is None:
            return None
        with self._lock, self._pool.checkout() as connection:
            # The lock was released during compute. Persist the recompute only if
            # the row is unchanged since we read it; if another writer re-staled it
            # or changed membership meanwhile (which bumps updated_at), skip the
            # save and return our value — the newer state is recomputed on the next
            # read rather than being clobbered here. (issue #71 review)
            self._save_window_embedding(connection, window_id, embedding, expected_updated_at=observed_updated_at)
        return embedding

    def _save_window_embedding(
        self,
        connection: sqlite3.Connection,
        window_id: str,
        embedding: np.ndarray,
        *,
        expected_updated_at: str | None = None,
    ) -> bool:
        """Persist a (re)computed window embedding and clear the stale flag.

        When ``expected_updated_at`` is supplied the write is optimistic: it only
        lands if the row's ``updated_at`` is still the value observed before the
        lock was released for ``compute_window_embedding``. A concurrent
        membership change or re-stale bumps ``updated_at``, so this prevents
        clobbering newer state with a vector computed from a stale read. Returns
        ``True`` if a row was written. (issue #71 review)
        """
        if expected_updated_at is None:
            cursor = connection.execute(
                """
                UPDATE context_windows
                SET embedding = ?, embedding_stale = 0, updated_at = ?
                WHERE tenant_id = ? AND id = ?
                """,
                (self._encode_embedding(embedding), utc_now().isoformat(), self.tenant_id, window_id),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE context_windows
                SET embedding = ?, embedding_stale = 0, updated_at = ?
                WHERE tenant_id = ? AND id = ? AND updated_at = ?
                """,
                (
                    self._encode_embedding(embedding),
                    utc_now().isoformat(),
                    self.tenant_id,
                    window_id,
                    expected_updated_at,
                ),
            )
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def extract_window_entities(self, window_id: str) -> list[dict[str, str]]:
        nodes = self.get_window_nodes(
            window_id,
            node_types=[NodeType.ENTITY, NodeType.FACT, NodeType.DECISION, NodeType.PREFERENCE],
        )
        return [
            {
                "label": node.label,
                "node_type": node.node_type.value,
                "content": node.content,
            }
            for node in nodes
        ]

    def create_context_window_edge(
        self,
        *,
        source_window_id: str,
        target_window_id: str,
        edge_type: str,
        shared_entities: list[str] | None = None,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> ContextWindowEdge:
        entities = sorted({entity.strip().lower() for entity in (shared_entities or []) if entity.strip()})
        edge = ContextWindowEdge(
            tenant_id=self.tenant_id,
            source_window_id=source_window_id,
            target_window_id=target_window_id,
            edge_type=edge_type,
            shared_entities=entities,
            weight=max(0.0, min(1.0, weight)),
            metadata=metadata or {},
        )
        with self._lock, self._pool.checkout() as connection:
            connection.execute(
                """
                INSERT INTO context_window_edges (
                    id, tenant_id, source_window_id, target_window_id, edge_type,
                    shared_entities, weight, metadata, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, source_window_id, target_window_id, edge_type, shared_entities)
                DO UPDATE SET weight = MAX(context_window_edges.weight, excluded.weight),
                              metadata = excluded.metadata
                """,
                (
                    edge.id,
                    edge.tenant_id,
                    edge.source_window_id,
                    edge.target_window_id,
                    edge.edge_type,
                    json.dumps(edge.shared_entities, sort_keys=True),
                    edge.weight,
                    _encode_metadata(edge.metadata),
                    edge.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT id, tenant_id, source_window_id, target_window_id, edge_type,
                       shared_entities, weight, metadata, created_at
                FROM context_window_edges
                WHERE tenant_id = ? AND source_window_id = ? AND target_window_id = ?
                  AND edge_type = ? AND shared_entities = ?
                """,
                (
                    self.tenant_id,
                    source_window_id,
                    target_window_id,
                    edge_type,
                    json.dumps(edge.shared_entities, sort_keys=True),
                ),
            ).fetchone()
        return self._row_to_context_window_edge(row)

    def derive_context_window_edges(self, window_id: str, repo_id: str) -> list[ContextWindowEdge]:
        current_entities = self.extract_window_entities(window_id)
        if not current_entities:
            return []

        current_by_label = {
            entity["label"].strip().lower(): entity for entity in current_entities if entity["label"].strip()
        }
        if not current_by_label:
            return []

        created_edges: list[ContextWindowEdge] = []
        other_windows = self.get_repo_windows(repo_id, exclude=window_id)
        if len(other_windows) > 200:
            other_windows = other_windows[:200]

        for other_window in other_windows:
            other_entities = self.extract_window_entities(other_window.id)
            other_by_label = {
                entity["label"].strip().lower(): entity for entity in other_entities if entity["label"].strip()
            }
            overlap = set(current_by_label) & set(other_by_label)
            if not overlap:
                continue

            has_conflict = any(
                normalize_text(current_by_label[label]["content"]) != normalize_text(other_by_label[label]["content"])
                for label in overlap
            )
            edge_type = "supersedes" if has_conflict else "entity_overlap"
            denominator = max(len(current_by_label), len(other_by_label), 1)
            created_edges.append(
                self.create_context_window_edge(
                    source_window_id=other_window.id,
                    target_window_id=window_id,
                    edge_type=edge_type,
                    shared_entities=sorted(overlap),
                    weight=len(overlap) / denominator,
                )
            )

        previous_window = next(iter(other_windows), None)
        if previous_window is not None:
            created_edges.append(
                self.create_context_window_edge(
                    source_window_id=previous_window.id,
                    target_window_id=window_id,
                    edge_type="temporal_sequence",
                    shared_entities=[],
                    weight=1.0,
                )
            )

        LOGGER.info(
            "window_edges_derived",
            extra={
                "window_id": window_id,
                "repo_id": repo_id,
                "edges_created": len(created_edges),
            },
        )
        return created_edges

    def get_node(self, node_id: str) -> Node:
        with self._lock, self._pool.checkout() as connection:
            row = self._fetch_node_row(connection, node_id)
            if row is None:
                raise ValueError(f"Node not found: {node_id}")
            return self._row_to_node(row)

    def get_node_history(self, *, node_id: str, max_depth: int = 2) -> NodeHistoryResult:
        node = self.get_node(node_id)
        related = self.get_related(node_id=node_id, max_depth=max_depth)
        related_nodes = [item for item in related.nodes if item.id != node_id]
        return NodeHistoryResult(node=node, related_nodes=related_nodes, edges=related.edges)

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
        include_invalidated: bool = False,
        as_of: datetime | None = None,
    ) -> SubgraphResult:
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._pool.checkout() as connection:
            node_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags,
                       source_prompt, metadata, evidence_records, valid_from, valid_to, created_at,
                       updated_at, access_count, embedding, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                """,
                (self.tenant_id,),
            ).fetchall()

            total_nodes = len(node_rows)
            if total_nodes == 0:
                return SubgraphResult(query=query, total_nodes_in_graph=0)

            active_session_id = _retrieval_session_scope(
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )

            target_types = {t.lower() for t in node_types} if node_types else None
            target_tags = {t.lower() for t in tags} if tags else None

            candidates: list[Node] = []
            embeddings_by_id: dict[str, np.ndarray] = {}
            for row in node_rows:
                node = self._row_to_node(row)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=active_session_id):
                    continue
                if target_types and node.node_type.value.lower() not in target_types:
                    continue
                if target_tags:
                    node_tags = {t.lower() for t in node.tags}
                    if not any(tag in node_tags for tag in target_tags):
                        continue
                candidates.append(node)
                if row["embedding"] is not None:
                    decoded_embedding = self._decode_embedding(row["embedding"])
                    if decoded_embedding is not None:
                        embeddings_by_id[node.id] = decoded_embedding

            # Apply temporal validity filtering
            candidates = _filter_valid_nodes(
                candidates,
                include_invalidated=include_invalidated,
                as_of=as_of,
            )
            valid_candidate_ids = {n.id for n in candidates}
            embeddings_by_id = {nid: emb for nid, emb in embeddings_by_id.items() if nid in valid_candidate_ids}

            if not candidates:
                return SubgraphResult(query=query, total_nodes_in_graph=total_nodes)

            if query.strip():
                expanded_query = self._expand_query_aliases(query)
                query_embedding = self.embedding_model.embed(expanded_query)

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
                selected_ids = {node.id for node in selected_nodes}
                expanded_ids = set(selected_ids)
                current_frontier = set(selected_ids)

                for _ in range(max_depth):
                    if not current_frontier:
                        break
                    next_frontier = set()
                    edges = self._fetch_edges_for_nodes(connection, list(current_frontier))
                    for edge in edges:
                        neighbor_id = edge.target_id if edge.source_id in current_frontier else edge.source_id
                        if neighbor_id not in expanded_ids:
                            expanded_ids.add(neighbor_id)
                            next_frontier.add(neighbor_id)
                    current_frontier = next_frontier

                if len(expanded_ids) > len(selected_ids):
                    missing_ids = expanded_ids - selected_ids
                    if missing_ids:
                        placeholders = ", ".join("?" for _ in missing_ids)
                        missing_rows = connection.execute(
                            f"""
                            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags,
                                   source_prompt, metadata, evidence_records, valid_from, valid_to, created_at,
                                   updated_at, access_count, embedding, tenant_id
                            FROM nodes
                            WHERE tenant_id = ? AND id IN ({placeholders})
                            """,
                            (self.tenant_id, *missing_ids),
                        ).fetchall()
                        for row in missing_rows:
                            selected_nodes.append(self._row_to_node(row))

            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(connection, selected_ids)
            self._increment_access_counts(connection, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="aggregate",
                query=query,
                total_nodes_in_graph=total_nodes,
            )

    def list_recent_nodes(
        self,
        limit: int = 10,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> list[Node]:
        limit = max(1, limit)
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                       created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (self.tenant_id,),
            ).fetchall()
            selected: list[Node] = []
            for row in rows:
                node = self._row_to_node(row)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                    continue
                selected.append(node)
                if len(selected) >= limit:
                    break
            return selected

    def list_context_scopes(self) -> ContextScopeResult:
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT agent_id, project, session_id
                FROM nodes
                WHERE tenant_id = ?
                """,
                (self.tenant_id,),
            ).fetchall()
        agent_ids = sorted({str(row["agent_id"]).strip() for row in rows if str(row["agent_id"]).strip()})
        projects = sorted({str(row["project"]).strip() for row in rows if str(row["project"]).strip()})
        session_ids = sorted({str(row["session_id"]).strip() for row in rows if str(row["session_id"]).strip()})
        return ContextScopeResult(agent_ids=agent_ids, projects=projects, session_ids=session_ids)

    def edge_quality_report(
        self,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Return an audit report of edge quality for the current tenant.

        Counts per edge type, average ``edge_confidence`` per type, and the
        top-10 highest- and lowest-confidence edges for each type.
        ``edge_confidence`` is read from the edge ``metadata`` JSON field.
        Edges without a stored confidence are treated as confidence = 1.0
        (they were created before this feature or via the explicit
        ``store_edge`` tool, which implies intentional creation).
        """
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.source_id, e.target_id, e.relationship, e.weight,
                       e.metadata, e.created_at,
                       sn.label AS source_label, tn.label AS target_label
                FROM edges AS e
                LEFT JOIN nodes AS sn ON sn.id = e.source_id AND sn.tenant_id = e.tenant_id
                LEFT JOIN nodes AS tn ON tn.id = e.target_id AND tn.tenant_id = e.tenant_id
                WHERE e.tenant_id = ?
                ORDER BY e.relationship ASC, e.created_at ASC
                """,
                (self.tenant_id,),
            ).fetchall()

        # Optionally filter by scope
        agent_id.strip().lower()
        project.strip().lower()
        session_id.strip().lower()

        by_type: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            meta = _decode_metadata(row["metadata"])
            confidence = float(meta.get("edge_confidence", 1.0))
            rel = str(row["relationship"])
            entry = {
                "id": row["id"],
                "source_id": row["source_id"],
                "target_id": row["target_id"],
                "source_label": row["source_label"] or row["source_id"],
                "target_label": row["target_label"] or row["target_id"],
                "relationship": rel,
                "weight": float(row["weight"]),
                "edge_confidence": confidence,
                "created_at": row["created_at"],
            }
            by_type.setdefault(rel, []).append(entry)

        report: dict[str, Any] = {"by_type": {}}
        total_edges = 0
        for rel, entries in sorted(by_type.items()):
            confidences = [e["edge_confidence"] for e in entries]
            avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
            sorted_asc = sorted(entries, key=lambda e: e["edge_confidence"])
            sorted_desc = sorted(entries, key=lambda e: e["edge_confidence"], reverse=True)
            report["by_type"][rel] = {
                "count": len(entries),
                "avg_confidence": round(avg_conf, 4),
                "top_10_highest": sorted_desc[:10],
                "top_10_lowest": sorted_asc[:10],
            }
            total_edges += len(entries)

        report["total_edges"] = total_edges
        report["total_edge_types"] = len(by_type)
        return report

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

        with self._lock, self._pool.checkout() as connection:
            node_rows = connection.execute(
                """
                SELECT id, label, content, node_type, tags, source_prompt, metadata,
                       created_at, updated_at, access_count
                FROM nodes
                WHERE tenant_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (self.tenant_id,),
            ).fetchall()
            edge_rows = connection.execute(
                """
                SELECT id, source_id, target_id, relationship, weight, metadata, created_at
                FROM edges
                WHERE tenant_id = ?
                ORDER BY created_at ASC
                """,
                (self.tenant_id,),
            ).fetchall()

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

        nodes = [self._row_to_node(row) for row in node_rows]
        edges = [self._row_to_edge(row) for row in edge_rows]

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
        try:
            from pyvis.network import Network
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyvis is not installed. Install the project dependencies again.") from exc

        repo_id = self.ensure_repo(project or "default")
        windows = self.get_repo_windows(repo_id, include_archived=True)
        window_ids = {window.id for window in windows}
        with self._lock, self._pool.checkout() as connection:
            edge_rows = (
                connection.execute(
                    """
                SELECT id, tenant_id, source_window_id, target_window_id, edge_type,
                       shared_entities, weight, metadata, created_at
                FROM context_window_edges
                WHERE tenant_id = ?
                  AND source_window_id IN ({})
                  AND target_window_id IN ({})
                ORDER BY created_at ASC
                """.format(
                        ", ".join("?" for _ in window_ids) or "NULL",
                        ", ".join("?" for _ in window_ids) or "NULL",
                    ),
                    (self.tenant_id, *window_ids, *window_ids),
                ).fetchall()
                if window_ids
                else []
            )
        edges = [self._row_to_context_window_edge(row) for row in edge_rows]

        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-window-graph-{timestamp}.html"
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

        status_colors = {
            "active": "#34d399",
            "closed": "#38bdf8",
            "archived": "#94a3b8",
        }
        edge_colors = {
            "entity_overlap": "#38bdf8",
            "supersedes": "#fb7185",
            "temporal_sequence": "#94a3b8",
            "continuation": "#fbbf24",
            "shared_scope": "#34d399",
        }

        for window in windows:
            connected_edges = [
                edge for edge in edges if edge.source_window_id == window.id or edge.target_window_id == window.id
            ]
            label = window.title or window.session_id or window.id
            title_lines = [
                f"<b>{label}</b>",
                f"Window: {window.id}",
                f"Repo: {window.repo_id}",
                f"Status: {window.status}",
                f"Session: {window.session_id}",
                f"Nodes: {window.node_count}",
                f"Connected Windows: {len(connected_edges)}",
                f"Created: {window.created_at.isoformat()}",
                f"Updated: {window.updated_at.isoformat()}",
            ]
            network.add_node(
                window.id,
                label=label,
                title="<br>".join(title_lines),
                color=status_colors.get(window.status, "#94a3b8"),
                shape="dot",
                size=18 + min(max(window.node_count, 0), 50),
            )

        for edge in edges:
            shared = ", ".join(edge.shared_entities)
            network.add_edge(
                edge.source_window_id,
                edge.target_window_id,
                label=edge.edge_type,
                title=f"weight={edge.weight}" + (f"<br>shared={shared}" if shared else ""),
                value=max(edge.weight, 0.1),
                color=edge_colors.get(edge.edge_type, "#94a3b8"),
                arrows="to",
            )

        destination.write_text(network.generate_html(notebook=False), encoding="utf-8")
        return destination

    def export_graph_backup(self, *, output_path: str | Path | None = None) -> BackupResult:
        with self._lock, self._pool.checkout() as connection:
            snapshot = self._build_backup_snapshot(connection)

        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-backup-{timestamp}.json"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)

        destination.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        result = BackupResult(
            output_path=str(destination),
            tenant_id=self.tenant_id,
            schema_version=SCHEMA_VERSION,
            node_count=len(snapshot["nodes"]),
            edge_count=len(snapshot["edges"]),
        )
        self.emit_audit_event(
            event_type="export.created",
            resource_type="backup",
            resource_id=result.output_path,
            action="export",
            metadata={"format": "backup", "node_count": result.node_count, "edge_count": result.edge_count},
        )
        return result

    def export_abhi(
        self,
        *,
        output_path: str | Path | None = None,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        scope: str = "all",
        since_date: str = "",
        include_embeddings: bool = True,
        passphrase: str = "",
        redact_patterns: list[str] | None = None,
        sign: bool = False,
        signing_key_dir: str | Path | None = None,
        include_low_confidence_edges: bool = False,
        low_confidence_threshold: float = 0.7,
    ) -> AbhiExportResult:
        with self._lock, self._pool.checkout() as connection:
            snapshot = self._build_backup_snapshot(connection, include_embeddings=include_embeddings)
        snapshot["ui"] = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-memory-{timestamp}.abhi"
        else:
            destination = Path(output_path).expanduser()
        result = write_abhi_document(
            snapshot,
            output_path=destination,
            passphrase=passphrase,
            scope=scope,
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            since_date=since_date,
            include_embeddings=include_embeddings,
            redact_patterns=redact_patterns,
            sign=sign,
            signing_key_dir=signing_key_dir,
            include_low_confidence_edges=include_low_confidence_edges,
            low_confidence_threshold=low_confidence_threshold,
        )
        self.emit_audit_event(
            event_type="export.created",
            resource_type="abhi_export",
            resource_id=result.output_path,
            action="export",
            metadata={
                "format": "abhi",
                "node_count": result.node_count,
                "edge_count": result.edge_count,
                "encrypted": result.encrypted,
            },
        )
        return result

    def get_graph_snapshot(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._pool.checkout() as connection:
            snapshot = self._build_backup_snapshot(connection)
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
        normalized_retrieval_mode = retrieval_mode.strip().lower()
        if normalized_mode not in {"prime", "query", "graph"}:
            raise ValidationFailure("mode must be one of: prime, query, graph.")
        if normalized_format not in {"markdown", "json", "both"}:
            raise ValidationFailure("format must be one of: markdown, json, both.")
        if normalized_audience not in {"llm", "human"}:
            raise ValidationFailure("audience must be one of: llm, human.")
        normalized_retrieval_mode = {"replay": "verbatim", "fusion": "hybrid"}.get(
            normalized_retrieval_mode, normalized_retrieval_mode
        )
        if normalized_retrieval_mode not in {"graph", "verbatim", "hybrid"}:
            raise ValidationFailure("retrieval_mode must be one of: graph, verbatim, hybrid.")
        if normalized_mode == "query" and not query.strip():
            raise ValidationFailure("query is required when mode='query'.")
        if normalized_mode != "query" and normalized_retrieval_mode != "graph":
            raise ValidationFailure("retrieval_mode is only supported when mode='query'.")

        replay_hits: list[ReplayHit] = []
        if normalized_mode == "prime":
            selected = self.prime_context(
                project=project, agent_id=agent_id, session_id=session_id, max_nodes=max_nodes
            )
            selected_nodes = selected.nodes
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
            with self._lock, self._pool.checkout() as connection:
                node_rows = connection.execute(
                    """
                    SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata,
                           evidence_records, valid_from, valid_to, created_at, updated_at, access_count, tenant_id
                    FROM nodes
                    WHERE tenant_id = ?
                    ORDER BY updated_at DESC, created_at DESC
                    """,
                    (self.tenant_id,),
                ).fetchall()
                edge_rows = connection.execute(
                    """
                    SELECT id, source_id, target_id, relationship, weight, metadata, created_at
                    FROM edges
                    WHERE tenant_id = ?
                    ORDER BY created_at ASC
                    """,
                    (self.tenant_id,),
                ).fetchall()
            selected_nodes = [
                node
                for row in node_rows
                for node in [self._row_to_node(row)]
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            ]
            selected_edges = [self._row_to_edge(row) for row in edge_rows] if include_edges else []
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
        result = export_context_bundle_files(
            bundle,
            output_path=output_path,
            export_dir=self.export_dir,
            format=normalized_format,
            include_edges=include_edges,
            include_timestamps=include_timestamps,
            include_source_prompt=include_source_prompt,
        )
        self.emit_audit_event(
            event_type="export.created",
            resource_type="context_bundle",
            resource_id=result.markdown_path or result.json_path or "",
            action="export",
            metadata={
                "format": normalized_format,
                "mode": normalized_mode,
                "node_count": result.node_count,
                "edge_count": result.edge_count,
            },
        )
        return result

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
        with self._lock, self._pool.checkout() as connection:
            node_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata,
                       evidence_records, valid_from, valid_to, created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                ORDER BY updated_at DESC, created_at DESC
                """,
                (self.tenant_id,),
            ).fetchall()
            edge_rows = connection.execute(
                """
                SELECT id, source_id, target_id, relationship, weight, metadata, created_at, tenant_id
                FROM edges
                WHERE tenant_id = ?
                ORDER BY created_at ASC
                """,
                (self.tenant_id,),
            ).fetchall()
        selected_nodes = [
            node
            for row in node_rows
            for node in [self._row_to_node(row)]
            if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
        ]
        selected_ids = {node.id for node in selected_nodes}
        selected_edges = [
            self._row_to_edge(row)
            for row in edge_rows
            if row["source_id"] in selected_ids and row["target_id"] in selected_ids
        ]
        node_by_id = {node.id: node for node in selected_nodes}
        files_written: list[str] = []
        for node in selected_nodes:
            project_dir = slugify(node.project or project or "default")
            node_type_dir = slugify(node.node_type.value)
            destination = root / project_dir / node_type_dir / vault_filename(node)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                render_node_document(node, selected_edges, node_by_id),
                encoding="utf-8",
            )
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
        root = Path(root_path).expanduser()
        documents = iter_vault_documents(root)
        result = MarkdownVaultImportResult(root_path=str(root), tenant_id=self.tenant_id)
        if not documents:
            return result

        with self._lock, self._pool.checkout() as connection:
            nodes_by_id = {
                node.id: node
                for node in self._fetch_nodes_by_ids(
                    connection,
                    [str(document.frontmatter["node_id"]) for document in documents],
                )
            }
            label_index: dict[str, Node] = {}
            all_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata,
                       evidence_records, valid_from, valid_to, created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                """,
                (self.tenant_id,),
            ).fetchall()
            for row in all_rows:
                node = self._row_to_node(row)
                label_index.setdefault(node.label.strip().lower(), node)
                nodes_by_id.setdefault(node.id, node)

            imported_id_map: dict[str, str] = {}
            for document in documents:
                original_node_id = str(document.frontmatter.get("node_id", "")).strip()
                node, created = self._upsert_vault_document(connection, document)
                nodes_by_id[node.id] = node
                if original_node_id:
                    imported_id_map[original_node_id] = node.id
                    nodes_by_id[original_node_id] = node
                label_index[node.label.strip().lower()] = node
                if created:
                    result.nodes_created += 1
                else:
                    result.nodes_updated += 1

            for document in documents:
                source_node_id = str(document.frontmatter.get("node_id", "")).strip()
                source_node = nodes_by_id.get(imported_id_map.get(source_node_id, source_node_id))
                if source_node is None:
                    result.conflicts.append(f"Missing source node for document {document.path}.")
                    continue
                for relation in document.relations:
                    target_lookup_id = imported_id_map.get(relation.target_node_id, relation.target_node_id)
                    target_node = nodes_by_id.get(target_lookup_id) if target_lookup_id else None
                    if target_node is None and relation.target_label:
                        target_node = label_index.get(relation.target_label.strip().lower())
                    if target_node is None and relation.target_label:
                        target_node = self._insert_vault_stub_node(
                            connection,
                            label=relation.target_label,
                            project=source_node.project,
                            agent_id=source_node.agent_id,
                            session_id=source_node.session_id,
                        )
                        nodes_by_id[target_node.id] = target_node
                        label_index[target_node.label.strip().lower()] = target_node
                        result.stub_nodes_created += 1
                    if target_node is None:
                        result.conflicts.append(
                            f"Could not resolve relation target '{relation.target_label}' in {document.path.name}."
                        )
                        continue
                    if relation.deleted:
                        if self._delete_edge_record(
                            connection,
                            source_id=source_node.id,
                            target_id=target_node.id,
                            relationship=relation.relationship,
                        ):
                            result.edges_deleted += 1
                        continue
                    if (
                        self._find_existing_edge(
                            connection,
                            source_id=source_node.id,
                            target_id=target_node.id,
                            relationship=relation.relationship,
                        )
                        is None
                    ):
                        self._insert_edge_record(
                            connection,
                            source_id=source_node.id,
                            target_id=target_node.id,
                            relationship=relation.relationship,
                        )
                        result.edges_created += 1
        return result

    def import_graph_backup(self, *, input_path: str | Path) -> ImportResult:
        source = Path(input_path).expanduser()
        snapshot = json.loads(source.read_text(encoding="utf-8"))

        with self._lock, self._pool.checkout() as connection:
            snapshot_tenant = str(snapshot.get("tenant_id") or self.tenant_id)
            result = ImportResult(
                input_path=str(source),
                tenant_id=self.tenant_id,
                schema_version=int(snapshot.get("schema_version", 1)),
            )
            for raw_repo in snapshot.get("repos", []):
                self._upsert_snapshot_repo(connection, {**raw_repo, "tenant_id": self.tenant_id})

            for raw_window in snapshot.get("context_windows", []):
                self._upsert_snapshot_context_window(connection, {**raw_window, "tenant_id": self.tenant_id})

            for raw_node in snapshot.get("nodes", []):
                raw_node = {**raw_node, "tenant_id": raw_node.get("tenant_id") or snapshot_tenant}
                if raw_node["tenant_id"] != self.tenant_id:
                    raw_node["tenant_id"] = self.tenant_id
                if self._fetch_node_row(connection, raw_node["id"]) is None:
                    self._insert_snapshot_node(connection, raw_node)
                    result.nodes_created += 1
                else:
                    self._update_snapshot_node(connection, raw_node)
                    result.nodes_updated += 1

            for raw_edge in snapshot.get("edges", []):
                raw_edge = {**raw_edge, "tenant_id": raw_edge.get("tenant_id") or snapshot_tenant}
                if raw_edge["tenant_id"] != self.tenant_id:
                    raw_edge["tenant_id"] = self.tenant_id
                if self._fetch_edge_row(connection, raw_edge["id"]) is None:
                    self._insert_snapshot_edge(connection, raw_edge)
                    result.edges_created += 1
                else:
                    self._update_snapshot_edge(connection, raw_edge)
                    result.edges_updated += 1

            for raw_window_edge in snapshot.get("context_window_edges", []):
                self._upsert_snapshot_context_window_edge(
                    connection,
                    {**raw_window_edge, "tenant_id": self.tenant_id},
                )

            for raw_window in snapshot.get("context_windows", []):
                window_id = str(raw_window.get("id", "")).strip()
                if window_id:
                    self._update_window_node_count(connection, window_id)
                    self._mark_window_embedding_stale(connection, window_id)
                    self._upsert_snapshot_context_window(connection, {**raw_window, "tenant_id": self.tenant_id})
        self.save_ui_state(
            positions=snapshot.get("ui", {}).get("positions", {}),
            zoom=snapshot.get("ui", {}).get("zoom", 1.0),
            viewport=snapshot.get("ui", {}).get("viewport", {"center_x": 0, "center_y": 0}),
            groups=snapshot.get("ui", {}).get("groups", []),
            collapsed_groups=snapshot.get("ui", {}).get("collapsed_groups", []),
            selected_nodes=snapshot.get("ui", {}).get("selected_nodes", []),
        )
        self.emit_audit_event(
            event_type="import.completed",
            resource_type="backup",
            resource_id=str(source),
            action="import",
            metadata={
                "format": "backup",
                "nodes_created": result.nodes_created,
                "nodes_updated": result.nodes_updated,
                "edges_created": result.edges_created,
                "edges_updated": result.edges_updated,
            },
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

    def import_abhi(
        self,
        *,
        input_path: str | Path,
        passphrase: str = "",
        namespace: str = "",
        merge_strategy: str = "skip-existing",
        verify_signature: bool = False,
        read_only: bool = False,
        reembed_on_mismatch: bool = False,
    ) -> AbhiImportResult:
        source = Path(input_path).expanduser()
        document = load_abhi_document(source, passphrase=passphrase)
        validation = validate_abhi_document(document, input_path=source)
        if not validation.valid:
            raise ValidationFailure("Invalid .abhi file: " + "; ".join(validation.errors))
        if verify_signature:
            validate_abhi_signature(document)
        executed_actions = dispatch_abhi_event(document, event_name="on_import", persist=False, input_path=source)
        source_model_id = str(document.get("manifest", {}).get("embedding_model_id", "")).strip()
        current_model_id = self._current_embedding_model_id()
        snapshot = abhi_to_snapshot(
            document,
            fallback_tenant_id=self.tenant_id,
            namespace=namespace,
            read_only=read_only,
            reembed_on_import=bool(reembed_on_mismatch and source_model_id and source_model_id != current_model_id),
        )

        with self._lock, self._pool.checkout() as connection:
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
            for raw_transcript in snapshot.get("transcripts", []):
                existing_transcript = self._fetch_transcript_row(connection, raw_transcript["id"])
                if existing_transcript is None:
                    self._insert_snapshot_transcript(connection, raw_transcript)
                elif merge_strategy in {"overwrite", "branch"}:
                    self._update_snapshot_transcript(connection, raw_transcript)

            for raw_repo in snapshot.get("repos", []):
                self._upsert_snapshot_repo(connection, {**raw_repo, "tenant_id": self.tenant_id})

            for raw_window in snapshot.get("context_windows", []):
                self._upsert_snapshot_context_window(connection, {**raw_window, "tenant_id": self.tenant_id})

            for raw_node in snapshot.get("nodes", []):
                raw_node = {**raw_node, "tenant_id": raw_node.get("tenant_id") or snapshot_tenant}
                if raw_node["tenant_id"] != self.tenant_id:
                    raw_node["tenant_id"] = self.tenant_id
                if self._fetch_node_row(connection, raw_node["id"]) is None:
                    self._insert_snapshot_node(connection, raw_node)
                    result.nodes_created += 1
                elif merge_strategy in {"overwrite", "branch"}:
                    self._update_snapshot_node(connection, raw_node)
                    result.nodes_updated += 1

            for raw_edge in snapshot.get("edges", []):
                raw_edge = {**raw_edge, "tenant_id": raw_edge.get("tenant_id") or snapshot_tenant}
                if raw_edge["tenant_id"] != self.tenant_id:
                    raw_edge["tenant_id"] = self.tenant_id
                if self._fetch_edge_row(connection, raw_edge["id"]) is None:
                    self._insert_snapshot_edge(connection, raw_edge)
                    result.edges_created += 1
                elif merge_strategy in {"overwrite", "branch"}:
                    self._update_snapshot_edge(connection, raw_edge)
                    result.edges_updated += 1

            for raw_window_edge in snapshot.get("context_window_edges", []):
                self._upsert_snapshot_context_window_edge(
                    connection,
                    {**raw_window_edge, "tenant_id": self.tenant_id},
                )

            for raw_window in snapshot.get("context_windows", []):
                window_id = str(raw_window.get("id", "")).strip()
                if window_id:
                    self._update_window_node_count(connection, window_id)
                    self._mark_window_embedding_stale(connection, window_id)
                    self._upsert_snapshot_context_window(connection, {**raw_window, "tenant_id": self.tenant_id})
        self.save_ui_state(
            positions=snapshot.get("ui", {}).get("positions", {}),
            zoom=snapshot.get("ui", {}).get("zoom", 1.0),
            viewport=snapshot.get("ui", {}).get("viewport", {"center_x": 0, "center_y": 0}),
            groups=snapshot.get("ui", {}).get("groups", []),
            collapsed_groups=snapshot.get("ui", {}).get("collapsed_groups", []),
            selected_nodes=snapshot.get("ui", {}).get("selected_nodes", []),
        )
        self.emit_audit_event(
            event_type="import.completed",
            resource_type="abhi_import",
            resource_id=str(source),
            action="import",
            metadata={
                "format": "abhi",
                "nodes_created": result.nodes_created,
                "nodes_updated": result.nodes_updated,
                "edges_created": result.edges_created,
                "edges_updated": result.edges_updated,
                "encrypted": result.encrypted,
            },
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

        atomic_items = split_atomic_items(trimmed_content)
        item_nodes: list[Node] = []
        for item in atomic_items:
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
            inferred = infer_relationship(
                previous,
                node,
                shared_tokens=shared_tokens,
                cosine_similarity=self._node_cosine_similarity(previous, node),
            )
            if inferred is not None:
                rel_type, confidence = inferred
                self.add_edge(
                    source_id=previous.id,
                    target_id=node.id,
                    relationship=rel_type,
                    metadata={"origin": "decomposition", "edge_confidence": confidence},
                )

        node_ids = [node.id for node in created_nodes]
        with self._lock, self._pool.checkout() as connection:
            edges = self._fetch_edges_for_nodes(connection, node_ids)
        return SubgraphResult(
            nodes=created_nodes,
            edges=edges,
            query=f"decomposition:{context.strip() or infer_label(trimmed_content)}",
            total_nodes_in_graph=self.get_stats().total_nodes,
        )

    # ---------------------------------------------------------------------------
    # Batch transcript ingestion (ingest-transcript-handoff)
    # ---------------------------------------------------------------------------

    def graph_diff(self, *, since: str = "24h") -> GraphDiffResult:
        cutoff = parse_since_value(since)
        with self._lock, self._pool.checkout() as connection:
            added_nodes = [
                self._row_to_node(row)
                for row in connection.execute(
                    """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                       created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    """,
                    (self.tenant_id, cutoff.isoformat()),
                ).fetchall()
            ]
            updated_nodes = [
                self._row_to_node(row)
                for row in connection.execute(
                    """
                    SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                           created_at, updated_at, access_count, tenant_id
                    FROM nodes
                    WHERE tenant_id = ?
                      AND updated_at >= ?
                      AND created_at < ?
                    ORDER BY updated_at DESC
                    """,
                    (self.tenant_id, cutoff.isoformat(), cutoff.isoformat()),
                ).fetchall()
            ]
            created_edges = [
                self._row_to_edge(row)
                for row in connection.execute(
                    """
                    SELECT id, source_id, target_id, relationship, weight, metadata, created_at
                    FROM edges
                    WHERE tenant_id = ? AND created_at >= ?
                    ORDER BY created_at DESC
                    """,
                    (self.tenant_id, cutoff.isoformat()),
                ).fetchall()
            ]
            contradiction_edges = [
                edge for edge in created_edges if edge.relationship == RelationType.CONTRADICTS.value
            ]
        return GraphDiffResult(
            since=since,
            added_nodes=added_nodes,
            updated_nodes=updated_nodes,
            created_edges=created_edges,
            contradiction_edges=contradiction_edges,
        )

    def prime_context(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 25,
    ) -> PrimeContextResult:
        with self._lock, self._pool.checkout() as connection:
            total_nodes = int(
                connection.execute("SELECT COUNT(*) FROM nodes WHERE tenant_id = ?", (self.tenant_id,)).fetchone()[0]
            )
            if total_nodes == 0:
                return PrimeContextResult(project=project, summary="No stored memory is available yet.")

            active_session_id = _retrieval_session_scope(
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            # Collect seed anchors from multiple sources
            seed_ids: list[str] = []
            seed_ids.extend(
                self._most_connected_node_ids(
                    connection,
                    limit=5,
                    agent_id=agent_id,
                    project=project,
                    session_id=active_session_id,
                )
            )
            seed_ids.extend(
                node.id
                for node in self.list_recent_nodes(
                    limit=5,
                    agent_id=agent_id,
                    project=project,
                    session_id=active_session_id,
                )
            )
            if project.strip():
                seed_ids.extend(
                    self._find_project_node_ids(
                        connection,
                        project=project,
                        agent_id=agent_id,
                        session_id=active_session_id,
                        limit=8,
                    )
                )
            seed_ids = list(dict.fromkeys(seed_ids))  # Deduplicate

            if not seed_ids:
                return PrimeContextResult(project=project, summary="No seed nodes found for priming.")

            # Load all embeddable nodes and build graph
            node_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                       created_at, updated_at, access_count, embedding, tenant_id
                FROM nodes
                WHERE tenant_id = ? AND embedding IS NOT NULL
                """,
                (self.tenant_id,),
            ).fetchall()

            if not node_rows:
                return PrimeContextResult(project=project, summary="No embeddable nodes available for expansion.")

            nodes_by_id: dict[str, Node] = {}
            for row in node_rows:
                node = self._row_to_node(row)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=active_session_id):
                    continue
                nodes_by_id[node.id] = node

            graph = self._load_graph(connection, node_ids=nodes_by_id.keys())

            if not nodes_by_id:
                return PrimeContextResult(project=project, summary="No scoped nodes found for priming.")

            # Seeds can include non-embeddable nodes (e.g., recently touched items). Filter to embeddable
            # nodes to avoid KeyError when scoring/expanding.
            scoped_seed_ids = [seed_id for seed_id in seed_ids if seed_id in nodes_by_id]
            if not scoped_seed_ids:
                # Fall back to a small set of recent embeddable nodes when none of the seeds are usable.
                scoped_seed_ids = list(nodes_by_id.keys())[:5]

            # Expand from seeds using relation-aware traversal
            max_depth = 2
            expanded_depths, expansion_metadata = self._expand_node_depths_with_context(
                graph, scoped_seed_ids, max_depth
            )

            # Build candidate nodes from expansion
            candidate_nodes = [nodes_by_id[nid] for nid in expanded_depths if nid in nodes_by_id]
            if not candidate_nodes:
                return PrimeContextResult(project=project, summary="Expansion produced no candidate nodes.")

            # Score with relation-aware ranking (no natural language query)
            expanded_ids_in_scope = [nid for nid in expanded_depths if nid in nodes_by_id]
            similarity_by_id = dict.fromkeys(expanded_ids_in_scope, 0.0)
            lexical_by_id = dict.fromkeys(expanded_ids_in_scope, 0.0)
            negation_boost_by_id = dict.fromkeys(expanded_ids_in_scope, 0.0)
            transcript_session_scores = self._recent_transcript_session_scores(
                agent_id=agent_id,
                project=project,
                session_id=active_session_id,
            )
            # Boost seed IDs synthetically
            for seed_id in scoped_seed_ids:
                if seed_id in similarity_by_id:
                    similarity_by_id[seed_id] = 0.5
            for node_id in list(similarity_by_id.keys()):
                node = nodes_by_id.get(node_id)
                if node is None:
                    continue
                similarity_by_id[node_id] = self._blend_session_signal(
                    base_similarity=similarity_by_id[node_id],
                    session_signal=transcript_session_scores.get(node.session_id, 0.0),
                    session_weight=0.35,
                )

            degree_by_id = dict(graph.degree(expanded_depths.keys()))
            max_access = max((node.access_count for node in candidate_nodes), default=0)
            max_degree = max(degree_by_id.values(), default=0)
            candidate_edges = self._fetch_edges_for_nodes(connection, [node.id for node in candidate_nodes])

            temporal_hints = _NeutralTemporalHints()
            scored_nodes = self._sort_scored_nodes(
                candidate_nodes,
                max_nodes=max_nodes,
                temporal_hints=temporal_hints,
                similarity_by_id=similarity_by_id,
                lexical_by_id=lexical_by_id,
                negation_boost_by_id=negation_boost_by_id,
                degree_by_id=degree_by_id,
                max_access=max_access,
                max_degree=max_degree,
                max_depth=max_depth,
                expanded_depths=expanded_depths,
                edges=candidate_edges,
                expansion_metadata=expansion_metadata,
            )

            # Apply support coverage
            selected_nodes = scored_nodes[:max_nodes]
            candidate_pool = {node.id: node for node in candidate_nodes}
            selected_nodes = self._ensure_support_coverage(selected_nodes, candidate_pool, graph, max_nodes)

            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(connection, selected_ids)

        # Build structured summary
        summary = self._build_prime_summary(
            selected_nodes=selected_nodes,
            edges=edges,
            total_nodes_in_graph=total_nodes,
            project=project,
        )

        return PrimeContextResult(
            project=project,
            summary=summary,
            nodes=selected_nodes,
            edges=edges,
            total_nodes_in_graph=total_nodes,
        )

    def _fetch_node_row(self, connection: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, aliases, source_prompt, embedding_model_id, embedding_dim, source_turn_pair_id, metadata, evidence_records, valid_from, valid_to,
                   created_at, updated_at, access_count, embedding, tenant_id
            FROM nodes
            WHERE id = ? AND tenant_id = ?
            """,
            (node_id, self.tenant_id),
        ).fetchone()

    def _fetch_nodes_by_ids(
        self,
        connection: sqlite3.Connection,
        node_ids: list[str],
    ) -> list[Node]:
        if not node_ids:
            return []
        placeholders = ", ".join("?" for _ in node_ids)
        rows = connection.execute(
            f"""
            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, aliases, source_prompt, embedding_model_id, embedding_dim, source_turn_pair_id, metadata, evidence_records, valid_from, valid_to,
                   created_at, updated_at, access_count, tenant_id
            FROM nodes
            WHERE tenant_id = ? AND id IN ({placeholders})
            """,
            (self.tenant_id, *node_ids),
        ).fetchall()
        rows_by_id = {row["id"]: row for row in rows}
        return [self._row_to_node(rows_by_id[node_id]) for node_id in node_ids if node_id in rows_by_id]

    def _row_to_node(self, row: sqlite3.Row) -> Node:
        row_keys = set(row.keys())
        return Node(
            id=row["id"],
            tenant_id=row["tenant_id"] if "tenant_id" in row_keys else self.tenant_id,
            agent_id=row["agent_id"] if "agent_id" in row_keys else "",
            project=row["project"] if "project" in row_keys else "",
            session_id=row["session_id"] if "session_id" in row_keys else "",
            context_window_id=row["context_window_id"] if "context_window_id" in row_keys else None,
            label=row["label"],
            content=row["content"],
            node_type=NodeType(row["node_type"]),
            tags=json.loads(row["tags"] or "[]"),
            aliases=json.loads(row["aliases"] or "[]") if "aliases" in row_keys else [],
            source_prompt=row["source_prompt"] or "",
            embedding_model_id=row["embedding_model_id"] if "embedding_model_id" in row_keys else "",
            embedding_dim=int(row["embedding_dim"] or 0) if "embedding_dim" in row_keys else 0,
            source_turn_pair_id=row["source_turn_pair_id"] if "source_turn_pair_id" in row_keys else "",
            metadata=_decode_metadata(row["metadata"]) if "metadata" in row_keys else {},
            evidence_records=_decode_evidence_records(row["evidence_records"])
            if "evidence_records" in row_keys
            else [],
            valid_from=_parse_datetime(row["valid_from"]) if "valid_from" in row_keys and row["valid_from"] else None,
            valid_to=_parse_datetime(row["valid_to"]) if "valid_to" in row_keys and row["valid_to"] else None,
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            access_count=int(row["access_count"] or 0),
        )

    def _row_to_context_window(self, row: sqlite3.Row) -> ContextWindow:
        row_keys = set(row.keys())
        return ContextWindow(
            id=row["id"],
            tenant_id=row["tenant_id"] if "tenant_id" in row_keys else self.tenant_id,
            repo_id=row["repo_id"],
            session_id=row["session_id"],
            title=row["title"] or "",
            status=row["status"] or "active",
            node_count=int(row["node_count"] or 0),
            embedding_stale=bool(row["embedding_stale"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            closed_at=_parse_datetime(row["closed_at"]) if row["closed_at"] else None,
        )

    def _row_to_context_window_edge(self, row: sqlite3.Row) -> ContextWindowEdge:
        row_keys = set(row.keys())
        return ContextWindowEdge(
            id=row["id"],
            tenant_id=row["tenant_id"] if "tenant_id" in row_keys else self.tenant_id,
            source_window_id=row["source_window_id"],
            target_window_id=row["target_window_id"],
            edge_type=row["edge_type"],
            shared_entities=json.loads(row["shared_entities"] or "[]"),
            weight=float(row["weight"] if row["weight"] is not None else 1.0),
            metadata=_decode_metadata(row["metadata"]),
            created_at=_parse_datetime(row["created_at"]),
        )

    def _row_to_edge(self, row: sqlite3.Row) -> Edge:
        row_keys = set(row.keys())
        return Edge(
            id=row["id"],
            tenant_id=row["tenant_id"] if "tenant_id" in row_keys else self.tenant_id,
            source_id=row["source_id"],
            target_id=row["target_id"],
            relationship=row["relationship"],
            weight=float(row["weight"]),
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=_parse_datetime(row["created_at"]),
        )

    def _upsert_vault_document(
        self,
        connection: sqlite3.Connection,
        document: Any,
    ) -> tuple[Node, bool]:
        node_id = str(document.frontmatter.get("node_id", "")).strip()
        row = self._fetch_node_row(connection, node_id)
        raw_type = str(document.frontmatter.get("node_type", "note") or "note")
        try:
            node_type = NodeType(raw_type)
        except ValueError:
            node_type = NodeType.NOTE
        tags = [str(tag) for tag in document.frontmatter.get("tags", []) or []]
        agent_id = str(document.frontmatter.get("agent_id", "") or "")
        project = str(document.frontmatter.get("project", "") or "")
        session_id = str(document.frontmatter.get("session_id", "") or "")
        valid_from = self._parse_optional_datetime(document.frontmatter.get("valid_from"))
        valid_to = self._parse_optional_datetime(document.frontmatter.get("valid_to"))
        evidence_records = evidence_from_lines(document.evidence_lines)
        content = document.content.strip() or document.label
        embedding_vector, embedding_model_id, embedding_dim = self._embed_with_metadata(content)
        embedding_bytes = self._encode_embedding(embedding_vector)
        if row is None:
            created_at = self._parse_optional_datetime(document.frontmatter.get("created_at")) or utc_now()
            updated_at = utc_now()
            node = Node(
                id=node_id,
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                label=document.label,
                content=content,
                node_type=node_type,
                tags=tags,
                embedding_model_id=embedding_model_id,
                embedding_dim=embedding_dim,
                evidence_records=evidence_records,
                valid_from=valid_from,
                valid_to=valid_to,
                created_at=created_at,
                updated_at=updated_at,
            )
            connection.execute(
                """
                INSERT INTO nodes (
                    id, tenant_id, agent_id, project, session_id, label, content, node_type, tags, metadata, embedding,
                    embedding_model_id, embedding_dim, source_prompt, source_turn_pair_id, evidence_records, valid_from, valid_to, created_at, updated_at, access_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.id,
                    node.tenant_id,
                    node.agent_id,
                    node.project,
                    node.session_id,
                    node.label,
                    node.content,
                    node.node_type.value,
                    json.dumps(node.tags),
                    _encode_metadata(node.metadata),
                    embedding_bytes,
                    node.embedding_model_id,
                    node.embedding_dim,
                    "",
                    "",
                    _encode_evidence_records(node.evidence_records),
                    node.valid_from.isoformat() if node.valid_from is not None else None,
                    node.valid_to.isoformat() if node.valid_to is not None else None,
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                    node.access_count,
                ),
            )
            return node, True

        existing = self._row_to_node(row)
        updated_at = utc_now()
        node = Node(
            id=existing.id,
            tenant_id=existing.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            label=document.label,
            content=content,
            node_type=node_type,
            tags=tags,
            source_prompt=existing.source_prompt,
            embedding_model_id=embedding_model_id,
            embedding_dim=embedding_dim,
            source_turn_pair_id=existing.source_turn_pair_id,
            metadata=existing.metadata,
            evidence_records=evidence_records or existing.evidence_records,
            valid_from=valid_from,
            valid_to=valid_to,
            created_at=existing.created_at,
            updated_at=updated_at,
            access_count=existing.access_count,
        )
        connection.execute(
            """
            UPDATE nodes
            SET agent_id = ?, project = ?, session_id = ?, label = ?, content = ?, node_type = ?, tags = ?,
                metadata = ?, embedding = ?, embedding_model_id = ?, embedding_dim = ?, evidence_records = ?, valid_from = ?, valid_to = ?, updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                node.agent_id,
                node.project,
                node.session_id,
                node.label,
                node.content,
                node.node_type.value,
                json.dumps(node.tags),
                _encode_metadata(node.metadata),
                embedding_bytes,
                node.embedding_model_id,
                node.embedding_dim,
                _encode_evidence_records(node.evidence_records),
                node.valid_from.isoformat() if node.valid_from is not None else None,
                node.valid_to.isoformat() if node.valid_to is not None else None,
                node.updated_at.isoformat(),
                node.id,
                self.tenant_id,
            ),
        )
        return node, False

    def _insert_vault_stub_node(
        self,
        connection: sqlite3.Connection,
        *,
        label: str,
        project: str,
        agent_id: str,
        session_id: str,
    ) -> Node:
        embedding_vector, embedding_model_id, embedding_dim = self._embed_with_metadata(
            f"Stub node imported from vault for {label}."
        )
        node = Node(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            label=label,
            content=f"Stub node imported from vault for {label}.",
            node_type=NodeType.NOTE,
            tags=["stub", "vault-import"],
            embedding_model_id=embedding_model_id,
            embedding_dim=embedding_dim,
        )
        connection.execute(
            """
            INSERT INTO nodes (
                id, tenant_id, agent_id, project, session_id, label, content, node_type, tags, metadata, embedding,
                embedding_model_id, embedding_dim, source_prompt, source_turn_pair_id, evidence_records, valid_from, valid_to, created_at, updated_at, access_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.id,
                node.tenant_id,
                node.agent_id,
                node.project,
                node.session_id,
                node.label,
                node.content,
                node.node_type.value,
                json.dumps(node.tags),
                _encode_metadata(node.metadata),
                self._encode_embedding(embedding_vector),
                node.embedding_model_id,
                node.embedding_dim,
                "",
                "",
                _encode_evidence_records([]),
                None,
                None,
                node.created_at.isoformat(),
                node.updated_at.isoformat(),
                node.access_count,
            ),
        )
        return node

    def _parse_optional_datetime(self, raw: Any) -> datetime | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        try:
            return _parse_datetime(str(raw))
        except ValueError:
            return None

    def _load_graph(
        self,
        connection: sqlite3.Connection,
        *,
        node_ids: Iterable[str],
    ) -> nx.DiGraph:
        graph = nx.DiGraph()
        graph.add_nodes_from(node_ids)
        rows = connection.execute(
            """
            SELECT source_id, target_id, relationship, weight, metadata, created_at
            FROM edges
            WHERE tenant_id = ?
            """,
            (self.tenant_id,),
        ).fetchall()

        for row in rows:
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}

            graph.add_edge(
                row["source_id"],
                row["target_id"],
                relationship=row["relationship"] or "relates_to",
                weight=float(row["weight"]) if row["weight"] is not None else 1.0,
                metadata=metadata,
                created_at=row["created_at"],
            )
        return graph

    def _node_is_superseded(self, node: Node) -> bool:
        metadata = node.metadata or {}
        superseded_by = str(metadata.get("superseded_by", "") or "").strip()
        return bool(superseded_by)

    def _increment_access_counts(self, connection: sqlite3.Connection, node_ids: list[str]) -> None:
        if not node_ids:
            return
        placeholders = ", ".join("?" for _ in node_ids)
        connection.execute(
            f"""
            UPDATE nodes
            SET access_count = access_count + 1
            WHERE tenant_id = ? AND id IN ({placeholders})
            """,
            (self.tenant_id, *node_ids),
        )

    def _fetch_edge_row(self, connection: sqlite3.Connection, edge_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT id, source_id, target_id, relationship, weight, metadata, created_at, tenant_id
            FROM edges
            WHERE id = ? AND tenant_id = ?
            """,
            (edge_id, self.tenant_id),
        ).fetchone()

    def _build_backup_snapshot(
        self, connection: sqlite3.Connection, *, include_embeddings: bool = False
    ) -> dict[str, Any]:
        node_rows = connection.execute(
            """
            SELECT id, tenant_id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, source_prompt,
                   embedding_model_id, embedding_dim, source_turn_pair_id, metadata,
                   evidence_records, valid_from, valid_to, created_at, updated_at, access_count, embedding
            FROM nodes
            WHERE tenant_id = ?
            ORDER BY created_at ASC
            """,
            (self.tenant_id,),
        ).fetchall()
        edge_rows = connection.execute(
            """
            SELECT id, tenant_id, source_id, target_id, relationship, weight, metadata, created_at
            FROM edges
            WHERE tenant_id = ?
            ORDER BY created_at ASC
            """,
            (self.tenant_id,),
        ).fetchall()
        repo_rows = connection.execute(
            """
            SELECT id, tenant_id, name, description, created_at, updated_at
            FROM repos
            WHERE tenant_id = ?
            ORDER BY created_at ASC
            """,
            (self.tenant_id,),
        ).fetchall()
        window_rows = connection.execute(
            """
            SELECT id, tenant_id, repo_id, session_id, title, status, node_count,
                   embedding_stale, created_at, updated_at, closed_at
            FROM context_windows
            WHERE tenant_id = ?
            ORDER BY created_at ASC
            """,
            (self.tenant_id,),
        ).fetchall()
        window_edge_rows = connection.execute(
            """
            SELECT id, tenant_id, source_window_id, target_window_id, edge_type,
                   shared_entities, weight, metadata, created_at
            FROM context_window_edges
            WHERE tenant_id = ?
            ORDER BY created_at ASC
            """,
            (self.tenant_id,),
        ).fetchall()
        transcript_rows = connection.execute(
            """
            SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role, transcript_text,
                   embedding, embedding_model_id, embedding_dim, content_hash, turn_pair_id, metadata
            FROM transcript_records
            WHERE tenant_id = ?
            ORDER BY observed_at ASC, turn_index ASC
            """,
            (self.tenant_id,),
        ).fetchall()
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "tenant_id": self.tenant_id,
            "embedding_model_id": self._current_embedding_model_id(),
            "repos": [
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "name": row["name"],
                    "description": row["description"] or "",
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in repo_rows
            ],
            "context_windows": [
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "repo_id": row["repo_id"],
                    "session_id": row["session_id"],
                    "title": row["title"] or "",
                    "status": row["status"] or "active",
                    "node_count": int(row["node_count"] or 0),
                    "embedding_stale": True,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "closed_at": row["closed_at"],
                }
                for row in window_rows
            ],
            "context_window_edges": [
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "source_window_id": row["source_window_id"],
                    "target_window_id": row["target_window_id"],
                    "edge_type": row["edge_type"],
                    "shared_entities": json.loads(row["shared_entities"] or "[]"),
                    "weight": float(row["weight"] if row["weight"] is not None else 1.0),
                    "metadata": _decode_metadata(row["metadata"]),
                    "created_at": row["created_at"],
                }
                for row in window_edge_rows
            ],
            "nodes": [
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "agent_id": row["agent_id"] or "",
                    "project": row["project"] or "",
                    "session_id": row["session_id"] or "",
                    "context_window_id": row["context_window_id"],
                    "label": row["label"],
                    "content": row["content"],
                    "node_type": row["node_type"],
                    "tags": json.loads(row["tags"] or "[]"),
                    "source_prompt": row["source_prompt"] or "",
                    "embedding_model_id": row["embedding_model_id"] or "",
                    "embedding_dim": int(row["embedding_dim"] or 0),
                    "source_turn_pair_id": row["source_turn_pair_id"] or "",
                    "metadata": _decode_metadata(row["metadata"]),
                    "evidence_records": [
                        record.model_dump(mode="json") for record in _decode_evidence_records(row["evidence_records"])
                    ],
                    "valid_from": row["valid_from"],
                    "valid_to": row["valid_to"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "access_count": int(row["access_count"] or 0),
                    "embedding": row["embedding"] if include_embeddings else None,
                }
                for row in node_rows
            ],
            "edges": [
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "relationship": row["relationship"],
                    "weight": float(row["weight"]),
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "created_at": row["created_at"],
                }
                for row in edge_rows
            ],
            "transcripts": [
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "agent_id": row["agent_id"] or "",
                    "project": row["project"] or "",
                    "session_id": row["session_id"] or "",
                    "observed_at": row["observed_at"],
                    "turn_index": int(row["turn_index"] or 0),
                    "role": row["role"] or "",
                    "transcript_text": row["transcript_text"],
                    "embedding_model_id": row["embedding_model_id"] or "",
                    "embedding_dim": int(row["embedding_dim"] or 0),
                    "content_hash": row["content_hash"] or "",
                    "turn_pair_id": row["turn_pair_id"] or "",
                    "metadata": _decode_metadata(row["metadata"]),
                    "embedding": row["embedding"] if include_embeddings else None,
                }
                for row in transcript_rows
            ],
        }
        if include_embeddings:
            snapshot["embedding_dim"] = next(
                (int(row["embedding_dim"] or 0) for row in node_rows if int(row["embedding_dim"] or 0)), 0
            )
        return snapshot

    def _coerce_imported_embedding(
        self, embedding: Any, *, text: str, model_id: str, dim: int
    ) -> tuple[bytes, str, int]:
        """Normalise an imported/snapshot embedding blob for persistence (issue #71).

        Imported blobs arrive from another store (or off disk) and were previously
        written verbatim, so a legacy blob stayed un-checksummed and a corrupt blob
        was persisted as-is and never re-embedded. This validates first:

        * a supplied blob the model can decode (legacy or checksummed) with valid
          metadata → canonical checksummed form, keeping the supplied
          ``model_id``/``dim``;
        * a missing, corrupt, or model-unreadable blob (or one with no usable
          metadata) → re-embed from ``text`` (a fresh checksummed vector with the
          current model id/dim).
        """
        if isinstance(embedding, (bytes, bytearray)):
            blob = bytes(embedding)
            # Validate with the model decoder, not just the wrapper: a CRC-valid
            # blob the model can't parse would otherwise be preserved here and then
            # read back as "missing" by every _decode_embedding while health stays
            # clean. Require real metadata too, so a kept blob isn't left stale.
            if self._decode_embedding(blob) is not None and model_id.strip() and dim > 0:
                return self._ensure_encoded_embedding(blob), model_id, dim
        vector, new_model_id, new_dim = self._embed_with_metadata(text)
        return self._encode_embedding(vector), new_model_id, new_dim

    def _insert_snapshot_node(self, connection: sqlite3.Connection, raw_node: dict[str, Any]) -> None:
        embedding, embedding_model_id, embedding_dim = self._coerce_imported_embedding(
            raw_node.get("embedding"),
            text=raw_node["content"],
            model_id=str(raw_node.get("embedding_model_id", "") or ""),
            dim=int(raw_node.get("embedding_dim", 0) or 0),
        )
        connection.execute(
            """
            INSERT INTO nodes (
                id, tenant_id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, metadata, embedding,
                embedding_model_id, embedding_dim, source_prompt, source_turn_pair_id, evidence_records, valid_from, valid_to, created_at, updated_at, access_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_node["id"],
                raw_node.get("tenant_id", self.tenant_id),
                raw_node.get("agent_id", ""),
                raw_node.get("project", ""),
                raw_node.get("session_id", ""),
                raw_node.get("context_window_id"),
                raw_node["label"],
                raw_node["content"],
                raw_node["node_type"],
                json.dumps(raw_node.get("tags", [])),
                _encode_metadata(raw_node.get("metadata", {})),
                embedding,
                embedding_model_id,
                embedding_dim,
                raw_node.get("source_prompt", ""),
                raw_node.get("source_turn_pair_id", ""),
                _encode_evidence_records(
                    [EvidenceRecord.model_validate(item) for item in raw_node.get("evidence_records", [])]
                ),
                raw_node.get("valid_from"),
                raw_node.get("valid_to"),
                raw_node["created_at"],
                raw_node["updated_at"],
                int(raw_node.get("access_count", 0)),
            ),
        )

    def _insert_snapshot_transcript(self, connection: sqlite3.Connection, raw_transcript: dict[str, Any]) -> None:
        embedding, embedding_model_id, embedding_dim = self._coerce_imported_embedding(
            raw_transcript.get("embedding"),
            text=raw_transcript["transcript_text"],
            model_id=str(raw_transcript.get("embedding_model_id", "") or ""),
            dim=int(raw_transcript.get("embedding_dim", 0) or 0),
        )
        connection.execute(
            """
            INSERT INTO transcript_records (
                id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role,
                transcript_text, embedding, embedding_model_id, embedding_dim, content_hash, turn_pair_id, metadata, message_identity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_transcript["id"],
                raw_transcript.get("tenant_id", self.tenant_id),
                raw_transcript.get("agent_id", ""),
                raw_transcript.get("project", ""),
                raw_transcript.get("session_id", ""),
                raw_transcript["observed_at"],
                int(raw_transcript.get("turn_index", 0)),
                raw_transcript.get("role", ""),
                raw_transcript["transcript_text"],
                embedding,
                embedding_model_id,
                embedding_dim,
                raw_transcript.get("content_hash", _normalized_content_hash(raw_transcript["transcript_text"])),
                raw_transcript.get("turn_pair_id", ""),
                _encode_metadata(raw_transcript.get("metadata", {})),
                None,
            ),
        )

    def _update_snapshot_transcript(self, connection: sqlite3.Connection, raw_transcript: dict[str, Any]) -> None:
        embedding, embedding_model_id, embedding_dim = self._coerce_imported_embedding(
            raw_transcript.get("embedding"),
            text=raw_transcript["transcript_text"],
            model_id=str(raw_transcript.get("embedding_model_id", "") or ""),
            dim=int(raw_transcript.get("embedding_dim", 0) or 0),
        )
        connection.execute(
            """
            UPDATE transcript_records
            SET tenant_id = ?, agent_id = ?, project = ?, session_id = ?, observed_at = ?, turn_index = ?, role = ?,
                transcript_text = ?, embedding = ?, embedding_model_id = ?, embedding_dim = ?, content_hash = ?, turn_pair_id = ?, metadata = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                raw_transcript.get("tenant_id", self.tenant_id),
                raw_transcript.get("agent_id", ""),
                raw_transcript.get("project", ""),
                raw_transcript.get("session_id", ""),
                raw_transcript["observed_at"],
                int(raw_transcript.get("turn_index", 0)),
                raw_transcript.get("role", ""),
                raw_transcript["transcript_text"],
                embedding,
                embedding_model_id,
                embedding_dim,
                raw_transcript.get("content_hash", _normalized_content_hash(raw_transcript["transcript_text"])),
                raw_transcript.get("turn_pair_id", ""),
                _encode_metadata(raw_transcript.get("metadata", {})),
                raw_transcript["id"],
                self.tenant_id,
            ),
        )

    def _upsert_snapshot_repo(self, connection: sqlite3.Connection, raw_repo: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        connection.execute(
            """
            INSERT INTO repos (id, tenant_id, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                name = excluded.name,
                description = excluded.description,
                updated_at = excluded.updated_at
            """,
            (
                raw_repo["id"],
                raw_repo.get("tenant_id", self.tenant_id),
                raw_repo.get("name", raw_repo["id"]),
                raw_repo.get("description", ""),
                raw_repo.get("created_at") or now,
                raw_repo.get("updated_at") or now,
            ),
        )

    def _upsert_snapshot_context_window(self, connection: sqlite3.Connection, raw_window: dict[str, Any]) -> None:
        now = utc_now().isoformat()
        connection.execute(
            """
            INSERT INTO context_windows (
                id, tenant_id, repo_id, session_id, title, status, node_count,
                embedding, embedding_stale, created_at, updated_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                repo_id = excluded.repo_id,
                session_id = excluded.session_id,
                title = excluded.title,
                status = excluded.status,
                node_count = excluded.node_count,
                embedding = NULL,
                embedding_stale = 1,
                updated_at = excluded.updated_at,
                closed_at = excluded.closed_at
            """,
            (
                raw_window["id"],
                raw_window.get("tenant_id", self.tenant_id),
                raw_window["repo_id"],
                raw_window.get("session_id", "default"),
                raw_window.get("title", ""),
                raw_window.get("status", "active"),
                int(raw_window.get("node_count", 0)),
                raw_window.get("created_at") or now,
                raw_window.get("updated_at") or now,
                raw_window.get("closed_at"),
            ),
        )

    def _upsert_snapshot_context_window_edge(self, connection: sqlite3.Connection, raw_edge: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO context_window_edges (
                id, tenant_id, source_window_id, target_window_id, edge_type,
                shared_entities, weight, metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                source_window_id = excluded.source_window_id,
                target_window_id = excluded.target_window_id,
                edge_type = excluded.edge_type,
                shared_entities = excluded.shared_entities,
                weight = excluded.weight,
                metadata = excluded.metadata,
                created_at = excluded.created_at
            """,
            (
                raw_edge["id"],
                raw_edge.get("tenant_id", self.tenant_id),
                raw_edge["source_window_id"],
                raw_edge["target_window_id"],
                raw_edge["edge_type"],
                json.dumps(raw_edge.get("shared_entities", []), sort_keys=True),
                float(raw_edge.get("weight", 1.0)),
                _encode_metadata(raw_edge.get("metadata", {})),
                raw_edge["created_at"],
            ),
        )

    def _update_snapshot_node(self, connection: sqlite3.Connection, raw_node: dict[str, Any]) -> None:
        embedding, embedding_model_id, embedding_dim = self._coerce_imported_embedding(
            raw_node.get("embedding"),
            text=raw_node["content"],
            model_id=str(raw_node.get("embedding_model_id", "") or ""),
            dim=int(raw_node.get("embedding_dim", 0) or 0),
        )
        connection.execute(
            """
            UPDATE nodes
            SET tenant_id = ?, agent_id = ?, project = ?, session_id = ?, context_window_id = ?, label = ?, content = ?, node_type = ?, tags = ?, metadata = ?, embedding = ?,
                embedding_model_id = ?, embedding_dim = ?, source_prompt = ?, source_turn_pair_id = ?, evidence_records = ?, valid_from = ?, valid_to = ?,
                created_at = ?, updated_at = ?, access_count = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                raw_node.get("tenant_id", self.tenant_id),
                raw_node.get("agent_id", ""),
                raw_node.get("project", ""),
                raw_node.get("session_id", ""),
                raw_node.get("context_window_id"),
                raw_node["label"],
                raw_node["content"],
                raw_node["node_type"],
                json.dumps(raw_node.get("tags", [])),
                _encode_metadata(raw_node.get("metadata", {})),
                embedding,
                embedding_model_id,
                embedding_dim,
                raw_node.get("source_prompt", ""),
                raw_node.get("source_turn_pair_id", ""),
                _encode_evidence_records(
                    [EvidenceRecord.model_validate(item) for item in raw_node.get("evidence_records", [])]
                ),
                raw_node.get("valid_from"),
                raw_node.get("valid_to"),
                raw_node["created_at"],
                raw_node["updated_at"],
                int(raw_node.get("access_count", 0)),
                raw_node["id"],
                self.tenant_id,
            ),
        )

    def _insert_snapshot_edge(self, connection: sqlite3.Connection, raw_edge: dict[str, Any]) -> None:
        self._require_node(connection, raw_edge["source_id"])
        self._require_node(connection, raw_edge["target_id"])
        connection.execute(
            """
            INSERT INTO edges (id, tenant_id, source_id, target_id, relationship, weight, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_edge["id"],
                raw_edge.get("tenant_id", self.tenant_id),
                raw_edge["source_id"],
                raw_edge["target_id"],
                raw_edge["relationship"],
                float(raw_edge.get("weight", 1.0)),
                json.dumps(raw_edge.get("metadata", {})),
                raw_edge["created_at"],
            ),
        )

    def _update_snapshot_edge(self, connection: sqlite3.Connection, raw_edge: dict[str, Any]) -> None:
        self._require_node(connection, raw_edge["source_id"])
        self._require_node(connection, raw_edge["target_id"])
        connection.execute(
            """
            UPDATE edges
            SET tenant_id = ?, source_id = ?, target_id = ?, relationship = ?, weight = ?, metadata = ?, created_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                raw_edge.get("tenant_id", self.tenant_id),
                raw_edge["source_id"],
                raw_edge["target_id"],
                raw_edge["relationship"],
                float(raw_edge.get("weight", 1.0)),
                json.dumps(raw_edge.get("metadata", {})),
                raw_edge["created_at"],
                raw_edge["id"],
                self.tenant_id,
            ),
        )
