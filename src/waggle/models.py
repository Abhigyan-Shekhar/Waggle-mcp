from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from waggle.errors import AuthorizationError


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def default_api_key_scopes() -> list[str]:
    return ["graph:read", "graph:write", "admin:read", "admin:write"]


class NodeType(StrEnum):
    FACT = "fact"
    ENTITY = "entity"
    CONCEPT = "concept"
    PREFERENCE = "preference"
    DECISION = "decision"
    QUESTION = "question"
    NOTE = "note"


class RelationType(StrEnum):
    RELATES_TO = "relates_to"
    CONTRADICTS = "contradicts"
    DEPENDS_ON = "depends_on"
    PART_OF = "part_of"
    UPDATES = "updates"
    DERIVED_FROM = "derived_from"
    SIMILAR_TO = "similar_to"


def normalize_relationship(value: Any) -> str:
    if isinstance(value, RelationType):
        return value.value
    text = str(value).strip().lower()
    if not text:
        raise ValueError("Relationship cannot be empty.")
    return text


class Node(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    agent_id: str = ""
    project: str = ""
    session_id: str = ""
    context_window_id: str | None = None
    label: str
    content: str
    node_type: NodeType
    tags: list[str] = Field(default_factory=list)
    # aliases: all phrasings that have been merged into this canonical node.
    # Populated automatically during dedup; also updated by canonicalize_node.
    aliases: list[str] = Field(default_factory=list)
    source_prompt: str = ""
    embedding_model_id: str = ""
    embedding_dim: int = 0
    source_turn_pair_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_records: list[EvidenceRecord] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    access_count: int = 0
    similarity_score: float | None = None
    recency_score: float | None = None
    edge_score: float | None = None
    final_score: float | None = None

    @field_validator("label", "content", mode="before")
    @classmethod
    def _validate_required_text(cls, value: Any) -> str:
        if value is None:
            raise ValueError("Value is required.")
        text = str(value).strip()
        if not text:
            raise ValueError("Value cannot be empty.")
        return text

    @field_validator("source_prompt", mode="before")
    @classmethod
    def _normalize_source_prompt(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("agent_id", "project", "session_id", "embedding_model_id", "source_turn_pair_id", mode="before")
    @classmethod
    def _normalize_scope_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("context_window_id", mode="before")
    @classmethod
    def _normalize_optional_scope_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized


class EvidenceRecord(BaseModel):
    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str = ""
    turn_index: int = 0
    source_role: str = ""
    source_text: str = ""
    source_span_start: int | None = None
    source_span_end: int | None = None
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("session_id", "source_role", "source_text", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class Edge(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    source_id: str
    target_id: str
    relationship: str
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("source_id", "target_id", mode="before")
    @classmethod
    def _validate_node_ids(cls, value: Any) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("Node IDs cannot be empty.")
        return text

    @field_validator("relationship", mode="before")
    @classmethod
    def _validate_relationship(cls, value: Any) -> str:
        return normalize_relationship(value)


class ConnectedNodeStat(BaseModel):
    id: str
    label: str
    node_type: NodeType
    connection_count: int = 0


class RecentNodeStat(BaseModel):
    id: str
    label: str
    node_type: NodeType
    updated_at: datetime


class GraphStats(BaseModel):
    total_nodes: int = 0
    total_edges: int = 0
    total_repos: int = 0
    total_context_windows: int = 0
    context_window_status_breakdown: dict[str, int] = Field(default_factory=dict)
    total_context_window_edges: int = 0
    context_window_edge_type_breakdown: dict[str, int] = Field(default_factory=dict)
    windows_with_embeddings: int = 0
    windows_with_stale_embeddings: int = 0
    node_type_breakdown: dict[str, int] = Field(default_factory=dict)
    most_connected_nodes: list[ConnectedNodeStat] = Field(default_factory=list)
    most_recent_nodes: list[RecentNodeStat] = Field(default_factory=list)


class Repo(BaseModel):
    id: str
    tenant_id: str = ""
    name: str
    description: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ContextWindow(BaseModel):
    id: str
    tenant_id: str = ""
    repo_id: str
    session_id: str
    title: str = ""
    status: str = "active"
    node_count: int = 0
    embedding_stale: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None


class ContextWindowEdge(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    source_window_id: str
    target_window_id: str
    edge_type: str
    shared_entities: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class BackfillStats(BaseModel):
    nodes_scanned: int = 0
    nodes_assigned: int = 0
    nodes_skipped_already_assigned: int = 0
    repos_created: int = 0
    windows_created: int = 0
    window_edges_created: int = 0
    embeddings_computed: int = 0
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False


class SubgraphResult(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    replay_hits: list[ReplayHit] = Field(default_factory=list)
    fusion_hits: list[FusionHit] = Field(default_factory=list)
    hybrid_hits: list[HybridHit] = Field(default_factory=list)
    retrieval_mode: str = "graph"
    query: str = ""
    total_nodes_in_graph: int = 0


class ConflictRecord(BaseModel):
    other_node_id: str
    other_node_label: str
    relationship: str = RelationType.CONTRADICTS.value
    reason: str


class NodeStoreResult(BaseModel):
    node: Node
    created: bool = True
    dedup_reason: str | None = None
    similarity: float | None = None
    conflicts: list[ConflictRecord] = Field(default_factory=list)


class CanonicalizeResult(BaseModel):
    canonical_node: Node
    merged_node_ids: list[str] = Field(default_factory=list)
    edges_repointed: int = 0
    aliases_added: list[str] = Field(default_factory=list)


class DedupCandidatePair(BaseModel):
    node_id_a: str
    node_id_b: str
    label_a: str
    label_b: str
    similarity: float


class DedupCandidatesResult(BaseModel):
    pairs: list[DedupCandidatePair] = Field(default_factory=list)
    threshold: float = 0.85
    total_nodes_scanned: int = 0


class BackupResult(BaseModel):
    output_path: str
    tenant_id: str = ""
    schema_version: int = 1
    node_count: int = 0
    edge_count: int = 0


class AbhiExportResult(BaseModel):
    output_path: str
    tenant_id: str = ""
    schema_version: int = 1
    abhi_spec_version: str = "1.1"
    node_count: int = 0
    edge_count: int = 0
    content_hash: str = ""
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""
    executed_actions: list[str] = Field(default_factory=list)
    export_context: dict[str, Any] = Field(default_factory=dict)


class ImportResult(BaseModel):
    input_path: str
    tenant_id: str = ""
    schema_version: int = 1
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_updated: int = 0


class AbhiImportResult(BaseModel):
    input_path: str
    tenant_id: str = ""
    schema_version: int = 1
    abhi_spec_version: str = "1.1"
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    hash_verified: bool = False
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""
    executed_actions: list[str] = Field(default_factory=list)


class AbhiValidationResult(BaseModel):
    input_path: str
    valid: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    content_hash: str = ""
    abhi_spec_version: str = "1.1"
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""
    # new fields
    dangling_edges: list[str] = Field(default_factory=list)
    dangling_edge_count: int = 0
    boundary_warning: str = ""


class AbhiInspectResult(BaseModel):
    input_path: str
    tenant_id: str = ""
    schema_version: int = 1
    abhi_spec_version: str = "1.1"
    node_count: int = 0
    edge_count: int = 0
    node_types: list[str] = Field(default_factory=list)
    edge_types: list[str] = Field(default_factory=list)
    constraint_count: int = 0
    version_count: int = 0
    query_count: int = 0
    event_count: int = 0
    chunk_count: int = 0
    load_strategy: str = "full"
    preload_chunks: list[str] = Field(default_factory=list)
    content_hash: str = ""
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""


class AbhiDiffResult(BaseModel):
    input_path_a: str
    input_path_b: str
    abhi_spec_version_a: str = "1.1"
    abhi_spec_version_b: str = "1.1"
    nodes_added: list[str] = Field(default_factory=list)
    nodes_removed: list[str] = Field(default_factory=list)
    nodes_updated: list[str] = Field(default_factory=list)
    edges_added: list[str] = Field(default_factory=list)
    edges_removed: list[str] = Field(default_factory=list)
    edges_updated: list[str] = Field(default_factory=list)
    semantic_changes: list[str] = Field(default_factory=list)


class AbhiMergeResult(BaseModel):
    base_input_path: str
    left_input_path: str
    right_input_path: str
    output_path: str
    merge_strategy: str = "prefer_right"
    abhi_spec_version: str = "1.1"
    nodes_merged: int = 0
    edges_merged: int = 0
    conflicts: list[str] = Field(default_factory=list)
    content_hash: str = ""
    embedding_count: int = 0
    encrypted: bool = False
    encryption_algorithm: str = ""
    executed_actions: list[str] = Field(default_factory=list)
    # new fields
    conflict_records: list[MergeConflictRecord] = Field(default_factory=list)
    dry_run: bool = False
    hash_verified: bool = False
    dangling_edges_dropped: list[str] = Field(default_factory=list)
    contradict_edges_added: int = 0


class AbhiQueryResult(BaseModel):
    input_path: str
    query_id: str = ""
    name: str = ""
    query: str = ""
    summary: str = ""
    node_count: int = 0
    edge_count: int = 0
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    scanned_chunk_count: int = 0
    executed_actions: list[str] = Field(default_factory=list)


class AbhiChunkLoadResult(BaseModel):
    input_path: str
    chunk_ids: list[str] = Field(default_factory=list)
    load_strategy: str = "full"
    node_count: int = 0
    edge_count: int = 0
    available_chunk_count: int = 0
    query: str = ""
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)


class DrivePushResult(BaseModel):
    local_path: str
    remote_file_id: str = ""
    remote_name: str = ""
    folder_id: str = ""
    web_view_link: str = ""
    encrypted: bool = False


class DrivePullResult(BaseModel):
    remote_file_id: str = ""
    remote_name: str = ""
    downloaded_path: str = ""
    merged_output_path: str = ""
    merge_strategy: str = "last_write_wins"
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_updated: int = 0


class DriveShareResult(BaseModel):
    remote_file_id: str = ""
    permission_id: str = ""
    web_view_link: str = ""


class ObservationResult(BaseModel):
    stored_nodes: list[Node] = Field(default_factory=list)
    created_count: int = 0
    reused_count: int = 0
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    # New fields for verbatim-first architecture with async extraction enrichment
    turn_id: str = ""  # UUID for the turn, persisted before extraction
    verbatim_stored: bool = True  # Whether the raw turn was successfully persisted
    nodes_extracted: int = 0  # Number of nodes successfully extracted and stored
    edges_inferred: int = 0  # Number of edges inferred between extracted nodes
    extraction_errors: list[str] = Field(default_factory=list)  # Non-fatal extraction errors logged for diagnostics


class GraphDiffResult(BaseModel):
    since: str
    generated_at: datetime = Field(default_factory=utc_now)
    added_nodes: list[Node] = Field(default_factory=list)
    updated_nodes: list[Node] = Field(default_factory=list)
    created_edges: list[Edge] = Field(default_factory=list)
    contradiction_edges: list[Edge] = Field(default_factory=list)


class PrimeContextResult(BaseModel):
    project: str = ""
    summary: str = ""
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    total_nodes_in_graph: int = 0


class NodeHistoryResult(BaseModel):
    node: Node
    related_nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


class ContextTimelineItem(BaseModel):
    kind: str
    timestamp: datetime
    label: str
    summary: str = ""
    node_id: str | None = None
    edge_id: str | None = None
    recency_score: float | None = None


class TimelineResult(BaseModel):
    scope: str = ""
    items: list[ContextTimelineItem] = Field(default_factory=list)


class ConflictEntry(BaseModel):
    edge: Edge
    source_node: Node
    target_node: Node
    resolved: bool = False
    resolution_note: str = ""
    resolved_at: datetime | None = None


class ConflictListResult(BaseModel):
    conflicts: list[ConflictEntry] = Field(default_factory=list)
    include_resolved: bool = False


class ContextScopeResult(BaseModel):
    agent_ids: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)


class ContextRenderHints(BaseModel):
    token_estimate: int = 0
    recommended_paste_order: list[str] = Field(default_factory=list)
    truncation_flags: list[str] = Field(default_factory=list)
    chunk_count: int = 1


class ContextBundle(BaseModel):
    schema_version: int = 1
    export_type: str = "context_bundle"
    generated_at: datetime = Field(default_factory=utc_now)
    tenant_id: str = ""
    project: str = ""
    mode: str = "prime"
    retrieval_mode: str = "graph"
    audience: str = "llm"
    query: str = ""
    summary: str = ""
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    replay_hits: list[ReplayHit] = Field(default_factory=list)
    timeline: list[ContextTimelineItem] = Field(default_factory=list)
    stats: GraphStats = Field(default_factory=GraphStats)
    render_hints: ContextRenderHints = Field(default_factory=ContextRenderHints)


class ContextBundleExportResult(BaseModel):
    tenant_id: str = ""
    project: str = ""
    mode: str = "prime"
    retrieval_mode: str = "graph"
    query: str = ""
    summary: str = ""
    markdown_path: str | None = None
    json_path: str | None = None
    node_count: int = 0
    edge_count: int = 0
    bundle: ContextBundle


class TopicCluster(BaseModel):
    cluster_id: int
    label: str
    node_count: int
    top_tags: list[str] = Field(default_factory=list)
    nodes: list[Node] = Field(default_factory=list)


class TopicResult(BaseModel):
    clusters: list[TopicCluster] = Field(default_factory=list)
    total_clusters: int = 0


class MarkdownVaultExportResult(BaseModel):
    root_path: str
    tenant_id: str = ""
    project: str = ""
    node_count: int = 0
    edge_count: int = 0
    files_written: list[str] = Field(default_factory=list)


class MarkdownVaultImportResult(BaseModel):
    root_path: str
    tenant_id: str = ""
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_deleted: int = 0
    stub_nodes_created: int = 0
    conflicts: list[str] = Field(default_factory=list)


class TenantRecord(BaseModel):
    tenant_id: str
    name: str = ""
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)


class ApiKeyRecord(BaseModel):
    api_key_id: str
    tenant_id: str
    key_hash: str
    prefix: str = ""
    name: str = ""
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_by: str = ""
    scopes: list[str] = Field(default_factory=default_api_key_scopes)

    @field_validator("scopes", mode="before")
    @classmethod
    def _normalize_scopes(cls, value: Any) -> list[str]:
        if value is None:
            return default_api_key_scopes()
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
        else:
            items = [str(item).strip() for item in value]
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not item or item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        return normalized or default_api_key_scopes()

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthorizationError(f"API key is missing required scope: {scope}")


class ApiKeyCreateResult(BaseModel):
    record: ApiKeyRecord
    raw_api_key: str


class RetentionPolicyRecord(BaseModel):
    tenant_id: str
    enabled: bool = False
    retention_days: int = 90
    prune_interval_hours: int = 24
    last_pruned_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RetentionPruneRunRecord(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    status: str = "completed"
    cutoff: datetime
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    deleted_nodes: int = 0
    deleted_edges: int = 0
    deleted_transcripts: int = 0
    deleted_context_windows: int = 0
    deleted_context_window_edges: int = 0
    deleted_exports: int = 0
    duration_ms: int = 0
    error_message: str = ""


class AuditEventRecord(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    event_type: str
    actor_type: str = "system"
    actor_id: str = ""
    api_key_id: str = ""
    resource_type: str = ""
    resource_id: str = ""
    action: str = ""
    status: str = "success"
    ip_address: str = ""
    user_agent: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TranscriptRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    agent_id: str = ""
    project: str = ""
    session_id: str = ""
    observed_at: datetime = Field(default_factory=utc_now)
    turn_index: int = 0
    role: str = ""
    transcript_text: str
    embedding_model_id: str = ""
    embedding_dim: int = 0
    content_hash: str = ""
    turn_pair_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "agent_id", "project", "session_id", "role", "embedding_model_id", "content_hash", "turn_pair_id", mode="before"
    )
    @classmethod
    def _normalize_scope_fields(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("transcript_text", mode="before")
    @classmethod
    def _validate_transcript_text(cls, value: Any) -> str:
        if value is None:
            raise ValueError("Transcript text is required.")
        text = str(value).strip()
        if not text:
            raise ValueError("Transcript text cannot be empty.")
        return text


class ReplayHit(BaseModel):
    score: float = 0.0
    session_id: str = ""
    turn_index: int = 0
    turn_pair_id: str = ""
    role: str = ""
    transcript_text: str = ""
    transcript_snippet: str = ""
    observed_at: datetime = Field(default_factory=utc_now)


class FusionHit(BaseModel):
    content: str
    score: float = 0.0
    source_lane: str = "graph"
    graph_rank: int | None = None
    replay_rank: int | None = None
    fused_rank: int = 0
    node_id: str | None = None
    node_type: str | None = None
    edges: list[dict[str, Any]] | None = None
    session_id: str | None = None
    transcript_snippet: str | None = None
    turn_index: int | None = None


class HybridHit(BaseModel):
    content: str
    score: float = 0.0
    source: str = "transcript"
    turn_pair_id: str = ""
    node_ids: list[str] = Field(default_factory=list)
    reasoning_from_reranker: str = ""
    observed_at: datetime | None = None
    layer_scores: dict[str, float] = Field(default_factory=dict)
    score_explanation: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transcript handoff ingestion models
# ---------------------------------------------------------------------------


ALLOWED_TRANSCRIPT_ROLES = frozenset({"user", "assistant", "system", "tool"})
EXTRACTION_ROLES = frozenset({"user", "assistant"})


class TranscriptMessage(BaseModel):
    """One message in a transcript handoff payload."""

    role: str
    content: str
    timestamp: str | None = None
    message_id: str | None = None

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: Any) -> str:
        text = str(value).strip().lower()
        if text not in ALLOWED_TRANSCRIPT_ROLES:
            raise ValueError(f"Unsupported role '{value}'. Allowed roles: {sorted(ALLOWED_TRANSCRIPT_ROLES)}")
        return text

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, value: Any) -> str:
        if value is None:
            raise ValueError("Message content is required.")
        text = str(value).strip()
        if not text:
            raise ValueError("Message content cannot be empty.")
        return text

    @field_validator("message_id", "timestamp", mode="before")
    @classmethod
    def _normalize_optional_str(cls, value: Any) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped if stripped else None


class TranscriptIngestionInput(BaseModel):
    """Top-level JSON payload for ingest-transcript-handoff."""

    messages: list[TranscriptMessage] = Field(default_factory=list)
    project: str = ""
    agent_id: str = ""
    session_id: str = ""

    @field_validator("project", "agent_id", "session_id", mode="before")
    @classmethod
    def _normalize_scope(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class TranscriptIngestionResult(BaseModel):
    """Result returned by the batch transcript ingestion backend helper."""

    # Resolved scope
    project: str = ""
    agent_id: str = ""
    session_id: str = ""
    # Input counts
    input_message_count: int = 0
    transcript_records_written: int = 0
    transcript_records_skipped: int = 0
    logical_turns_processed: int = 0
    unpaired_trailing_blocks: int = 0
    # Node extraction counts
    nodes_created: int = 0
    nodes_reused: int = 0
    conflicts: int = 0
    # Export metadata
    export_skipped: bool = False
    export_skipped_reason: str = ""
    markdown_path: str | None = None
    json_path: str | None = None
    export_node_count: int = 0
    export_edge_count: int = 0
    checkpoint_path: str | None = None
    checkpoint_scope: str = ""


class ClearScopeResult(BaseModel):
    scope: str
    project: str = ""
    session_id: str = ""
    deleted_nodes: int = 0
    deleted_edges: int = 0
    deleted_transcripts: int = 0
    deleted_context_windows: int = 0
    deleted_context_window_edges: int = 0
    deleted_repos: int = 0
    deleted_graph_ui_rows: int = 0
    dry_run: bool = False
    counts_by_node_type: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Diff / Merge models (abhi-diff-merge-tool)
# ---------------------------------------------------------------------------


class FieldDelta(BaseModel):
    field: str  # e.g. "content", "tags", "metadata.source_app"
    old_value: Any  # None when the object was added
    new_value: Any  # None when the object was removed


class NodeDiffRecord(BaseModel):
    node_id: str
    classification: Literal["added", "removed", "modified", "identical"]
    label: str = ""  # label or content[:60] for display
    deltas: list[FieldDelta] = Field(default_factory=list)


class EdgeDiffRecord(BaseModel):
    edge_id: str
    classification: Literal["added", "removed", "modified", "identical"]
    deltas: list[FieldDelta] = Field(default_factory=list)


class MergeConflictRecord(BaseModel):
    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    object_id: str  # node or edge ID
    object_type: Literal["node", "edge"]
    field: str  # e.g. "content", "tags"
    base_value: Any  # value in the base document (None if absent)
    left_value: Any  # value in the left document
    right_value: Any  # value in the right document
    resolved_by: str = ""  # set after resolution
    resolved_value: Any = None  # the value selected after resolution


class FieldLevelDiffResult(BaseModel):
    input_path_a: str
    input_path_b: str
    input_path_base: str = ""  # empty string for two-way diff
    abhi_spec_version_a: str = ""
    abhi_spec_version_b: str = ""
    diff_mode: Literal["two_way", "three_way"] = "two_way"

    # Node-level summary (IDs only, for backward compat)
    nodes_added: list[str] = Field(default_factory=list)
    nodes_removed: list[str] = Field(default_factory=list)
    nodes_updated: list[str] = Field(default_factory=list)

    # Edge-level summary (IDs only)
    edges_added: list[str] = Field(default_factory=list)
    edges_removed: list[str] = Field(default_factory=list)
    edges_updated: list[str] = Field(default_factory=list)

    # Field-level detail
    node_records: list[NodeDiffRecord] = Field(default_factory=list)
    edge_records: list[EdgeDiffRecord] = Field(default_factory=list)

    # Three-way only
    conflict_records: list[MergeConflictRecord] = Field(default_factory=list)

    # Warnings
    warnings: list[str] = Field(default_factory=list)
    schema_version_mismatch: bool = False


class MergeStrategyFieldOverride(BaseModel):
    field: str  # e.g. "tags", "metadata.source_app"
    strategy: str  # "prefer_left" | "prefer_right" | "last_write_wins" | "contradict"


class MergeStrategyTypeOverride(BaseModel):
    node_type: str  # e.g. "decision", "fact"
    strategy: str


class MergeStrategyConfig(BaseModel):
    default_strategy: str = "contradict"
    field_overrides: list[MergeStrategyFieldOverride] = Field(default_factory=list)
    type_overrides: list[MergeStrategyTypeOverride] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None) -> MergeStrategyConfig:
        """
        Load from path, or from ~/.waggle/merge-strategies.yaml if path is None.
        Returns a default config if the file does not exist.
        """
        import os

        if path is None:
            config_path = Path(os.path.expanduser("~/.waggle/merge-strategies.yaml"))
        else:
            config_path = Path(path)
        if not config_path.exists():
            return cls()
        try:
            import yaml  # type: ignore[import]

            data = yaml.safe_load(config_path.read_text())
            if isinstance(data, dict):
                return cls(**data)
        except Exception:
            pass
        return cls()
