from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
import warnings
import webbrowser
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import mcp.server.stdio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from waggle import __version__
from waggle.abhi import (
    ABHI_MERGE_STRATEGIES,
    DEFAULT_ABHI_MERGE_STRATEGY,
    abhi_to_snapshot,
    build_abhi_document,
    execute_abhi_query,
    load_abhi_document,
    serialize_abhi_diff,
    validate_abhi_document,
)
from waggle.config import DEFAULT_DB_PATH, AppConfig
from waggle.embeddings import EMBEDDING_FREE_TOOLS, STATUS_DISABLED, STATUS_READY, EmbeddingModel
from waggle.errors import (
    AuthenticationError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    ValidationFailure,
    WaggleError,
)
from waggle.graph import MemoryGraph
from waggle.graph_ui import render_graph_editor_html
from waggle.logging_utils import configure_logging
from waggle.metrics import MetricsRegistry
from waggle.models import (
    ApiKeyRecord,
    AuditEventRecord,
    ClearScopeResult,
    ConflictEntry,
    ConflictListResult,
    ContextBundleExportResult,
    ContextScopeResult,
    ContextWindow,
    ContextWindowEdge,
    DrivePullResult,
    GraphDiffResult,
    GraphStats,
    MarkdownVaultExportResult,
    MarkdownVaultImportResult,
    Node,
    NodeHistoryResult,
    NodeType,
    ObservationResult,
    PrimeContextResult,
    RelationType,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
    SubgraphResult,
    TimelineResult,
    TopicResult,
    TranscriptIngestionInput,
    utc_now,
)
from waggle.rate_limit import RateLimiter
from waggle.recursive_context import (
    RECURSIVE_CONTEXT_ENABLED,
    RecursiveContextController,
)
from waggle.runtime_context import runtime_context
from waggle.runtime_info import SERVER_NAME, WAGGLE_SERVER_INFO
from waggle.secure_temp import secure_temp_path, write_secure_temp
from waggle.serializer import (
    serialize_abhi_chunk_load,
    serialize_abhi_inspect,
    serialize_abhi_merge,
    serialize_abhi_query,
    serialize_abhi_validation,
    serialize_conflict_entry,
    serialize_conflicts,
    serialize_context_bundle_export,
    serialize_graph_diff,
    serialize_node_history,
    serialize_observation_result,
    serialize_prime_context,
    serialize_recent_nodes,
    serialize_stats,
    serialize_subgraph,
    serialize_timeline,
    serialize_topics,
)

LOGGER = logging.getLogger(__name__)
_DRIVE_SYNC_IMPORT_ERROR: Exception | None = None

try:
    from waggle.drive_sync import (
        download_drive_file,
        ensure_drive_credentials,
        merge_downloaded_abhi,
        push_file_to_drive,
        resolve_drive_file_id,
        share_drive_file,
    )
except Exception as exc:  # pragma: no cover - depends on optional Google libs
    download_drive_file = None
    ensure_drive_credentials = None
    merge_downloaded_abhi = None
    push_file_to_drive = None
    resolve_drive_file_id = None
    share_drive_file = None
    _DRIVE_SYNC_IMPORT_ERROR = exc

_ABHI_IMPORT_STRATEGIES = (
    "skip-existing",
    "overwrite",
    "branch",
)

WRITE_HEAVY_TOOLS = {
    "store_node",
    "store_edge",
    "decompose_and_store",
    "observe_conversation",
    "clear_session",
    "clear_project",
    "clear_all",
    # git-vocabulary names (canonical)
    "pull",
    "merge",
    "grep",
    # legacy aliases kept for backward compatibility
    "import_graph_backup",
    "import_abhi",
    "merge_abhi",
    "load_abhi_chunks",
    "query_abhi",
    "import_markdown_vault",
}
REQUIRED_RUNTIME_METHODS = (
    "export_context_bundle",
    "export_markdown_vault",
    "export_abhi",
    "diff_abhi",
    "import_abhi",
    "merge_abhi",
    "load_abhi_chunks",
    "query_abhi",
    "validate_abhi",
    "inspect_abhi",
    "list_context_scopes",
    "get_node_history",
    "import_markdown_vault",
    "timeline",
    "list_conflicts",
    "resolve_conflict",
    "edge_quality_report",
)

# Mapping from legacy tool names to their canonical git-vocabulary equivalents.
# Both names are accepted; the dispatch normalises to the canonical name.
# Parametric alias table: legacy tool name → (canonical name, default args).
# Default args are merged with caller-provided args; caller wins on collision.
# This means export_context_bundle literally IS commit --commit_format=bundle,
# and callers who pass explicit args still get what they asked for.
_TOOL_ALIASES: dict[str, tuple[str, dict[str, object]]] = {
    "export_graph_backup": ("commit", {"commit_format": "backup"}),
    "export_abhi": ("commit", {"commit_format": "abhi"}),
    "export_context_bundle": ("commit", {"commit_format": "bundle"}),
    "import_graph_backup": ("pull", {"pull_format": "backup"}),
    "import_abhi": ("pull", {"pull_format": "abhi"}),
    "diff_abhi": ("diff", {}),
    "merge_abhi": ("merge", {}),
    "validate_abhi": ("fsck", {}),
    "inspect_abhi": ("show", {}),
    "query_abhi": ("grep", {}),
    # Recursive context assembly aliases
    "recursive_context": ("build_context", {}),
    "assemble_context": ("build_context", {}),
    "rlm_context": ("build_context", {}),
}

_EXPORT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("JWT token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}\b")),
    ("Password assignment", re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*['\"]?\S+")),
    (
        "Secret/token assignment",
        re.compile(r"(?i)\b(api[_ -]?key|secret[_ -]?key|access[_ -]?token)\b\s*[:=]\s*['\"]?\S+"),
    ),
)


def _resolve_passphrase(args: argparse.Namespace) -> str:
    env_name = str(getattr(args, "passphrase_env", "") or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    if bool(getattr(args, "encrypt", False)):
        return getpass.getpass("ABHI passphrase: ").strip()
    return ""


def _resolve_drive_token_path(args: argparse.Namespace, config: AppConfig) -> Path:
    raw = str(getattr(args, "token_path", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    export_root = Path(config.export_dir).expanduser() if config.export_dir else Path.home() / ".waggle"
    return export_root / "google-drive-token.json"


def _serialize_api_key_record(record: ApiKeyRecord) -> dict[str, Any]:
    return {
        "api_key_id": record.api_key_id,
        "tenant_id": record.tenant_id,
        "prefix": record.prefix,
        "name": record.name,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "created_by": record.created_by,
        "scopes": record.scopes,
    }


def _serialize_retention_policy(record: RetentionPolicyRecord) -> dict[str, Any]:
    next_due_at = None
    if record.last_pruned_at is not None:
        next_due_at = record.last_pruned_at + timedelta(hours=record.prune_interval_hours)
    return {
        "tenant_id": record.tenant_id,
        "enabled": record.enabled,
        "retention_days": record.retention_days,
        "prune_interval_hours": record.prune_interval_hours,
        "last_pruned_at": record.last_pruned_at.isoformat() if record.last_pruned_at else None,
        "next_due_at": next_due_at.isoformat() if next_due_at else None,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _serialize_retention_run(record: RetentionPruneRunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "tenant_id": record.tenant_id,
        "status": record.status,
        "cutoff": record.cutoff.isoformat(),
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "deleted_nodes": record.deleted_nodes,
        "deleted_edges": record.deleted_edges,
        "deleted_transcripts": record.deleted_transcripts,
        "deleted_context_windows": record.deleted_context_windows,
        "deleted_context_window_edges": record.deleted_context_window_edges,
        "deleted_exports": record.deleted_exports,
        "duration_ms": record.duration_ms,
        "error_message": record.error_message,
    }


def _serialize_audit_event(record: AuditEventRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "tenant_id": record.tenant_id,
        "event_type": record.event_type,
        "actor_type": record.actor_type,
        "actor_id": record.actor_id,
        "api_key_id": record.api_key_id,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "action": record.action,
        "status": record.status,
        "ip_address": record.ip_address,
        "user_agent": record.user_agent,
        "created_at": record.created_at.isoformat(),
        "metadata": record.metadata,
    }


def _parse_api_key_scopes(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _require_drive_sync() -> None:
    if _DRIVE_SYNC_IMPORT_ERROR is None:
        return
    raise ValidationFailure(
        "Google Drive sync requires optional Google API dependencies in the active environment. "
        f"Original import error: {_DRIVE_SYNC_IMPORT_ERROR}"
    )


def _scan_export_transcripts_for_secrets(
    backend: MemoryGraph,
    *,
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    scope: str = "all",
    since_date: str = "",
    max_findings: int = 10,
) -> list[dict[str, Any]]:
    snapshot = backend.get_graph_snapshot()
    document = build_abhi_document(
        snapshot,
        scope=scope,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        since_date=since_date,
        include_embeddings=False,
        encrypted=False,
    )
    findings: list[dict[str, Any]] = []
    for row in document.get("transcripts", []):
        text = str(row.get("transcript_text", ""))
        if not text.strip():
            continue
        for label, pattern in _EXPORT_SECRET_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            secret = match.group(0)
            preview = text.replace(secret, "[REDACTED]")
            findings.append(
                {
                    "pattern": label,
                    "transcript_id": str(row.get("id", "")),
                    "session_id": str(row.get("session_id", "")),
                    "turn_index": int(row.get("turn_index", 0) or 0),
                    "role": str(row.get("role", "")),
                    "preview": preview[:180],
                }
            )
            break
        if len(findings) >= max_findings:
            break
    return findings


def _assert_export_safe(
    backend: MemoryGraph,
    *,
    force: bool,
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    scope: str = "all",
    since_date: str = "",
) -> None:
    findings = _scan_export_transcripts_for_secrets(
        backend,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        scope=scope,
        since_date=since_date,
    )
    if findings and not force:
        summary = "; ".join(
            f"{item['pattern']} in {item['role']} turn {item['turn_index']} of session {item['session_id'] or 'default'}"
            for item in findings[:3]
        )
        raise ValidationFailure(
            "Export refused because transcript_records appear to contain secrets. "
            f"Run again with --force only after redacting or confirming the export scope is safe. Findings: {summary}."
        )


MEMORY_AUTOMATION_POLICY = """Waggle automatic memory policy

The user should not manually manage memory. The assistant/runtime is responsible for using Waggle tools.
Waggle should remember relevant conversational context automatically. If memory looks empty, the likely issue is that this session is not loading the automatic memory policy or is bypassing the orchestrated runtime hooks.

Before answering:
- Use prime_context at the start of a new session when project, agent, or session scope is known.
- Use query_graph before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context.
- Keep retrieval narrow: start with max_nodes 8-12, max_depth 1-2, retrieval_mode hybrid. Use graph only when transcript evidence is not needed.

After answering:
- Use observe_conversation after completed turns that contain durable information: decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes.
- Do not call store_node for normal conversation memory unless the user explicitly gives one atomic fact and no inference is needed.
- Skip memory writes for acknowledgements, greetings, short chatter, or failed/aborted work.

Scoping:
- Always pass stable project, agent_id, and session_id when available.
- Prefer scoped memory over global memory in shared workspaces.
"""

AUTOMATIC_MEMORY_RULE_TEXT = """Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call prime_context.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call query_graph with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call observe_conversation automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call build_context before answers and on_assistant_turn after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.
"""

_AGENTS_MEMORY_BLOCK_HEADER = "<!-- waggle:auto-memory:start -->"
_AGENTS_MEMORY_BLOCK_FOOTER = "<!-- waggle:auto-memory:end -->"
_AGENTS_MEMORY_BLOCK = (
    f"{_AGENTS_MEMORY_BLOCK_HEADER}\n"
    "## Waggle Automatic Memory\n\n"
    f"{AUTOMATIC_MEMORY_RULE_TEXT.rstrip()}\n"
    f"{_AGENTS_MEMORY_BLOCK_FOOTER}\n"
)


def _object_input_schema(
    properties: dict[str, Any] | None = None,
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _scope_properties() -> dict[str, dict[str, Any]]:
    return {
        "agent_id": {
            "type": "string",
            "default": "",
            "description": "Optional agent or client identifier used to partition memory.",
        },
        "project": {
            "type": "string",
            "default": "",
            "description": "Optional project or workspace name used to partition memory.",
        },
        "session_id": {
            "type": "string",
            "default": "",
            "description": "Optional conversation or run identifier used to partition memory.",
        },
    }


def _assert_runtime_feature_parity() -> None:
    missing = [name for name in REQUIRED_RUNTIME_METHODS if not hasattr(MemoryGraph, name)]
    if not missing:
        return
    joined = ", ".join(missing)
    raise RuntimeError(
        "Detected a stale waggle runtime on the import path. Missing methods: "
        f"{joined}. This usually means an older copied package in site-packages is "
        "shadowing the current source tree or editable install. Recreate the virtualenv "
        "or uninstall old waggle/graph-memory-mcp builds before running waggle-mcp."
    )


def _build_backend(config: AppConfig) -> Any:
    embedding_model = EmbeddingModel(
        config.model_name,
        embedding_backend=config.embedding_backend,
    )
    # Disable ML entirely in fast/inspection mode.
    if config.is_fast_mode:
        embedding_model.disable_warmup()
    if config.backend == "sqlite":
        return MemoryGraph(
            config.db_path,
            embedding_model,
            tenant_id=config.default_tenant_id,
            dedup_similarity_threshold=config.dedup_threshold,
            recency_half_life_days=config.recency_half_life_days,
            tiered_retrieval=config.tiered_retrieval,
            tiered_retrieval_top_k_windows=config.tiered_retrieval_top_k_windows,
            hybrid_retrieval_config=config.hybrid_retrieval_config(),
            export_dir=config.export_dir,
            api_key_environment=config.api_key_environment,
        )
    from waggle.neo4j_graph import Neo4jMemoryGraph

    return Neo4jMemoryGraph(
        uri=config.neo4j_uri,
        username=config.neo4j_username,
        password=config.neo4j_password,
        database=config.neo4j_database or None,
        embedding_model=embedding_model,
        tenant_id=config.default_tenant_id,
        export_dir=config.export_dir,
        api_key_environment=config.api_key_environment,
    )


class WaggleServer:
    """MCP server wrapper with tenant-aware graph resolution."""

    def __init__(
        self,
        graph: Any | None = None,
        *,
        config: AppConfig | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.metrics = metrics or MetricsRegistry()
        self._static_graph = graph
        self._root_graph = graph or _build_backend(self.config)
        self.server = Server("waggle")
        self._register_handlers()

    @property
    def graph(self) -> Any:
        return self.current_graph()

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return self.build_tools()

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await anyio.to_thread.run_sync(self.handle_tool_call, name, arguments or {})

        @self.server.list_resources()
        async def list_resources(
            request: types.ListResourcesRequest | None = None,
        ) -> types.ListResourcesResult:
            del request
            return self.build_resources()

        @self.server.read_resource()
        async def read_resource(uri: Any) -> str:
            return self.read_resource_text(str(uri))

        @self.server.list_prompts()
        async def list_prompts() -> list[types.Prompt]:
            return self.build_prompts()

        @self.server.get_prompt()
        async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
            return self.get_prompt_result(name, arguments or {})

    def build_tools(self) -> list[types.Tool]:
        return [
            types.Tool(
                name="store_node",
                description=(
                    "Store a piece of knowledge as a node in the persistent memory graph. "
                    "Call this whenever you learn something important from the user: facts, "
                    "preferences, decisions, entities, concepts, or questions. Prefer atomic facts."
                ),
                inputSchema=_object_input_schema(
                    {
                        "label": {"type": "string", "description": "Short label for the knowledge being stored."},
                        "content": {
                            "type": "string",
                            "description": "Full natural-language description for this node.",
                        },
                        "node_type": {
                            "type": "string",
                            "enum": [node_type.value for node_type in NodeType],
                            "description": "Category of knowledge represented by the node.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags for categorization.",
                            "default": [],
                        },
                        "source_prompt": {
                            "type": "string",
                            "description": "Optional original prompt that produced this knowledge.",
                            "default": "",
                        },
                        **_scope_properties(),
                    },
                    required=["label", "content", "node_type"],
                ),
            ),
            types.Tool(
                name="store_edge",
                description=(
                    "Create a relationship between two stored nodes. Use this immediately after "
                    "storing related nodes so the memory graph preserves structure, updates, and conflicts."
                ),
                inputSchema=_object_input_schema(
                    {
                        "source_id": {"type": "string", "description": "Source node ID."},
                        "target_id": {"type": "string", "description": "Target node ID."},
                        "relationship": {
                            "type": "string",
                            "enum": [relation.value for relation in RelationType],
                            "description": "Relationship between the two nodes.",
                        },
                        "weight": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "default": 1.0,
                            "description": "Optional strength of the relationship.",
                        },
                    },
                    required=["source_id", "target_id", "relationship"],
                ),
            ),
            types.Tool(
                name="canonicalize_node",
                description=(
                    "Manually merge multiple nodes into a single canonical node. "
                    "All aliases from the merged nodes flow into the canonical node's aliases. "
                    "All edges pointing to/from merged nodes are re-pointed to the canonical node. "
                    "Merged nodes are deleted.  Idempotent: merging an already-merged node is a no-op. "
                    "Use this after reviewing dedup_candidates to resolve ambiguous duplicates."
                ),
                inputSchema=_object_input_schema(
                    {
                        "node_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of node IDs to merge into the canonical node.",
                        },
                        "canonical_id": {"type": "string", "description": "The canonical node ID to merge into."},
                        **_scope_properties(),
                    },
                    required=["node_ids", "canonical_id"],
                ),
            ),
            types.Tool(
                name="dedup_candidates",
                description=(
                    "Return pairs of nodes whose embeddings are above a threshold but below the "
                    "auto-merge threshold.  Intended for human review before calling canonicalize_node. "
                    "Returns pairs sorted by descending similarity so the most likely duplicates appear first."
                ),
                inputSchema=_object_input_schema(
                    {
                        "project": {
                            "type": "string",
                            "default": "",
                            "description": "Optional project scope to filter candidates.",
                        },
                        "agent_id": {
                            "type": "string",
                            "default": "",
                            "description": "Optional agent scope to filter candidates.",
                        },
                        "session_id": {
                            "type": "string",
                            "default": "",
                            "description": "Optional session scope to filter candidates.",
                        },
                        "threshold": {
                            "type": "number",
                            "minimum": 0.85,
                            "maximum": 0.99,
                            "default": 0.85,
                            "description": "Minimum cosine similarity to report (default 0.85).",
                        },
                    },
                ),
            ),
            types.Tool(
                name="aggregate_graph",
                description=(
                    "Retrieve a broad set of nodes bypassing standard semantic limits, optimized for "
                    "global aggregation and map-reduce tasks. Supports filtering by node_type and tags."
                ),
                inputSchema=_object_input_schema(
                    {
                        "query": {
                            "type": "string",
                            "description": "Optional natural-language search query to rank the broad retrieval.",
                        },
                        "node_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of node types to filter by (e.g., 'fact', 'entity').",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of tags to require.",
                        },
                        "max_nodes": {
                            "type": "integer",
                            "description": "Maximum number of nodes to return (default 100, up to 1000).",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Relationship traversal depth around matching nodes.",
                        },
                        "include_invalidated": {
                            "type": "boolean",
                            "default": False,
                            "description": "When true, include nodes whose valid_to has passed. Default false excludes expired nodes.",
                        },
                        "as_of": {
                            "type": "string",
                            "description": "ISO-8601 datetime. When provided, return only nodes valid at that point in time (overrides include_invalidated).",
                        },
                        **_scope_properties(),
                    },
                ),
            ),
            types.Tool(
                name="query_graph",
                description=(
                    "Automatically search the memory graph before answering questions that may depend on prior context, "
                    "user preferences, project decisions, constraints, or earlier conversation state. "
                    "Returns a serialized subgraph with matching nodes and their connected neighborhood. "
                    "Uses hybrid retrieval (transcript + graph) by default for robust fallback. "
                    "Understands temporal references such as 'recently', 'latest', 'originally', and 'last week'. "
                    "Benchmark modes: use retrieval_mode='graph' for graph-only (no verbatim fallback), 'verbatim' for transcript-only."
                ),
                inputSchema=_object_input_schema(
                    {
                        "query": {"type": "string", "description": "Natural-language search query."},
                        "max_nodes": {
                            "type": "integer",
                            "default": 20,
                            "minimum": 1,
                            "description": "Maximum number of matching nodes to return.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 0,
                            "description": "Relationship traversal depth around matching nodes.",
                        },
                        "expand_depth": {
                            "type": "integer",
                            "default": 0,
                            "minimum": 0,
                            "description": "Optional support expansion depth. At 1, graph mode may return up to twice max_nodes.",
                        },
                        **_scope_properties(),
                        "retrieval_mode": {
                            "type": "string",
                            "enum": ["graph", "verbatim", "hybrid"],
                            "default": "hybrid",
                            "description": "Retrieval strategy: graph-only, verbatim transcript retrieval, or hybrid fusion with reranking.",
                        },
                        "include_invalidated": {
                            "type": "boolean",
                            "default": False,
                            "description": "When true, include nodes whose valid_to has passed. Default false excludes expired nodes.",
                        },
                        "as_of": {
                            "type": "string",
                            "description": "ISO-8601 datetime. When provided, return only nodes valid at that point in time (overrides include_invalidated).",
                        },
                    },
                    required=["query"],
                ),
            ),
            types.Tool(
                name="debug_retrieval",
                description=(
                    "Diagnose memory retrieval ranking for a query. Returns query embedding preview, context-window "
                    "routing scores, selected windows, flat top nodes, and tiered top nodes for comparison."
                ),
                inputSchema=_object_input_schema(
                    {
                        "query": {"type": "string", "description": "Natural-language search query to diagnose."},
                        "max_nodes": {
                            "type": "integer",
                            "default": 10,
                            "minimum": 1,
                            "description": "Maximum number of flat and tiered node matches to include.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 0,
                            "description": "Relationship traversal depth for the flat retrieval comparison.",
                        },
                        "retrieval_mode": {
                            "type": "string",
                            "enum": ["graph", "verbatim", "hybrid"],
                            "default": "hybrid",
                            "description": "Which retrieval stack to diagnose.",
                        },
                        **_scope_properties(),
                    },
                    required=["query"],
                ),
            ),
            types.Tool(
                name="get_related",
                description=(
                    "Fetch the neighborhood around a specific memory node. Use when you already have a node ID "
                    "and need its connected context. Returns matching nodes and edges as a serialized subgraph."
                ),
                inputSchema=_object_input_schema(
                    {
                        "node_id": {
                            "type": "string",
                            "description": "ID of the node whose neighborhood should be returned.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 0,
                            "description": "Relationship traversal depth from the starting node.",
                        },
                    },
                    required=["node_id"],
                ),
            ),
            types.Tool(
                name="get_node_history",
                description=(
                    "Inspect one memory node's evidence, validity window, and connected context. Use when auditing "
                    "why a memory exists or how it changed. Returns the node, evidence records, related nodes, and edges."
                ),
                inputSchema=_object_input_schema(
                    {
                        "node_id": {"type": "string", "description": "ID of the node to inspect."},
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 0,
                            "description": "Relationship traversal depth for related context.",
                        },
                    },
                    required=["node_id"],
                ),
            ),
            types.Tool(
                name="list_context_scopes",
                description=(
                    "List known agent, project, and session scope values stored in the current tenant graph. "
                    "Use before filtering memory by scope. Returns arrays of scope identifiers."
                ),
                inputSchema=_object_input_schema(),
            ),
            types.Tool(
                name="list_context_windows",
                description=(
                    "List context windows for a project. Use to inspect chat/session-level memory containers, "
                    "their status, node counts, and update times."
                ),
                inputSchema=_object_input_schema(
                    {
                        "project": {
                            "type": "string",
                            "description": "Optional project/repository scope to filter windows.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["active", "closed", "archived"],
                            "description": "Optional status filter for returned windows.",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 20,
                            "minimum": 1,
                            "description": "Maximum number of context windows to return.",
                        },
                    }
                ),
            ),
            types.Tool(
                name="get_context_window",
                description=(
                    "Inspect one context window, including its nodes and links to other context windows. "
                    "Use when auditing what a conversation/session contributed to memory."
                ),
                inputSchema=_object_input_schema(
                    {
                        "window_id": {"type": "string", "description": "ID of the context window to inspect."},
                        "include_nodes": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether to include memory nodes stored in this context window.",
                        },
                    },
                    required=["window_id"],
                ),
            ),
            types.Tool(
                name="close_context_window",
                description=(
                    "Close a context window, recompute its final graph embedding, refresh node counts, "
                    "and derive cross-window edges. Use when a chat/session is complete."
                ),
                inputSchema=_object_input_schema(
                    {"window_id": {"type": "string", "description": "ID of the context window to close."}},
                    required=["window_id"],
                ),
            ),
            types.Tool(
                name="timeline",
                description=(
                    "Build a chronological view of memory changes for a node, a query result, or the whole tenant. "
                    "Use when order and evidence matter. Returns timestamped timeline items."
                ),
                inputSchema=_object_input_schema(
                    {
                        "node_id": {"type": "string", "description": "Optional node ID to anchor the timeline."},
                        "query": {
                            "type": "string",
                            "description": "Optional natural-language query to select relevant memories.",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 25,
                            "minimum": 1,
                            "description": "Maximum number of timeline items to return.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 0,
                            "description": "Relationship traversal depth when a node ID or query is supplied.",
                        },
                        "include_evidence": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether to include evidence records alongside node and edge events.",
                        },
                    },
                ),
            ),
            types.Tool(
                name="list_conflicts",
                description=(
                    "List contradiction and update edges, with unresolved conflicts shown by default. "
                    "Use to review memory disagreements before resolving them. Returns conflict entries with source and target nodes."
                ),
                inputSchema=_object_input_schema(
                    {
                        "include_resolved": {
                            "type": "boolean",
                            "default": False,
                            "description": "Whether to include conflicts that were already marked resolved.",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 25,
                            "minimum": 1,
                            "description": "Maximum number of conflicts to return.",
                        },
                    },
                ),
            ),
            types.Tool(
                name="resolve_conflict",
                description=(
                    "Mark a contradiction or update edge as resolved without deleting the underlying history. "
                    "Use after deciding how competing memories should be interpreted. Returns the resolved conflict entry. "
                    "When winner is provided and the edge is CONTRADICTS or UPDATES, the losing node's valid_to is set to now, "
                    "excluding it from future default queries."
                ),
                inputSchema=_object_input_schema(
                    {
                        "edge_id": {"type": "string", "description": "ID of the conflict edge to mark resolved."},
                        "resolution_note": {
                            "type": "string",
                            "default": "",
                            "description": "Optional human-readable note explaining the resolution.",
                        },
                        "winner": {
                            "type": "string",
                            "description": "Optional node ID of the winning node. Must be source_id or target_id of the edge. "
                            "When provided, the losing node's valid_to is set to now, superseding it.",
                        },
                    },
                    required=["edge_id"],
                ),
            ),
            types.Tool(
                name="update_node",
                description=(
                    "Update an existing memory node's content, label, or tags. Use when a stored memory needs correction "
                    "without deleting its identity. Returns the updated node."
                ),
                inputSchema=_object_input_schema(
                    {
                        "node_id": {"type": "string", "description": "ID of the node to update."},
                        "content": {
                            "type": "string",
                            "description": "Replacement natural-language content for the node.",
                        },
                        "label": {"type": "string", "description": "Replacement short label for the node."},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Replacement tag list for the node.",
                        },
                    },
                    required=["node_id"],
                ),
            ),
            types.Tool(
                name="delete_node",
                description="Delete a node and all connected edges from persistent memory.",
                inputSchema=_object_input_schema(
                    {"node_id": {"type": "string", "description": "ID of the node to delete."}},
                    required=["node_id"],
                ),
            ),
            types.Tool(
                name="clear_session",
                description=(
                    "Delete all memory data for one session/context window stream, including nodes, transcripts, "
                    "context windows, and connected edges. Requires confirm=true."
                ),
                inputSchema=_object_input_schema(
                    {
                        "session_id": {"type": "string", "description": "Session identifier to clear."},
                        "confirm": {
                            "type": "boolean",
                            "default": False,
                            "description": "Must be true to perform the destructive clear operation.",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "default": False,
                            "description": "Preview the clear operation without deleting data.",
                        },
                    },
                    required=["session_id"],
                ),
            ),
            types.Tool(
                name="clear_project",
                description=(
                    "Delete all memory data for one project/repository, including nodes, transcripts, repos, "
                    "context windows, and connected edges. Requires confirm=true."
                ),
                inputSchema=_object_input_schema(
                    {
                        "project": {"type": "string", "description": "Project/repository scope to clear."},
                        "confirm": {
                            "type": "boolean",
                            "default": False,
                            "description": "Must be true to perform the destructive clear operation.",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "default": False,
                            "description": "Preview the clear operation without deleting data.",
                        },
                    },
                    required=["project"],
                ),
            ),
            types.Tool(
                name="clear_all",
                description=(
                    "Delete all graph memory data for the current tenant. Requires confirm=true. "
                    "This does not remove API keys or tenant metadata."
                ),
                inputSchema=_object_input_schema(
                    {
                        "confirm": {
                            "type": "boolean",
                            "default": False,
                            "description": "Must be true to perform the destructive clear operation.",
                        },
                        "dry_run": {
                            "type": "boolean",
                            "default": False,
                            "description": "Preview the clear operation without deleting data.",
                        },
                    }
                ),
            ),
            types.Tool(
                name="decompose_and_store",
                description=(
                    "Break long or complex content into atomic memory nodes, store them automatically, and create inferred edges. "
                    "Use for notes, summaries, or multi-fact passages. Returns the stored subgraph."
                ),
                inputSchema=_object_input_schema(
                    {
                        "content": {
                            "type": "string",
                            "description": "Long-form content to decompose into memory nodes.",
                        },
                        "context": {
                            "type": "string",
                            "default": "",
                            "description": "Optional background that helps classify and connect extracted memories.",
                        },
                    },
                    required=["content"],
                ),
            ),
            types.Tool(
                name="observe_conversation",
                description=(
                    "Automatically observe a completed user-assistant turn. ALWAYS persists the verbatim turn first. "
                    "Then runs extraction (graph inference) as optional enrichment. If extraction fails, the verbatim turn is still stored. "
                    "Use after turns containing preferences, decisions, constraints, requirements, corrections, project facts, "
                    "or meaningful task outcomes. Do not ask the user to trigger this. "
                    "Returns: turn_id, verbatim_stored (bool), nodes_extracted (count), edges_inferred (count), extraction_errors (non-fatal). "
                    "Required fields: 'user_message' (the user's text) and 'assistant_response' (the assistant's reply). "
                    "Do NOT use 'user_text' or 'assistant_text' — those field names are not accepted."
                ),
                inputSchema=_object_input_schema(
                    {
                        "user_message": {
                            "type": "string",
                            "description": "The user's message from the completed turn.",
                        },
                        "assistant_response": {
                            "type": "string",
                            "description": "The assistant's response from the completed turn.",
                        },
                        **_scope_properties(),
                    },
                    required=["user_message", "assistant_response"],
                ),
            ),
            types.Tool(
                name="graph_diff",
                description=(
                    "Show what changed in the memory graph recently, including added nodes, updated nodes, created edges, "
                    "and contradiction edges. Use for review or handoff. Returns a serialized graph diff."
                ),
                inputSchema=_object_input_schema(
                    {
                        "since": {
                            "type": "string",
                            "default": "24h",
                            "description": "Lookback window such as '24h', '7d', or an ISO-like timestamp.",
                        }
                    }
                ),
            ),
            types.Tool(
                name="prime_context",
                description=(
                    "Automatically build a compact context brief at the start of a scoped conversation or before work that needs continuity. "
                    "Use to hydrate an assistant with the most relevant scoped memories. Returns summary text plus nodes and edges."
                ),
                inputSchema=_object_input_schema(_scope_properties()),
            ),
            types.Tool(
                name="get_topics",
                description=(
                    "Detect topic clusters in the graph using community detection. Use to understand the main themes "
                    "in memory. Returns labeled clusters with representative nodes and tags. "
                    "Note: scope filtering (project, agent_id, session_id) is optional and silently ignored — "
                    "topic detection always runs across the full tenant graph."
                ),
                inputSchema=_object_input_schema(_scope_properties()),
            ),
            types.Tool(
                name="get_stats",
                description=(
                    "Return high-level statistics about the current memory graph. Use for health checks or quick summaries. "
                    "Returns node and edge counts, node type breakdowns, and recent or highly connected nodes."
                ),
                inputSchema=_object_input_schema(),
            ),
            types.Tool(
                name="export_graph_html",
                description=(
                    "Export the current memory graph as an interactive HTML visualization. "
                    "Use when a human needs to inspect the graph visually. Returns the output path and graph counts."
                ),
                inputSchema=_object_input_schema(
                    {
                        "output_path": {
                            "type": "string",
                            "description": "Optional destination HTML file path. If omitted, Waggle chooses an export path.",
                        },
                        "include_physics": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether the visualization should use physics-based node layout.",
                        },
                    },
                ),
            ),
            types.Tool(
                name="window_graph_viz",
                description=(
                    "Export the context-window graph as an interactive HTML visualization. "
                    "Each node is a chat/session window and edges show overlap, supersession, temporal order, or shared scope."
                ),
                inputSchema=_object_input_schema(
                    {
                        "project": {
                            "type": "string",
                            "description": "Optional project/repository scope whose context-window graph should be exported.",
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Optional destination HTML file path. If omitted, Waggle chooses an export path.",
                        },
                        "include_physics": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether the visualization should use physics-based node layout.",
                        },
                    },
                ),
            ),
            types.Tool(
                name="commit",
                description=(
                    "Snapshot the current memory graph to a portable file (waggle commit). "
                    "Exports a JSON backup for migration, restore drills, or offline archive. "
                    "Use commit_format='abhi' (default) for a full .abhi export, or 'backup' for a raw JSON backup. "
                    "Returns the output path, schema version, and object counts."
                ),
                inputSchema=_object_input_schema(
                    {
                        "output_path": {
                            "type": "string",
                            "description": "Optional destination file path. If omitted, Waggle chooses an export path.",
                        },
                        "commit_format": {
                            "type": "string",
                            "enum": ["abhi", "backup", "bundle"],
                            "default": "abhi",
                            "description": (
                                "'abhi' (default) exports a validated .abhi memory file; "
                                "'backup' exports a raw JSON backup; "
                                "'bundle' exports a portable Markdown/JSON context bundle."
                            ),
                        },
                        "force": {
                            "type": "boolean",
                            "default": False,
                            "description": "Override the secret-scan refusal if transcript records contain likely secrets. Use only after deliberate review.",
                        },
                        "include_low_confidence_edges": {
                            "type": "boolean",
                            "default": False,
                            "description": "When true, include RELATES_TO edges with edge_confidence < 0.7 that are normally filtered from exports.",
                        },
                        **_scope_properties(),
                    }
                ),
            ),
            types.Tool(
                name="pull",
                description=(
                    "Load a memory file into the current graph (waggle pull). "
                    "Accepts a .abhi file (default) or a raw JSON backup. "
                    "Runs integrity verification, schema validation, and constraint checks before merging. "
                    "Returns counts for created and updated nodes and edges."
                ),
                inputSchema=_object_input_schema(
                    {
                        "input_path": {
                            "type": "string",
                            "description": "Path to the .abhi or JSON backup file to import.",
                        },
                        "pull_format": {
                            "type": "string",
                            "enum": ["abhi", "backup"],
                            "default": "abhi",
                            "description": "'abhi' (default) imports a .abhi memory file; 'backup' imports a raw JSON backup.",
                        },
                    },
                    required=["input_path"],
                ),
            ),
            types.Tool(
                name="diff",
                description=(
                    "Compare two .abhi memory files (waggle diff). "
                    "Reports structural graph changes — added/removed/updated nodes and edges — "
                    "plus lightweight semantic changes. The output is the screenshot that goes on the homepage."
                ),
                inputSchema=_object_input_schema(
                    {
                        "input_path_a": {
                            "type": "string",
                            "description": "Path to the first .abhi file (base / ours).",
                        },
                        "input_path_b": {
                            "type": "string",
                            "description": "Path to the second .abhi file (theirs / feature branch).",
                        },
                    },
                    required=["input_path_a", "input_path_b"],
                ),
            ),
            types.Tool(
                name="merge",
                description=(
                    "Three-way merge branching .abhi memory files (waggle merge). "
                    "Merges left and right branches against a common base into one output file. "
                    "Conflicts surface as CONTRADICTS edges — nobody else can do this. "
                    "Use --merge-strategy to control winner selection when both sides changed the same object."
                ),
                inputSchema=_object_input_schema(
                    {
                        "base_input_path": {"type": "string", "description": "Path to the common base .abhi file."},
                        "left_input_path": {
                            "type": "string",
                            "description": "Path to the left branch .abhi file (ours).",
                        },
                        "right_input_path": {
                            "type": "string",
                            "description": "Path to the right branch .abhi file (theirs).",
                        },
                        "output_path": {"type": "string", "description": "Destination path for the merged .abhi file."},
                        "merge_strategy": {
                            "type": "string",
                            "enum": ["prefer_right", "prefer_left", "last_write_wins"],
                            "default": "prefer_right",
                            "description": "Winner strategy when both sides changed the same object differently.",
                        },
                    },
                    required=["base_input_path", "left_input_path", "right_input_path", "output_path"],
                ),
            ),
            types.Tool(
                name="grep",
                description=(
                    "Execute a saved or ad hoc query against an .abhi file (waggle grep). "
                    "Triggers the file's on_query event actions and returns matching nodes."
                ),
                inputSchema=_object_input_schema(
                    {
                        "input_path": {"type": "string", "description": "Path to the .abhi file to query."},
                        "query_id": {"type": "string", "description": "Optional saved query id from the file."},
                        "query_text": {"type": "string", "description": "Optional ad hoc query text to execute."},
                    },
                    required=["input_path"],
                ),
            ),
            types.Tool(
                name="load_abhi_chunks",
                description=(
                    "Load only selected or query-relevant chunks from an .abhi file for partial graph inspection."
                ),
                inputSchema=_object_input_schema(
                    {
                        "input_path": {"type": "string", "description": "Path to the .abhi file to inspect."},
                        "chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional explicit chunk ids to load.",
                        },
                        "query_id": {"type": "string", "description": "Optional saved query id used to select chunks."},
                        "query_text": {
                            "type": "string",
                            "description": "Optional ad hoc query text used to select chunks.",
                        },
                    },
                    required=["input_path"],
                ),
            ),
            types.Tool(
                name="fsck",
                description=(
                    "Validate an .abhi memory file without importing it (waggle fsck). "
                    "Verifies integrity hash, schema compliance, and constraint satisfaction. "
                    "Like git fsck — run this before trusting a file you received."
                ),
                inputSchema=_object_input_schema(
                    {"input_path": {"type": "string", "description": "Path to the .abhi file to validate."}},
                    required=["input_path"],
                ),
            ),
            types.Tool(
                name="show",
                description=(
                    "Inspect an .abhi memory file without loading it into the graph (waggle show). "
                    "Returns summary stats, node/edge type breakdowns, and metadata counts. "
                    "Like git show — quick read-only inspection of a commit object."
                ),
                inputSchema=_object_input_schema(
                    {"input_path": {"type": "string", "description": "Path to the .abhi file to inspect."}},
                    required=["input_path"],
                ),
            ),
            types.Tool(
                name="export_markdown_vault",
                description=(
                    "Export the current graph as an Obsidian-compatible Markdown vault. "
                    "Use when a human wants browsable note files with graph links. Returns written files and graph counts."
                ),
                inputSchema=_object_input_schema(
                    {
                        "root_path": {"type": "string", "description": "Destination directory for the Markdown vault."},
                        **_scope_properties(),
                    },
                    required=["root_path"],
                ),
            ),
            types.Tool(
                name="import_markdown_vault",
                description=(
                    "Import an Obsidian-compatible Markdown vault into the current graph non-destructively. "
                    "Use to sync edited vault notes back into memory. Returns created, updated, deleted-edge, and conflict counts."
                ),
                inputSchema=_object_input_schema(
                    {
                        "root_path": {
                            "type": "string",
                            "description": "Source directory of the Markdown vault to import.",
                        }
                    },
                    required=["root_path"],
                ),
            ),
            types.Tool(
                name="edge_quality_report",
                description=(
                    "Audit the quality of relationship edges in the memory graph. "
                    "Returns counts per edge type, average edge_confidence per type, and the top-10 "
                    "highest- and lowest-confidence edges for each type. "
                    "Useful for diagnosing graph health and identifying noisy RELATES_TO edges."
                ),
                inputSchema=_object_input_schema(
                    {
                        **_scope_properties(),
                    }
                ),
            ),
            *(
                [
                    types.Tool(
                        name="build_context",
                        description=(
                            "Recursively retrieves and compresses relevant Waggle memory for the current task, "
                            "using graph, hybrid, transcript, update, and conflict-aware retrieval. "
                            "Decomposes the query into targeted subqueries, expands the graph around key nodes, "
                            "resolves contradictions and superseded memories, and returns a compact context pack "
                            "under a configurable token budget. "
                            "Aliases: recursive_context, assemble_context, rlm_context."
                        ),
                        inputSchema=_object_input_schema(
                            {
                                "query": {
                                    "type": "string",
                                    "description": "Current user task or question to build context for.",
                                },
                                **_scope_properties(),
                                "token_budget": {
                                    "type": "integer",
                                    "default": 1200,
                                    "description": "Maximum token budget for the context pack (approximate).",
                                },
                                "depth": {
                                    "type": "integer",
                                    "default": 2,
                                    "minimum": 0,
                                    "description": "Graph expansion depth around retrieved nodes.",
                                },
                                "max_subqueries": {
                                    "type": "integer",
                                    "default": 6,
                                    "minimum": 1,
                                    "description": "Maximum number of decomposed subqueries to run.",
                                },
                                "include_evidence": {
                                    "type": "boolean",
                                    "default": True,
                                    "description": "Whether to include verbatim transcript evidence in the context pack.",
                                },
                                "mode": {
                                    "type": "string",
                                    "enum": ["fast", "balanced", "deep"],
                                    "default": "balanced",
                                    "description": (
                                        "Retrieval depth mode: "
                                        "'fast' runs fewer subqueries for low latency; "
                                        "'balanced' is the default; "
                                        "'deep' adds extra subqueries for thorough coverage."
                                    ),
                                },
                            },
                            required=["query"],
                        ),
                    )
                ]
                if RECURSIVE_CONTEXT_ENABLED
                else []
            ),
        ]

    def build_prompts(self) -> list[types.Prompt]:
        return [
            types.Prompt(
                name="waggle_memory_policy",
                title="Waggle Memory Policy",
                description=(
                    "Instructions for automatic memory retrieval and ingestion. "
                    "Use this prompt to make the assistant handle memory without user-triggered tool calls."
                ),
                arguments=[
                    types.PromptArgument(
                        name="project",
                        description="Optional project/workspace scope to pass to Waggle tools.",
                        required=False,
                    ),
                    types.PromptArgument(
                        name="agent_id",
                        description="Optional agent/client identifier to pass to Waggle tools.",
                        required=False,
                    ),
                    types.PromptArgument(
                        name="session_id",
                        description="Optional conversation/session identifier to pass to Waggle tools.",
                        required=False,
                    ),
                ],
            )
        ]

    def get_prompt_result(self, name: str, arguments: dict[str, str]) -> types.GetPromptResult:
        if name != "waggle_memory_policy":
            raise ValidationFailure(f"Unknown prompt: {name}")
        project = str(arguments.get("project", "")).strip()
        agent_id = str(arguments.get("agent_id", "")).strip()
        session_id = str(arguments.get("session_id", "")).strip()
        scope_lines = []
        if project:
            scope_lines.append(f"- project: {project}")
        if agent_id:
            scope_lines.append(f"- agent_id: {agent_id}")
        if session_id:
            scope_lines.append(f"- session_id: {session_id}")
        scope_text = "\n".join(scope_lines) if scope_lines else "- no explicit scope supplied"
        text = f"{MEMORY_AUTOMATION_POLICY}\nSuggested scope for this conversation:\n{scope_text}\n"
        return types.GetPromptResult(
            description="Automatic Waggle memory policy.",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=text),
                )
            ],
        )

    def _get_request(self) -> Request | None:
        try:
            current = request_ctx.get()
        except LookupError:
            return None
        return current.request if isinstance(current.request, Request) else None

    def current_graph(self) -> Any:
        request = self._get_request()
        if request is not None and getattr(request.state, "tenant_id", ""):
            return self._root_graph.for_tenant(request.state.tenant_id)
        return self._root_graph.for_tenant(self.config.default_tenant_id)

    def validate_startup(self) -> None:
        graph = self.current_graph()
        started = time.perf_counter()
        graph.ensure_tenant(graph.tenant_id)
        if self.config.api_key_environment == "live" and self.config.default_tenant_id == "local-default":
            LOGGER.warning(
                "WAGGLE_API_KEY_ENVIRONMENT is set to 'live' but "
                "WAGGLE_DEFAULT_TENANT_ID is still 'local-default'. "
                "Production deployments should use a unique tenant ID."
            )
        em = graph.embedding_model
        if self.config.is_fast_mode:
            # Fast mode: zero ML overhead. Schema inspection is the goal.
            LOGGER.info(
                "startup_fast_mode",
                extra={"startup_mode": self.config.startup_mode},
            )
        elif self.config.is_strict_mode:
            # Strict mode: block here until the model is fully loaded.
            LOGGER.info(
                "startup_strict_mode_waiting_for_embedding",
                extra={"model": em.model_name},
            )
            try:
                em.embed("startup validation", wait_timeout=120.0)
                if em.warmup_status != STATUS_READY:
                    LOGGER.warning(
                        "startup_strict_mode_embedding_not_ready",
                        extra={"status": em.warmup_status, "error": em.warmup_error},
                    )
            except Exception:
                LOGGER.exception("startup_strict_mode_embedding_failed")
        else:
            # Normal mode: embedding loads in background; startup is instant.
            # Fire a quick embed so the model is warm for the first real call;
            # failures are non-fatal and captured in warmup_status.
            try:
                em.embed("startup validation", wait_timeout=0.5)
            except Exception:
                LOGGER.debug("startup_embedding_probe_skipped")
        self.metrics.observe(
            "waggle_startup_validation_seconds",
            time.perf_counter() - started,
            backend=self.config.backend,
        )

    def build_resources(self) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri="graph://stats",
                    name="Graph Stats",
                    description="Current graph statistics.",
                    mimeType="text/plain",
                ),
                types.Resource(
                    uri="graph://recent",
                    name="Recent Graph Nodes",
                    description="The 10 most recently updated nodes.",
                    mimeType="text/plain",
                ),
                types.Resource(
                    uri="graph://windows",
                    name="Context Windows",
                    description="Recent context windows grouped by project/session.",
                    mimeType="text/plain",
                ),
                types.Resource(
                    uri="graph://memory-policy",
                    name="Automatic Memory Policy",
                    description="Policy for when assistants should retrieve and write Waggle memory automatically.",
                    mimeType="text/plain",
                ),
            ]
        )

    def read_resource_text(self, uri: str) -> str:
        graph = self.current_graph()
        if uri == "graph://stats":
            return serialize_stats(graph.get_stats())
        if uri == "graph://recent":
            return serialize_recent_nodes(graph.list_recent_nodes(limit=10))
        if uri == "graph://windows":
            windows = graph.list_context_windows(limit=50)
            if not windows:
                return "=== Context Windows: No context windows stored ==="
            lines = ["=== Context Windows ==="]
            for window in windows:
                lines.append(
                    f"• {window.id} [{window.status}] session={window.session_id} "
                    f"nodes={window.node_count} updated={window.updated_at.isoformat()}"
                )
            lines.append("=== End Context Windows ===")
            return "\n".join(lines)
        if uri == "graph://memory-policy":
            return MEMORY_AUTOMATION_POLICY
        raise ValidationFailure(f"Unknown resource: {uri}")

    def initialization_options(self) -> InitializationOptions:
        return InitializationOptions(
            server_name=SERVER_NAME,
            server_version=__version__,
            capabilities=self.server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={"waggle_server_info": WAGGLE_SERVER_INFO},
            ),
        )

    def _check_embedding_available(
        self, name: str, graph: Any, arguments: dict[str, Any]
    ) -> types.CallToolResult | None:
        """Return a degraded response if fast mode blocks semantic tools."""
        if not self.config.is_fast_mode:
            return None
        if name in EMBEDDING_FREE_TOOLS:
            return None
        em = graph.embedding_model
        if em.warmup_status in (STATUS_READY,):  # shouldn't happen in fast mode, but guard it
            return None
        # Best-effort: if the tool supports retrieval_mode, try replay fallback.
        retrieval_mode = (
            arguments.get("retrieval_mode", "") if name in ("query_graph", "export_context_bundle", "commit") else ""
        )
        if retrieval_mode in ("verbatim", "lexical"):
            return None  # let it through — no embeddings needed
        return self._tool_result(
            f"Tool '{name}' requires semantic embeddings which are unavailable in fast/inspection mode "
            f"(WAGGLE_STARTUP_MODE={self.config.startup_mode}). "
            "Use retrieval_mode='verbatim' for transcript-only retrieval, or restart with "
            "WAGGLE_STARTUP_MODE=normal.",
            {
                "status": "unavailable",
                "reason": "fast_mode",
                "startup_mode": self.config.startup_mode,
                "embedding_status": STATUS_DISABLED,
                "tool": name,
            },
        )

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        graph = self.current_graph()
        request = self._get_request()
        request_id = ""
        if request is not None:
            request_id = getattr(request.state, "request_id", "")
        else:
            try:
                request_id = str(request_ctx.get().request_id)
            except LookupError:
                request_id = ""

        # Fast-mode guard: return structured degraded response for semantic tools.
        fast_mode_result = self._check_embedding_available(name, graph, arguments)
        if fast_mode_result is not None:
            return fast_mode_result

        started = time.perf_counter()
        with runtime_context(
            request_id=request_id,
            tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
            transport="http" if request is not None else "stdio",
            backend=self.config.backend,
            api_key_id=getattr(getattr(request, "state", object()), "api_key_id", "") if request is not None else "",
            tool_name=name,
        ):
            try:
                self._validate_tool_payload(name, arguments)
                # Normalise legacy tool names to their canonical git-vocabulary equivalents.
                # _TOOL_ALIASES maps old name → (canonical name, default args).
                # Default args are merged first; caller-provided args win on collision.
                if name in _TOOL_ALIASES:
                    canonical_name, default_args = _TOOL_ALIASES[name]
                    arguments = {**default_args, **arguments}
                    name = canonical_name
                if name == "build_context" and not RECURSIVE_CONTEXT_ENABLED:
                    return self._error_result(
                        ValueError("build_context is disabled by WAGGLE_RECURSIVE_CONTEXT_ENABLED=false.")
                    )
                LOGGER.info("tool_call_started")
                if name == "store_node":
                    store_result = graph.add_node(
                        label=arguments["label"],
                        content=arguments["content"],
                        node_type=NodeType(arguments["node_type"]),
                        tags=arguments.get("tags", []),
                        source_prompt=arguments.get("source_prompt", ""),
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                    )
                    node = store_result.node
                    text = (
                        f"Stored node '{node.label}' with id {node.id}."
                        if store_result.created
                        else f"Reused existing node '{node.label}' with id {node.id}."
                    )
                    if store_result.conflicts:
                        text += f" Detected {len(store_result.conflicts)} potential conflict(s)."
                    if not store_result.created:
                        self.metrics.increment(
                            "waggle_dedup_hits_total",
                            tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
                            dedup_reason=store_result.dedup_reason or "unknown",
                        )
                    if store_result.conflicts:
                        self.metrics.increment(
                            "waggle_conflicts_total",
                            value=len(store_result.conflicts),
                            tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
                        )
                    result = self._tool_result(
                        text,
                        {
                            **self._node_payload(node),
                            "created": store_result.created,
                            "dedup_reason": store_result.dedup_reason,
                            "similarity": store_result.similarity,
                            "conflicts": [
                                {
                                    "other_node_id": conflict.other_node_id,
                                    "other_node_label": conflict.other_node_label,
                                    "relationship": conflict.relationship,
                                    "reason": conflict.reason,
                                }
                                for conflict in store_result.conflicts
                            ],
                        },
                    )
                elif name == "store_edge":
                    edge = graph.add_edge(
                        source_id=arguments["source_id"],
                        target_id=arguments["target_id"],
                        relationship=arguments["relationship"],
                        weight=float(arguments.get("weight", 1.0)),
                    )
                    result = self._tool_result(
                        f"Created edge {edge.id} linking {edge.source_id} to {edge.target_id} as {edge.relationship}.",
                        self._edge_payload(edge),
                    )
                elif name == "canonicalize_node":
                    _scope = {
                        "project": arguments.get("project", ""),
                        "agent_id": arguments.get("agent_id", ""),
                        "session_id": arguments.get("session_id", ""),
                    }
                    result_obj = graph.canonicalize_node(
                        node_ids=arguments["node_ids"],
                        canonical_id=arguments["canonical_id"],
                    )
                    result = self._tool_result(
                        f"Merged {len(result_obj.merged_node_ids)} node(s) into canonical node '{result_obj.canonical_node.label}' ({result_obj.canonical_node.id}). "
                        f"Repointed {result_obj.edges_repointed} edge(s). Added {len(result_obj.aliases_added)} new alias(es).",
                        {
                            "canonical_node": self._node_payload(result_obj.canonical_node),
                            "merged_node_ids": result_obj.merged_node_ids,
                            "edges_repointed": result_obj.edges_repointed,
                            "aliases_added": result_obj.aliases_added,
                        },
                    )
                elif name == "dedup_candidates":
                    _scope = {
                        "project": arguments.get("project", ""),
                        "agent_id": arguments.get("agent_id", ""),
                        "session_id": arguments.get("session_id", ""),
                    }
                    threshold = float(arguments.get("threshold", 0.85))
                    result_obj = graph.dedup_candidates(
                        scope=_scope,
                        threshold=threshold,
                    )
                    lines = [
                        f"Found {len(result_obj.pairs)} candidate pair(s) above threshold {threshold} (auto-merge threshold is higher).",
                        "Top candidates (sorted by similarity):",
                    ]
                    for pair in result_obj.pairs[:10]:
                        lines.append(
                            f"  {pair.similarity:.4f}: {pair.node_id_a} ({pair.label_a}) ↔ {pair.node_id_b} ({pair.label_b})"
                        )
                    if len(result_obj.pairs) > 10:
                        lines.append(f"  ... and {len(result_obj.pairs) - 10} more")
                    result = self._tool_result(
                        "\n".join(lines),
                        {
                            "pairs": [
                                {
                                    "node_id_a": p.node_id_a,
                                    "node_id_b": p.node_id_b,
                                    "label_a": p.label_a,
                                    "label_b": p.label_b,
                                    "similarity": p.similarity,
                                }
                                for p in result_obj.pairs
                            ],
                            "threshold": result_obj.threshold,
                            "total_nodes_scanned": result_obj.total_nodes_scanned,
                        },
                    )
                elif name == "aggregate_graph":
                    _as_of_raw = arguments.get("as_of")
                    _as_of = datetime.fromisoformat(_as_of_raw).astimezone(UTC) if _as_of_raw else None
                    subgraph = graph.aggregate(
                        query=arguments.get("query", ""),
                        node_types=arguments.get("node_types"),
                        tags=arguments.get("tags"),
                        max_nodes=int(arguments.get("max_nodes", 100)),
                        max_depth=int(arguments.get("max_depth", 1)),
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                        include_invalidated=bool(arguments.get("include_invalidated", False)),
                        as_of=_as_of,
                    )
                    result = self._tool_result(
                        serialize_subgraph(subgraph),
                        self._subgraph_payload(subgraph),
                    )
                elif name == "query_graph":
                    _as_of_raw = arguments.get("as_of")
                    _as_of = datetime.fromisoformat(_as_of_raw).astimezone(UTC) if _as_of_raw else None
                    subgraph = graph.query(
                        query=arguments["query"],
                        max_nodes=int(arguments.get("max_nodes", 20)),
                        max_depth=int(arguments.get("max_depth", 2)),
                        expand_depth=int(arguments.get("expand_depth", 0)),
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                        retrieval_mode=arguments.get("retrieval_mode", "hybrid"),
                        include_invalidated=bool(arguments.get("include_invalidated", False)),
                        as_of=_as_of,
                    )
                    result = self._tool_result(
                        serialize_subgraph(subgraph),
                        self._subgraph_payload(subgraph),
                    )
                elif name == "debug_retrieval":
                    debug = graph.debug_retrieval(
                        query=arguments["query"],
                        max_nodes=int(arguments.get("max_nodes", 10)),
                        max_depth=int(arguments.get("max_depth", 2)),
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                        retrieval_mode=arguments.get("retrieval_mode", "hybrid"),
                    )
                    result = self._tool_result(
                        json.dumps(debug, indent=2),
                        debug,
                    )
                elif name == "list_context_scopes":
                    scopes = graph.list_context_scopes()
                    result = self._tool_result(
                        f"Known scopes: {len(scopes.agent_ids)} agents, {len(scopes.projects)} projects, {len(scopes.session_ids)} sessions.",
                        self._context_scope_payload(scopes),
                    )
                elif name == "list_context_windows":
                    windows = graph.list_context_windows(
                        project=arguments.get("project", ""),
                        status=arguments.get("status", ""),
                        limit=int(arguments.get("limit", 20)),
                    )
                    result = self._tool_result(
                        f"Context windows: {len(windows)}",
                        {"windows": [self._context_window_payload(window) for window in windows]},
                    )
                elif name == "get_context_window":
                    window = graph.get_context_window(arguments["window_id"])
                    edges = graph.get_context_window_edges(window.id)
                    nodes = graph.get_window_nodes(window.id) if bool(arguments.get("include_nodes", True)) else []
                    result = self._tool_result(
                        f"Context window {window.id}: {window.node_count} nodes, {len(edges)} connected window edge(s).",
                        {
                            "window": self._context_window_payload(window),
                            "nodes": [self._node_payload(node) for node in nodes],
                            "window_edges": [self._context_window_edge_payload(edge) for edge in edges],
                        },
                    )
                elif name == "close_context_window":
                    window = graph.close_context_window(arguments["window_id"])
                    edges = graph.get_context_window_edges(window.id)
                    result = self._tool_result(
                        f"Closed context window {window.id} with {window.node_count} nodes and {len(edges)} connected window edge(s).",
                        {
                            "window": self._context_window_payload(window),
                            "window_edges": [self._context_window_edge_payload(edge) for edge in edges],
                        },
                    )
                elif name == "get_related":
                    subgraph = graph.get_related(
                        node_id=arguments["node_id"], max_depth=int(arguments.get("max_depth", 2))
                    )
                    result = self._tool_result(serialize_subgraph(subgraph), self._subgraph_payload(subgraph))
                elif name == "get_node_history":
                    history = graph.get_node_history(
                        node_id=arguments["node_id"], max_depth=int(arguments.get("max_depth", 2))
                    )
                    result = self._tool_result(serialize_node_history(history), self._node_history_payload(history))
                elif name == "timeline":
                    timeline = graph.timeline(
                        node_id=arguments.get("node_id", ""),
                        query=arguments.get("query", ""),
                        limit=int(arguments.get("limit", 25)),
                        max_depth=int(arguments.get("max_depth", 2)),
                        include_evidence=bool(arguments.get("include_evidence", True)),
                    )
                    result = self._tool_result(serialize_timeline(timeline), self._timeline_payload(timeline))
                elif name == "list_conflicts":
                    conflicts = graph.list_conflicts(
                        include_resolved=bool(arguments.get("include_resolved", False)),
                        limit=int(arguments.get("limit", 25)),
                    )
                    result = self._tool_result(serialize_conflicts(conflicts), self._conflict_list_payload(conflicts))
                elif name == "resolve_conflict":
                    resolved = graph.resolve_conflict(
                        edge_id=arguments["edge_id"],
                        resolution_note=arguments.get("resolution_note", ""),
                        winner=arguments.get("winner"),
                    )
                    result = self._tool_result(
                        serialize_conflict_entry(resolved), self._conflict_entry_payload(resolved)
                    )
                elif name == "update_node":
                    node = graph.update_node(
                        node_id=arguments["node_id"],
                        content=arguments.get("content"),
                        label=arguments.get("label"),
                        tags=arguments.get("tags"),
                    )
                    result = self._tool_result(f"Updated node '{node.label}' ({node.id}).", self._node_payload(node))
                elif name == "delete_node":
                    node = graph.delete_node(node_id=arguments["node_id"])
                    result = self._tool_result(
                        f"Deleted node '{node.label}' ({node.id}) and its connected edges.",
                        {"id": node.id, "label": node.label, "tenant_id": node.tenant_id},
                    )
                elif name == "clear_session":
                    dry_run = bool(arguments.get("dry_run", False))
                    if not dry_run:
                        self._require_clear_confirmation(arguments, "clear_session")
                    cleared = graph.clear_session(session_id=arguments["session_id"], dry_run=dry_run)
                    prefix = "[Preview] Would clear" if dry_run else "Cleared"
                    verb = "Would delete" if dry_run else "Deleted"
                    result = self._tool_result(
                        f"{prefix} session '{cleared.session_id}'. {verb} {cleared.deleted_nodes} node(s), "
                        f"{cleared.deleted_edges} edge(s), and {cleared.deleted_transcripts} transcript record(s).",
                        self._clear_scope_payload(cleared),
                    )
                elif name == "clear_project":
                    dry_run = bool(arguments.get("dry_run", False))
                    if not dry_run:
                        self._require_clear_confirmation(arguments, "clear_project")
                    cleared = graph.clear_project(project=arguments["project"], dry_run=dry_run)
                    prefix = "[Preview] Would clear" if dry_run else "Cleared"
                    verb = "Would delete" if dry_run else "Deleted"
                    result = self._tool_result(
                        f"{prefix} project '{cleared.project}'. {verb} {cleared.deleted_nodes} node(s), "
                        f"{cleared.deleted_edges} edge(s), and {cleared.deleted_transcripts} transcript record(s).",
                        self._clear_scope_payload(cleared),
                    )
                elif name == "clear_all":
                    dry_run = bool(arguments.get("dry_run", False))
                    if not dry_run:
                        self._require_clear_confirmation(arguments, "clear_all")
                    cleared = graph.clear_all(dry_run=dry_run)
                    prefix = "[Preview] Would clear" if dry_run else "Cleared"
                    verb = "Would delete" if dry_run else "Deleted"
                    result = self._tool_result(
                        f"{prefix} all graph memory data for tenant '{graph.tenant_id}'. {verb} {cleared.deleted_nodes} node(s), "
                        f"{cleared.deleted_edges} edge(s), and {cleared.deleted_transcripts} transcript record(s).",
                        self._clear_scope_payload(cleared),
                    )
                elif name == "decompose_and_store":
                    subgraph = graph.decompose_and_store(
                        content=arguments["content"], context=arguments.get("context", "")
                    )
                    result = self._tool_result(serialize_subgraph(subgraph), self._subgraph_payload(subgraph))
                elif name == "observe_conversation":
                    observation = graph.observe_conversation(
                        user_message=arguments["user_message"],
                        assistant_response=arguments["assistant_response"],
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                    )
                    result = self._tool_result(
                        serialize_observation_result(observation),
                        self._observation_payload(observation),
                    )
                elif name == "graph_diff":
                    diff = graph.graph_diff(since=arguments.get("since", "24h"))
                    result = self._tool_result(serialize_graph_diff(diff), self._graph_diff_payload(diff))
                elif name == "prime_context":
                    context_result = graph.prime_context(
                        project=arguments.get("project", ""),
                        agent_id=arguments.get("agent_id", ""),
                        session_id=arguments.get("session_id", ""),
                    )
                    result = self._tool_result(
                        serialize_prime_context(context_result), self._prime_context_payload(context_result)
                    )
                elif name == "get_topics":
                    # Scope parameters are accepted by the schema but ignored —
                    # topic detection runs across the full tenant graph.
                    topics = graph.get_topics()
                    result = self._tool_result(serialize_topics(topics), self._topic_payload(topics))
                elif name == "get_stats":
                    stats = graph.get_stats()
                    em = graph.embedding_model
                    stats_payload = self._stats_payload(stats)
                    # Augment with live embedding status — never triggers ML load.
                    embedding_status = getattr(em, "warmup_status", "unknown")
                    embedding_error = getattr(em, "warmup_error", "")
                    stats_payload["embedding_status"] = embedding_status
                    if embedding_error:
                        stats_payload["embedding_error"] = embedding_error
                    stats_payload["startup_mode"] = self.config.startup_mode
                    result = self._tool_result(
                        serialize_stats(stats)
                        + f"\nEmbedding status: {embedding_status}"
                        + (f" (error: {embedding_error}" + ")" if embedding_error else ""),
                        stats_payload,
                    )
                elif name == "export_graph_html":
                    output_path = graph.export_graph_html(
                        output_path=arguments.get("output_path"),
                        include_physics=bool(arguments.get("include_physics", True)),
                    )
                    stats = graph.get_stats()
                    result = self._tool_result(
                        f"Exported graph visualization to {output_path}.",
                        {
                            "output_path": str(output_path),
                            "tenant_id": graph.tenant_id,
                            "total_nodes": stats.total_nodes,
                            "total_edges": stats.total_edges,
                        },
                    )
                elif name == "window_graph_viz":
                    output_path = graph.export_window_graph_html(
                        project=arguments.get("project", ""),
                        output_path=arguments.get("output_path"),
                        include_physics=bool(arguments.get("include_physics", True)),
                    )
                    windows = graph.list_context_windows(project=arguments.get("project", ""), limit=10_000)
                    edge_count = sum(len(graph.get_context_window_edges(window.id)) for window in windows)
                    result = self._tool_result(
                        f"Exported context-window graph visualization to {output_path}.",
                        {
                            "output_path": str(output_path),
                            "tenant_id": graph.tenant_id,
                            "project": arguments.get("project", ""),
                            "total_context_windows": len(windows),
                            "total_context_window_edges": edge_count,
                        },
                    )
                elif name == "commit":
                    # waggle commit — unified export: abhi (default), backup, or bundle
                    commit_format = arguments.get("commit_format", "abhi")
                    if commit_format == "backup":
                        backup = graph.export_graph_backup(output_path=arguments.get("output_path"))
                        result = self._tool_result(
                            f"Committed graph backup to {backup.output_path}.",
                            {
                                "output_path": backup.output_path,
                                "tenant_id": backup.tenant_id,
                                "schema_version": backup.schema_version,
                                "node_count": backup.node_count,
                                "edge_count": backup.edge_count,
                                "commit_format": "backup",
                            },
                        )
                    elif commit_format == "bundle":
                        exported = graph.export_context_bundle(
                            mode=arguments.get("mode", "prime"),
                            query=arguments.get("query", ""),
                            project=arguments.get("project", ""),
                            agent_id=arguments.get("agent_id", ""),
                            session_id=arguments.get("session_id", ""),
                            max_nodes=int(arguments.get("max_nodes", 25)),
                            max_depth=int(arguments.get("max_depth", 2)),
                            retrieval_mode=arguments.get("retrieval_mode", "hybrid"),
                            format=arguments.get("format", "both"),
                            output_path=arguments.get("output_path"),
                            include_edges=bool(arguments.get("include_edges", True)),
                            include_timestamps=bool(arguments.get("include_timestamps", True)),
                            include_source_prompt=bool(arguments.get("include_source_prompt", False)),
                            audience=arguments.get("audience", "llm"),
                        )
                        result = self._tool_result(
                            serialize_context_bundle_export(exported),
                            self._context_bundle_payload(exported),
                        )
                    else:
                        # default: abhi
                        _assert_export_safe(
                            graph,
                            force=bool(arguments.get("force", False)),
                            project=arguments.get("project", ""),
                            agent_id=arguments.get("agent_id", ""),
                            session_id=arguments.get("session_id", ""),
                        )
                        exported = graph.export_abhi(
                            output_path=arguments.get("output_path"),
                            project=arguments.get("project", ""),
                            agent_id=arguments.get("agent_id", ""),
                            session_id=arguments.get("session_id", ""),
                            include_low_confidence_edges=bool(arguments.get("include_low_confidence_edges", False)),
                        )
                        edge_filter = exported.export_context.get("edge_filter", {})
                        filter_summary = ""
                        if edge_filter:
                            filtered_count = edge_filter.get("edges_filtered", 0)
                            total_count = edge_filter.get("edges_total", 0)
                            if filtered_count:
                                filter_summary = f" ({filtered_count} low-confidence RELATES_TO edges filtered from {total_count} total)"
                        result = self._tool_result(
                            f"Committed memory to {exported.output_path}.{filter_summary}",
                            {
                                "output_path": exported.output_path,
                                "tenant_id": exported.tenant_id,
                                "schema_version": exported.schema_version,
                                "abhi_spec_version": exported.abhi_spec_version,
                                "node_count": exported.node_count,
                                "edge_count": exported.edge_count,
                                "content_hash": exported.content_hash,
                                "edge_filter_summary": edge_filter,
                                "commit_format": "abhi",
                            },
                        )
                elif name == "pull":
                    # waggle pull — unified import: abhi (default) or backup
                    pull_format = arguments.get("pull_format", "abhi")
                    if pull_format == "backup":
                        imported = graph.import_graph_backup(input_path=arguments["input_path"])
                        result = self._tool_result(
                            f"Pulled graph backup from {imported.input_path}.",
                            {
                                "input_path": imported.input_path,
                                "tenant_id": imported.tenant_id,
                                "schema_version": imported.schema_version,
                                "nodes_created": imported.nodes_created,
                                "nodes_updated": imported.nodes_updated,
                                "edges_created": imported.edges_created,
                                "edges_updated": imported.edges_updated,
                                "pull_format": "backup",
                            },
                        )
                    else:
                        # default: abhi
                        imported = graph.import_abhi(input_path=arguments["input_path"])
                        result = self._tool_result(
                            f"Pulled memory from {imported.input_path}.",
                            {
                                "input_path": imported.input_path,
                                "tenant_id": imported.tenant_id,
                                "schema_version": imported.schema_version,
                                "abhi_spec_version": imported.abhi_spec_version,
                                "nodes_created": imported.nodes_created,
                                "nodes_updated": imported.nodes_updated,
                                "edges_created": imported.edges_created,
                                "edges_updated": imported.edges_updated,
                                "hash_verified": imported.hash_verified,
                                "pull_format": "abhi",
                            },
                        )
                elif name == "diff":
                    # waggle diff
                    diff = graph.diff_abhi(
                        input_path_a=arguments["input_path_a"],
                        input_path_b=arguments["input_path_b"],
                    )
                    result = self._tool_result(
                        serialize_abhi_diff(diff),
                        diff.model_dump(mode="json"),
                    )
                elif name == "merge":
                    # waggle merge
                    merged = graph.merge_abhi(
                        base_input_path=arguments["base_input_path"],
                        left_input_path=arguments["left_input_path"],
                        right_input_path=arguments["right_input_path"],
                        output_path=arguments["output_path"],
                        merge_strategy=arguments.get("merge_strategy", "prefer_right"),
                    )
                    result = self._tool_result(
                        serialize_abhi_merge(merged),
                        merged.model_dump(mode="json"),
                    )
                elif name == "grep":
                    # waggle grep
                    queried = graph.query_abhi(
                        input_path=arguments["input_path"],
                        query_id=arguments.get("query_id", ""),
                        query_text=arguments.get("query_text", ""),
                    )
                    result = self._tool_result(
                        serialize_abhi_query(queried),
                        queried.model_dump(mode="json"),
                    )
                elif name == "load_abhi_chunks":
                    loaded = graph.load_abhi_chunks(
                        input_path=arguments["input_path"],
                        chunk_ids=list(arguments.get("chunk_ids", [])),
                        query_id=arguments.get("query_id", ""),
                        query_text=arguments.get("query_text", ""),
                    )
                    result = self._tool_result(
                        serialize_abhi_chunk_load(loaded),
                        loaded.model_dump(mode="json"),
                    )
                elif name == "fsck":
                    # waggle fsck
                    validation = graph.validate_abhi(input_path=arguments["input_path"])
                    result = self._tool_result(
                        serialize_abhi_validation(validation),
                        validation.model_dump(mode="json"),
                    )
                elif name == "show":
                    # waggle show
                    inspection = graph.inspect_abhi(input_path=arguments["input_path"])
                    result = self._tool_result(
                        serialize_abhi_inspect(inspection),
                        inspection.model_dump(mode="json"),
                    )
                elif name == "export_markdown_vault":
                    exported = graph.export_markdown_vault(
                        root_path=arguments["root_path"],
                        project=arguments.get("project", ""),
                        agent_id=arguments.get("agent_id", ""),
                        session_id=arguments.get("session_id", ""),
                    )
                    result = self._tool_result(
                        f"Exported Markdown vault to {exported.root_path}.",
                        self._markdown_vault_export_payload(exported),
                    )
                elif name == "import_markdown_vault":
                    imported = graph.import_markdown_vault(root_path=arguments["root_path"])
                    result = self._tool_result(
                        f"Imported Markdown vault from {imported.root_path}.",
                        self._markdown_vault_import_payload(imported),
                    )
                elif name == "edge_quality_report":
                    report = graph.edge_quality_report(
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                    )
                    lines = [
                        f"Edge quality report: {report['total_edges']} edges across {report['total_edge_types']} type(s)."
                    ]
                    for rel, stats in sorted(report.get("by_type", {}).items()):
                        lines.append(f"  {rel}: count={stats['count']} avg_confidence={stats['avg_confidence']:.3f}")
                    result = self._tool_result("\n".join(lines), report)
                elif name == "build_context":
                    controller = RecursiveContextController(graph=graph)
                    ctx_result = controller.build_context(
                        query=arguments["query"],
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                        context_window_id=arguments.get("context_window_id"),
                        token_budget=int(arguments.get("token_budget", 1200)),
                        depth=int(arguments.get("depth", 2)),
                        max_subqueries=int(arguments.get("max_subqueries", 6)),
                        include_evidence=bool(arguments.get("include_evidence", True)),
                        mode=arguments.get("mode", "balanced"),
                    )
                    payload = {
                        "context_pack": ctx_result.context_pack,
                        "subqueries": [sq.model_dump() for sq in ctx_result.subqueries],
                        "nodes_used": [self._node_payload(n) for n in ctx_result.nodes_used],
                        "edges_used": [self._edge_payload(e) for e in ctx_result.edges_used],
                        "transcript_evidence": [
                            (t.model_dump() if hasattr(t, "model_dump") else str(t))
                            for t in ctx_result.transcript_evidence
                        ],
                        "conflicts": ctx_result.conflicts,
                        "token_estimate": ctx_result.token_estimate,
                        "debug": ctx_result.debug,
                    }
                    result = self._tool_result(ctx_result.context_pack, payload)
                else:
                    raise ValidationFailure(f"Unknown tool: {name}")

                elapsed = time.perf_counter() - started
                self.metrics.increment(
                    "waggle_tool_requests_total",
                    tool=name,
                    status="success",
                    tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
                )
                self.metrics.observe("waggle_tool_latency_seconds", elapsed, tool=name)
                self._record_graph_size(graph)
                LOGGER.info("tool_call_completed")
                return result
            except Exception as exc:
                elapsed = time.perf_counter() - started
                self.metrics.increment(
                    "waggle_tool_requests_total",
                    tool=name,
                    status="error",
                    tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
                )
                self.metrics.observe("waggle_tool_latency_seconds", elapsed, tool=name)
                if isinstance(exc, AuthenticationError):
                    self.metrics.increment(
                        "waggle_auth_failures_total",
                        tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
                    )
                LOGGER.exception("tool_call_failed")
                return self._error_result(exc)

    def _tool_result(self, text: str, structured: dict[str, Any]) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structuredContent=structured)

    def _error_result(self, exc: Exception) -> types.CallToolResult:
        if isinstance(exc, WaggleError):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Error [{exc.code}]: {exc}")],
                structuredContent={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "error_code": exc.code,
                    "status_code": exc.status_code,
                },
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error: {exc}")],
            structuredContent={"error": str(exc), "error_type": type(exc).__name__},
            isError=True,
        )

    @staticmethod
    def _require_clear_confirmation(arguments: dict[str, Any], command_name: str) -> None:
        if bool(arguments.get("confirm", False)):
            return
        raise ValidationFailure(f"{command_name} is destructive and requires confirm=true.")

    def _node_payload(self, node: Node) -> dict[str, Any]:
        return {
            "id": node.id,
            "tenant_id": node.tenant_id,
            "agent_id": node.agent_id,
            "project": node.project,
            "session_id": node.session_id,
            "context_window_id": node.context_window_id,
            "label": node.label,
            "content": node.content,
            "node_type": node.node_type.value,
            "tags": node.tags,
            "source_prompt": node.source_prompt,
            "metadata": node.metadata,
            "evidence_records": [
                {
                    "evidence_id": record.evidence_id,
                    "session_id": record.session_id,
                    "turn_index": record.turn_index,
                    "source_role": record.source_role,
                    "source_text": record.source_text,
                    "source_span_start": record.source_span_start,
                    "source_span_end": record.source_span_end,
                    "observed_at": record.observed_at.isoformat(),
                }
                for record in node.evidence_records
            ],
            "valid_from": node.valid_from.isoformat() if node.valid_from is not None else None,
            "valid_to": node.valid_to.isoformat() if node.valid_to is not None else None,
            "created_at": node.created_at.isoformat(),
            "updated_at": node.updated_at.isoformat(),
            "access_count": node.access_count,
            "similarity_score": node.similarity_score,
            "recency_score": node.recency_score,
            "edge_score": node.edge_score,
            "final_score": node.final_score,
        }

    def _edge_payload(self, edge: Any) -> dict[str, Any]:
        return {
            "id": edge.id,
            "tenant_id": getattr(edge, "tenant_id", ""),
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "relationship": edge.relationship,
            "weight": edge.weight,
            "metadata": edge.metadata,
            "created_at": edge.created_at.isoformat(),
        }

    def _subgraph_payload(self, result: SubgraphResult) -> dict[str, Any]:
        return {
            "query": result.query,
            "retrieval_mode": result.retrieval_mode,
            "total_nodes_in_graph": result.total_nodes_in_graph,
            "nodes": [self._node_payload(node) for node in result.nodes],
            "edges": [self._edge_payload(edge) for edge in result.edges],
            "replay_hits": [
                {
                    "score": hit.score,
                    "session_id": hit.session_id,
                    "turn_index": hit.turn_index,
                    "role": hit.role,
                    "transcript_text": hit.transcript_text,
                    "transcript_snippet": hit.transcript_snippet,
                    "observed_at": hit.observed_at.isoformat(),
                }
                for hit in result.replay_hits
            ],
            "fusion_hits": [
                {
                    "content": hit.content,
                    "score": hit.score,
                    "source_lane": hit.source_lane,
                    "graph_rank": hit.graph_rank,
                    "replay_rank": hit.replay_rank,
                    "fused_rank": hit.fused_rank,
                    "node_id": hit.node_id,
                    "node_type": hit.node_type,
                    "edges": hit.edges,
                    "session_id": hit.session_id,
                    "transcript_snippet": hit.transcript_snippet,
                    "turn_index": hit.turn_index,
                }
                for hit in result.fusion_hits
            ],
            "hybrid_hits": [
                {
                    "content": hit.content,
                    "score": hit.score,
                    "source": hit.source,
                    "turn_pair_id": hit.turn_pair_id,
                    "node_ids": hit.node_ids,
                    "reasoning_from_reranker": hit.reasoning_from_reranker,
                    "observed_at": hit.observed_at.isoformat() if hit.observed_at is not None else None,
                    "layer_scores": hit.layer_scores,
                }
                for hit in result.hybrid_hits
            ],
        }

    def _observation_payload(self, result: ObservationResult) -> dict[str, Any]:
        return {
            "turn_id": result.turn_id,
            "verbatim_stored": result.verbatim_stored,
            "nodes_extracted": result.nodes_extracted,
            "edges_inferred": result.edges_inferred,
            "extraction_errors": result.extraction_errors,
            "stored_nodes": [self._node_payload(node) for node in result.stored_nodes],
            "created_count": result.created_count,
            "reused_count": result.reused_count,
            "conflicts": [
                {
                    "other_node_id": conflict.other_node_id,
                    "other_node_label": conflict.other_node_label,
                    "relationship": conflict.relationship,
                    "reason": conflict.reason,
                }
                for conflict in result.conflicts
            ],
        }

    def _node_history_payload(self, result: NodeHistoryResult) -> dict[str, Any]:
        return {
            "node": self._node_payload(result.node),
            "related_nodes": [self._node_payload(node) for node in result.related_nodes],
            "edges": [self._edge_payload(edge) for edge in result.edges],
        }

    def _timeline_payload(self, result: TimelineResult) -> dict[str, Any]:
        return {
            "scope": result.scope,
            "items": [
                {
                    "kind": item.kind,
                    "timestamp": item.timestamp.isoformat(),
                    "label": item.label,
                    "summary": item.summary,
                    "node_id": item.node_id,
                    "edge_id": item.edge_id,
                    "recency_score": item.recency_score,
                }
                for item in result.items
            ],
        }

    def _conflict_entry_payload(self, entry: ConflictEntry) -> dict[str, Any]:
        return {
            "edge": self._edge_payload(entry.edge),
            "source_node": self._node_payload(entry.source_node),
            "target_node": self._node_payload(entry.target_node),
            "resolved": entry.resolved,
            "resolution_note": entry.resolution_note,
            "resolved_at": entry.resolved_at.isoformat() if entry.resolved_at is not None else None,
        }

    def _conflict_list_payload(self, result: ConflictListResult) -> dict[str, Any]:
        return {
            "include_resolved": result.include_resolved,
            "conflicts": [self._conflict_entry_payload(entry) for entry in result.conflicts],
        }

    def _context_scope_payload(self, result: ContextScopeResult) -> dict[str, Any]:
        return {
            "agent_ids": result.agent_ids,
            "projects": result.projects,
            "session_ids": result.session_ids,
        }

    def _clear_scope_payload(self, result: ClearScopeResult) -> dict[str, Any]:
        return result.model_dump(mode="json")

    def _context_window_payload(self, window: ContextWindow) -> dict[str, Any]:
        return {
            "id": window.id,
            "tenant_id": window.tenant_id,
            "repo_id": window.repo_id,
            "session_id": window.session_id,
            "title": window.title,
            "status": window.status,
            "node_count": window.node_count,
            "embedding_stale": window.embedding_stale,
            "created_at": window.created_at.isoformat(),
            "updated_at": window.updated_at.isoformat(),
            "closed_at": window.closed_at.isoformat() if window.closed_at is not None else None,
        }

    def _context_window_edge_payload(self, edge: ContextWindowEdge) -> dict[str, Any]:
        return {
            "id": edge.id,
            "tenant_id": edge.tenant_id,
            "source_window_id": edge.source_window_id,
            "target_window_id": edge.target_window_id,
            "edge_type": edge.edge_type,
            "shared_entities": edge.shared_entities,
            "weight": edge.weight,
            "metadata": edge.metadata,
            "created_at": edge.created_at.isoformat(),
        }

    def _graph_diff_payload(self, result: GraphDiffResult) -> dict[str, Any]:
        return {
            "since": result.since,
            "generated_at": result.generated_at.isoformat(),
            "added_nodes": [self._node_payload(node) for node in result.added_nodes],
            "updated_nodes": [self._node_payload(node) for node in result.updated_nodes],
            "created_edges": [self._edge_payload(edge) for edge in result.created_edges],
            "contradiction_edges": [self._edge_payload(edge) for edge in result.contradiction_edges],
        }

    def _prime_context_payload(self, result: PrimeContextResult) -> dict[str, Any]:
        return {
            "project": result.project,
            "summary": result.summary,
            "total_nodes_in_graph": result.total_nodes_in_graph,
            "nodes": [self._node_payload(node) for node in result.nodes],
            "edges": [self._edge_payload(edge) for edge in result.edges],
        }

    def _context_bundle_payload(self, result: ContextBundleExportResult) -> dict[str, Any]:
        return {
            "tenant_id": result.tenant_id,
            "project": result.project,
            "mode": result.mode,
            "retrieval_mode": result.retrieval_mode,
            "query": result.query,
            "summary": result.summary,
            "markdown_path": result.markdown_path,
            "json_path": result.json_path,
            "node_count": result.node_count,
            "edge_count": result.edge_count,
            "render_hints": {
                "token_estimate": result.bundle.render_hints.token_estimate,
                "recommended_paste_order": result.bundle.render_hints.recommended_paste_order,
                "truncation_flags": result.bundle.render_hints.truncation_flags,
                "chunk_count": result.bundle.render_hints.chunk_count,
            },
        }

    def _topic_payload(self, result: TopicResult) -> dict[str, Any]:
        return {
            "total_clusters": result.total_clusters,
            "clusters": [
                {
                    "cluster_id": cluster.cluster_id,
                    "label": cluster.label,
                    "node_count": cluster.node_count,
                    "top_tags": cluster.top_tags,
                    "nodes": [self._node_payload(node) for node in cluster.nodes],
                }
                for cluster in result.clusters
            ],
        }

    def _stats_payload(self, stats: GraphStats) -> dict[str, Any]:
        return {
            "total_nodes": stats.total_nodes,
            "total_edges": stats.total_edges,
            "total_repos": stats.total_repos,
            "total_context_windows": stats.total_context_windows,
            "context_window_status_breakdown": stats.context_window_status_breakdown,
            "total_context_window_edges": stats.total_context_window_edges,
            "context_window_edge_type_breakdown": stats.context_window_edge_type_breakdown,
            "windows_with_embeddings": stats.windows_with_embeddings,
            "windows_with_stale_embeddings": stats.windows_with_stale_embeddings,
            "node_type_breakdown": stats.node_type_breakdown,
            "most_connected_nodes": [
                {
                    "id": node.id,
                    "label": node.label,
                    "node_type": node.node_type.value,
                    "connection_count": node.connection_count,
                }
                for node in stats.most_connected_nodes
            ],
            "most_recent_nodes": [
                {
                    "id": node.id,
                    "label": node.label,
                    "node_type": node.node_type.value,
                    "updated_at": node.updated_at.isoformat(),
                }
                for node in stats.most_recent_nodes
            ],
        }

    def _markdown_vault_export_payload(self, result: MarkdownVaultExportResult) -> dict[str, Any]:
        return {
            "root_path": result.root_path,
            "tenant_id": result.tenant_id,
            "project": result.project,
            "node_count": result.node_count,
            "edge_count": result.edge_count,
            "files_written": result.files_written,
        }

    def _markdown_vault_import_payload(self, result: MarkdownVaultImportResult) -> dict[str, Any]:
        return {
            "root_path": result.root_path,
            "tenant_id": result.tenant_id,
            "nodes_created": result.nodes_created,
            "nodes_updated": result.nodes_updated,
            "edges_created": result.edges_created,
            "edges_deleted": result.edges_deleted,
            "stub_nodes_created": result.stub_nodes_created,
            "conflicts": result.conflicts,
        }

    def _validate_tool_payload(self, name: str, arguments: dict[str, Any]) -> None:
        limit = self.config.max_payload_bytes
        if name == "store_node":
            self._assert_payload_size(arguments.get("label", ""), limit, "store_node.label")
            self._assert_payload_size(arguments.get("content", ""), limit, "store_node.content")
            self._assert_payload_size(arguments.get("source_prompt", ""), limit, "store_node.source_prompt")
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "store_node.agent_id")
            self._assert_payload_size(arguments.get("project", ""), limit, "store_node.project")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "store_node.session_id")
            return
        if name == "decompose_and_store":
            self._assert_payload_size(arguments.get("content", ""), limit, "decompose_and_store.content")
            self._assert_payload_size(arguments.get("context", ""), limit, "decompose_and_store.context")
            return
        if name == "observe_conversation":
            self._assert_payload_size(arguments.get("user_message", ""), limit, "observe_conversation.user_message")
            self._assert_payload_size(
                arguments.get("assistant_response", ""), limit, "observe_conversation.assistant_response"
            )
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "observe_conversation.agent_id")
            self._assert_payload_size(arguments.get("project", ""), limit, "observe_conversation.project")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "observe_conversation.session_id")
            return
        if name == "aggregate_graph":
            self._assert_payload_size(arguments.get("query", ""), limit, "aggregate_graph.query")
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "aggregate_graph.agent_id")
            self._assert_payload_size(arguments.get("project", ""), limit, "aggregate_graph.project")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "aggregate_graph.session_id")
            return
        if name == "query_graph":
            self._assert_payload_size(arguments.get("query", ""), limit, "query_graph.query")
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "query_graph.agent_id")
            self._assert_payload_size(arguments.get("project", ""), limit, "query_graph.project")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "query_graph.session_id")
            return
        if name == "debug_retrieval":
            self._assert_payload_size(arguments.get("query", ""), limit, "debug_retrieval.query")
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "debug_retrieval.agent_id")
            self._assert_payload_size(arguments.get("project", ""), limit, "debug_retrieval.project")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "debug_retrieval.session_id")
            return
        if name in ("export_context_bundle", "commit"):
            self._assert_payload_size(arguments.get("query", ""), limit, "commit.query")
            self._assert_payload_size(arguments.get("project", ""), limit, "commit.project")
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "commit.agent_id")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "commit.session_id")
            self._assert_payload_size(arguments.get("output_path", ""), limit, "commit.output_path")
            return
        if name == "window_graph_viz":
            self._assert_payload_size(arguments.get("project", ""), limit, "window_graph_viz.project")
            self._assert_payload_size(arguments.get("output_path", ""), limit, "window_graph_viz.output_path")
            return
        if name == "timeline":
            self._assert_payload_size(arguments.get("query", ""), limit, "timeline.query")
            self._assert_payload_size(arguments.get("node_id", ""), limit, "timeline.node_id")
            return
        if name == "resolve_conflict":
            self._assert_payload_size(arguments.get("edge_id", ""), limit, "resolve_conflict.edge_id")
            self._assert_payload_size(arguments.get("resolution_note", ""), limit, "resolve_conflict.resolution_note")
            self._assert_payload_size(arguments.get("winner", ""), limit, "resolve_conflict.winner")

    @staticmethod
    def _assert_payload_size(value: Any, limit: int, field_name: str) -> None:
        if value is None:
            return
        size = len(str(value).encode("utf-8"))
        if size > limit:
            raise PayloadTooLargeError(f"{field_name} exceeds the configured payload limit.")

    def _record_graph_size(self, graph: Any) -> None:
        stats = graph.get_stats()
        tenant_id = getattr(graph, "tenant_id", self.config.default_tenant_id)
        self.metrics.set_gauge("waggle_graph_nodes", stats.total_nodes, tenant_id=tenant_id)
        self.metrics.set_gauge("waggle_graph_edges", stats.total_edges, tenant_id=tenant_id)


class MCPHttpApp:
    def __init__(self, app_server: WaggleServer, config: AppConfig) -> None:
        self.app_server = app_server
        self.config = config
        self.metrics = app_server.metrics
        self.rate_limiter = RateLimiter(
            requests_per_minute=config.rate_limit_rpm,
            max_concurrent_requests=config.max_concurrent_requests,
            write_requests_per_minute=config.write_rate_limit_rpm,
        )
        self.transport: StreamableHTTPServerTransport | None = None
        self.ready = False
        self.draining = False

    @asynccontextmanager
    async def lifespan(self, app: Starlette):
        self.transport = StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=False)
        # Kick off background embedding warmup for HTTP transport (non-blocking).
        em = self.app_server._root_graph.embedding_model
        if (
            not self.config.is_fast_mode
            and hasattr(em, "start_background_warmup")
            and not getattr(em, "_warmup_started", False)
        ):
            em.start_background_warmup()
        async with self.transport.connect() as (read_stream, write_stream), anyio.create_task_group() as tg:
            tg.start_soon(
                self.app_server.server.run,
                read_stream,
                write_stream,
                self.app_server.initialization_options(),
                False,
                True,
            )
            self.app_server.validate_startup()
            self.ready = True
            self.metrics.set_gauge("waggle_ready", 1)
            app.state.http_service = self
            try:
                yield
            finally:
                self.draining = True
                self.ready = False
                self.metrics.set_gauge("waggle_ready", 0)
                tg.cancel_scope.cancel()

    async def mcp_asgi(self, scope: Any, receive: Any, send: Any) -> None:
        started = time.perf_counter()
        method = scope["method"]
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        request_id = headers.get("x-request-id", str(uuid.uuid4()))
        status_holder = {"status": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            if self.draining:
                raise ServiceUnavailableError("Server is draining.")
            body = b""
            receive_callable = receive
            if method == "POST":
                request = Request(scope, receive)
                body = await request.body()
                if len(body) > self.config.max_payload_bytes:
                    raise PayloadTooLargeError()
                receive_callable = self._replay_receive(body)
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])
                }

            raw_api_key = headers.get("x-api-key", "")
            if not raw_api_key:
                raise AuthenticationError("Missing X-API-Key header.")
            principal = self.app_server._root_graph.authenticate_api_key(raw_api_key)
            scope.setdefault("state", {})
            scope["state"]["tenant_id"] = principal.tenant_id
            scope["state"]["api_key_id"] = principal.api_key_id
            scope["state"]["request_id"] = request_id
            tenant_graph = self.app_server._root_graph.for_tenant(principal.tenant_id)
            tenant_graph.emit_audit_event(
                event_type="api_key.used",
                actor_type="api_key",
                actor_id=principal.name or principal.api_key_id,
                api_key_id=principal.api_key_id,
                resource_type="mcp_request",
                resource_id=request_id,
                action="use",
                ip_address=scope.get("client", ("", 0))[0] or "",
                user_agent=headers.get("user-agent", ""),
                metadata={"method": method, "tool_name": self._extract_tool_name(body)},
            )

            tool_name = self._extract_tool_name(body)
            principal.require_scope("graph:write" if tool_name in WRITE_HEAVY_TOOLS else "graph:read")
            await self.rate_limiter.check_rate(principal.api_key_id, is_write=tool_name in WRITE_HEAVY_TOOLS)
            async with self.rate_limiter.concurrency_slot(principal.api_key_id):
                with runtime_context(
                    request_id=request_id,
                    tenant_id=principal.tenant_id,
                    transport="http",
                    backend=self.config.backend,
                    api_key_id=principal.api_key_id,
                    tool_name=tool_name,
                ):
                    with anyio.fail_after(self.config.request_timeout_seconds):
                        assert self.transport is not None
                        await self.transport.handle_request(scope, receive_callable, send_wrapper)
        except WaggleError as exc:
            LOGGER.warning("http_request_failed", extra={"error_code": exc.code, "status_code": exc.status_code})
            if isinstance(exc, AuthenticationError):
                self.metrics.increment("waggle_auth_failures_total")
            if exc.code == "rate_limited":
                self.metrics.increment("waggle_rate_limit_rejections_total")
            await JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status_code)(
                scope, receive, send
            )
            status_holder["status"] = exc.status_code
        finally:
            elapsed = time.perf_counter() - started
            self.metrics.increment(
                "waggle_http_requests_total",
                path="/mcp",
                method=method,
                status=str(status_holder["status"]),
            )
            self.metrics.observe("waggle_http_request_latency_seconds", elapsed, path="/mcp", method=method)

    @staticmethod
    def _extract_tool_name(body: bytes) -> str:
        if not body:
            return ""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return ""
        params = payload.get("params", {})
        if isinstance(params, dict):
            return str(params.get("name", ""))
        return ""

    @staticmethod
    def _replay_receive(body: bytes):
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive


class _RequestBodySizeMiddleware:
    """Reject requests whose Content-Length exceeds max_payload_bytes."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except (ValueError, TypeError):
                length = 0
            if length > self.max_bytes:
                response = Response(
                    f"Request body exceeds maximum allowed size of {self.max_bytes} bytes.",
                    status_code=413,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_application(app_server: WaggleServer, config: AppConfig) -> Starlette:
    service = MCPHttpApp(app_server, config)

    async def waggle_error_handler(request: Request, exc: WaggleError) -> Response:
        if isinstance(exc, AuthenticationError):
            service.metrics.increment("waggle_auth_failures_total")
        if exc.code == "rate_limited":
            service.metrics.increment("waggle_rate_limit_rejections_total")
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status_code)

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "live"})

    async def ready(request: Request) -> Response:
        http_service: MCPHttpApp = request.app.state.http_service
        if not http_service.ready or http_service.draining:
            return JSONResponse({"status": "not-ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    async def metrics_endpoint(request: Request) -> Response:
        return PlainTextResponse(request.app.state.http_service.metrics.render_prometheus())

    def _scope_from_request(request: Request) -> dict[str, str]:
        return {
            "project": request.query_params.get("project", "").strip(),
            "agent_id": request.query_params.get("agent_id", "").strip(),
            "session_id": request.query_params.get("session_id", "").strip(),
        }

    def _graph_from_request(request: Request, *, tenant_override: str = "") -> tuple[Any, Any | None]:
        raw_api_key = request.headers.get("x-api-key", "").strip()
        if raw_api_key:
            principal = app_server._root_graph.authenticate_api_key(raw_api_key)
            return app_server._root_graph.for_tenant(principal.tenant_id), principal
        tenant_id = (
            tenant_override.strip() or request.query_params.get("tenant_id", "").strip() or config.default_tenant_id
        )
        return app_server.graph.for_tenant(tenant_id), None

    def _emit_http_audit(
        request: Request,
        *,
        event_type: str,
        resource_type: str,
        resource_id: str = "",
        action: str = "",
        metadata: dict[str, Any] | None = None,
        tenant_override: str = "",
    ) -> None:
        try:
            graph, principal = _graph_from_request(request, tenant_override=tenant_override)
        except AuthenticationError:
            graph = app_server.graph.for_tenant(tenant_override.strip() or config.default_tenant_id)
            principal = None
        graph.emit_audit_event(
            event_type=event_type,
            actor_type="api_key" if principal is not None else "admin",
            actor_id=(principal.name or principal.api_key_id) if principal is not None else "local-http",
            api_key_id=principal.api_key_id if principal is not None else "",
            resource_type=resource_type,
            resource_id=resource_id,
            action=action or event_type,
            ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
            metadata=metadata or {},
        )

    def _require_http_scope(
        request: Request, required_scope: str, *, tenant_override: str = ""
    ) -> tuple[Any, Any | None]:
        graph, principal = _graph_from_request(request, tenant_override=tenant_override)
        if principal is not None:
            principal.require_scope(required_scope)
        return graph, principal

    def _build_scoped_abhi(graph: Any, scope: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = graph.get_graph_snapshot(**scope)
        return snapshot, build_abhi_document(snapshot)

    def _validate_live_snapshot(snapshot: dict[str, Any]) -> None:
        validation = validate_abhi_document(build_abhi_document(snapshot), input_path="live://graph")
        blocking_errors = [
            error
            for error in validation.errors
            if "cannot originate from node type" not in error and "cannot target node type" not in error
        ]
        if blocking_errors:
            raise ValidationFailure("; ".join(blocking_errors))

    def _node_snapshot_payload(
        *,
        snapshot: dict[str, Any],
        payload: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = existing or {}
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {
            "id": str(current.get("id") or payload.get("id") or f"preview-{uuid.uuid4()}").strip(),
            "tenant_id": str(snapshot.get("tenant_id", "")),
            "agent_id": str(payload.get("agent_id", current.get("agent_id", "")) or current.get("agent_id", "")),
            "project": str(payload.get("project", current.get("project", "")) or current.get("project", "")),
            "session_id": str(
                payload.get("session_id", current.get("session_id", "")) or current.get("session_id", "")
            ),
            "context_window_id": current.get("context_window_id"),
            "label": str(payload.get("label", current.get("label", "")) or current.get("label", "")).strip(),
            "content": str(payload.get("content", current.get("content", "")) or current.get("content", "")).strip(),
            "node_type": str(
                payload.get("node_type", current.get("node_type", "note")) or current.get("node_type", "note")
            ).strip(),
            "tags": payload.get("tags", current.get("tags", [])) or [],
            "source_prompt": current.get("source_prompt", ""),
            "metadata": current.get("metadata", {}),
            "evidence_records": current.get("evidence_records", []),
            "valid_from": current.get("valid_from"),
            "valid_to": current.get("valid_to"),
            "created_at": current.get("created_at", now),
            "updated_at": now,
            "access_count": current.get("access_count", 0),
        }

    def _edge_snapshot_payload(
        *,
        snapshot: dict[str, Any],
        payload: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = existing or {}
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {
            "id": str(current.get("id") or payload.get("id") or f"preview-{uuid.uuid4()}").strip(),
            "tenant_id": str(snapshot.get("tenant_id", "")),
            "source_id": str(
                payload.get("source_id", current.get("source_id", "")) or current.get("source_id", "")
            ).strip(),
            "target_id": str(
                payload.get("target_id", current.get("target_id", "")) or current.get("target_id", "")
            ).strip(),
            "relationship": str(
                payload.get("relationship", current.get("relationship", "")) or current.get("relationship", "")
            ).strip(),
            "weight": float(payload.get("weight", current.get("weight", 1.0)) or current.get("weight", 1.0)),
            "metadata": current.get("metadata", {}),
            "created_at": current.get("created_at", now),
        }

    def _json_safe_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        safe = deepcopy(snapshot)
        for node in safe.get("nodes", []):
            node.pop("embedding", None)
        for transcript in safe.get("transcripts", []):
            transcript.pop("embedding", None)
        return safe

    async def graph_editor(request: Request) -> Response:
        mode = request.query_params.get("mode", "edit").strip().lower()
        if mode not in {"edit", "view"}:
            mode = "edit"
        scope = _scope_from_request(request)
        return HTMLResponse(
            render_graph_editor_html(
                mode=mode,
                project=scope["project"],
                agent_id=scope["agent_id"],
                session_id=scope["session_id"],
            )
        )

    async def graph_snapshot(request: Request) -> Response:
        scope = _scope_from_request(request)
        graph, _ = _require_http_scope(request, "graph:read")
        include_source_prompt = request.query_params.get("include_source_prompt", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            snapshot = graph.get_graph_snapshot(include_source_prompt=include_source_prompt, **scope)
        except TypeError:
            snapshot = graph.get_graph_snapshot(**scope)
        _emit_http_audit(
            request,
            event_type="graph.snapshot.read",
            resource_type="graph_snapshot",
            action="read",
            metadata={"project": scope["project"], "agent_id": scope["agent_id"], "session_id": scope["session_id"]},
        )
        return JSONResponse(
            {
                "tenant_id": snapshot.get("tenant_id", ""),
                "schema_version": snapshot.get("schema_version", 1),
                "nodes": snapshot.get("nodes", []),
                "edges": snapshot.get("edges", []),
                "ui": snapshot.get("ui", {}),
                "node_types": [node_type.value for node_type in NodeType],
                "relation_types": [relation.value for relation in RelationType],
            }
        )

    async def graph_transcripts(request: Request) -> Response:
        scope = _scope_from_request(request)
        limit = int(request.query_params.get("limit", "200") or "200")
        offset = int(request.query_params.get("offset", "0") or "0")
        query_text = request.query_params.get("query", "").strip()
        graph, _ = _require_http_scope(request, "graph:read")
        if query_text and hasattr(graph, "search_transcript_records"):
            hits = graph.search_transcript_records(query=query_text, limit=limit, **scope)
            _emit_http_audit(
                request,
                event_type="record.read",
                resource_type="transcript_records",
                action="read",
                metadata={"mode": "hybrid", "query": query_text, "limit": limit},
            )
            return JSONResponse(
                {
                    "mode": "hybrid",
                    "query": query_text,
                    "hits": [hit.model_dump(mode="json") for hit in hits],
                }
            )
        if not hasattr(graph, "list_transcript_records"):
            raise ValidationFailure("Transcript listing is not available in this backend.")
        records = graph.list_transcript_records(limit=limit, offset=offset, **scope)
        total_count = graph.count_transcript_records(**scope)
        _emit_http_audit(
            request,
            event_type="record.read",
            resource_type="transcript_records",
            action="read",
            metadata={"mode": "chronological", "limit": limit, "offset": offset},
        )
        return JSONResponse(
            {
                "mode": "chronological",
                "records": [record.model_dump(mode="json") for record in records],
                "pagination": {
                    "offset": offset,
                    "limit": limit,
                    "total_count": total_count,
                },
            }
        )

    async def graph_retrieval_debug(request: Request) -> Response:
        payload = await request.json()
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        query_text = str(payload.get("query", "")).strip()
        if not query_text:
            raise ValidationFailure("query is required.")
        max_nodes = int(payload.get("max_nodes", 8) or 8)
        max_depth = int(payload.get("max_depth", 1) or 1)
        graph, _ = _require_http_scope(request, "graph:read")
        debug = graph.debug_retrieval(
            query=query_text, max_nodes=max_nodes, max_depth=max_depth, retrieval_mode="hybrid", **scope
        )
        fusion = graph.query(
            query=query_text,
            retrieval_mode="hybrid",
            max_nodes=max_nodes,
            max_depth=max_depth,
            **scope,
        )
        token_estimate = (
            sum(
                max(1, len((hit.content if hasattr(hit, "content") else "") or "").split())
                for hit in fusion.fusion_hits
            )
            * 1.33
        )
        fused_ranking: list[dict[str, Any]] = []
        for hit in fusion.fusion_hits:
            reasoning: list[str] = []
            if hit.source_lane == "graph":
                reasoning.append("semantic graph node")
            if hit.graph_rank is not None:
                reasoning.append(f"graph rank {hit.graph_rank}")
            if hit.replay_rank is not None:
                reasoning.append(f"replay rank {hit.replay_rank}")
            if hit.session_id:
                reasoning.append(f"session {hit.session_id}")
            fused_ranking.append(
                {
                    "content": hit.content,
                    "score": hit.score,
                    "source_lane": hit.source_lane,
                    "graph_rank": hit.graph_rank,
                    "replay_rank": hit.replay_rank,
                    "fused_rank": hit.fused_rank,
                    "node_id": hit.node_id,
                    "session_id": hit.session_id,
                    "turn_index": hit.turn_index,
                    "transcript_snippet": hit.transcript_snippet,
                    "reasoning": ", ".join(reasoning) or "ranked by reciprocal-rank fusion",
                }
            )
        _emit_http_audit(
            request,
            event_type="graph.query.executed",
            resource_type="retrieval_debug",
            action="read",
            metadata={"query": query_text, "max_nodes": max_nodes, "max_depth": max_depth},
        )
        return JSONResponse(
            {
                "debug": debug,
                "replay_hits": [hit.model_dump(mode="json") for hit in fusion.replay_hits],
                "fusion_hits": fused_ranking,
                "token_estimate": int(token_estimate),
            }
        )

    async def graph_abhi_preview(request: Request) -> Response:
        scope = _scope_from_request(request)
        graph, _ = _require_http_scope(request, "graph:read")
        snapshot, document = _build_scoped_abhi(graph, scope)
        validation = validate_abhi_document(document, input_path="live://graph")
        _emit_http_audit(
            request,
            event_type="export.previewed",
            resource_type="abhi_preview",
            action="read",
            metadata={"project": scope["project"], "agent_id": scope["agent_id"], "session_id": scope["session_id"]},
        )
        return JSONResponse(
            {
                "tenant_id": snapshot.get("tenant_id", ""),
                "scope": scope,
                "schema": document.get("schema", {}),
                "constraints": document.get("constraints", []),
                "queries": document.get("queries", {}),
                "events": document.get("events", {}),
                "versions": document.get("versions", []),
                "integrity": document.get("integrity", {}),
                "validation": validation.model_dump(mode="json"),
            }
        )

    async def graph_query(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:read")
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        _, document = _build_scoped_abhi(graph, scope)
        result = execute_abhi_query(
            document,
            query_id=str(payload.get("query_id", "")).strip(),
            query_text=str(payload.get("query", "")).strip(),
        )
        _emit_http_audit(
            request,
            event_type="graph.query.executed",
            resource_type="abhi_query",
            action="read",
            metadata={
                "query_id": str(payload.get("query_id", "")).strip(),
                "query": str(payload.get("query", "")).strip(),
            },
        )
        return JSONResponse(result)

    async def graph_diff_feed(request: Request) -> Response:
        since = request.query_params.get("since", "24h").strip() or "24h"
        graph, _ = _require_http_scope(request, "graph:read")
        diff = graph.graph_diff(since=since)
        _emit_http_audit(
            request,
            event_type="graph.diff.read",
            resource_type="graph_diff",
            action="read",
            metadata={"since": since},
        )
        return JSONResponse(
            {
                "since": diff.since,
                "added_nodes": [node.model_dump(mode="json") for node in diff.added_nodes],
                "updated_nodes": [node.model_dump(mode="json") for node in diff.updated_nodes],
                "created_edges": [edge.model_dump(mode="json") for edge in diff.created_edges],
                "contradiction_edges": [edge.model_dump(mode="json") for edge in diff.contradiction_edges],
            }
        )

    async def graph_save_ui(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        saved = graph.save_ui_state(
            project=str(payload.get("project", "")).strip(),
            agent_id=str(payload.get("agent_id", "")).strip(),
            session_id=str(payload.get("session_id", "")).strip(),
            positions=payload.get("positions"),
            zoom=float(payload["zoom"]) if "zoom" in payload and payload.get("zoom") is not None else None,
            viewport=payload.get("viewport"),
            groups=payload.get("groups"),
            collapsed_groups=payload.get("collapsed_groups"),
            selected_nodes=payload.get("selected_nodes"),
        )
        return JSONResponse(saved)

    async def graph_restore(request: Request) -> Response:
        payload = await request.json()
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        graph, _ = _require_http_scope(request, "graph:write")
        current = graph.get_graph_snapshot(**scope)
        desired_nodes = {
            str(node.get("id", "")).strip(): node
            for node in payload.get("nodes", [])
            if str(node.get("id", "")).strip()
        }
        desired_edges = {
            str(edge.get("id", "")).strip(): edge
            for edge in payload.get("edges", [])
            if str(edge.get("id", "")).strip()
        }
        current_nodes = {
            str(node.get("id", "")).strip(): node
            for node in current.get("nodes", [])
            if str(node.get("id", "")).strip()
        }
        current_edges = {
            str(edge.get("id", "")).strip(): edge
            for edge in current.get("edges", [])
            if str(edge.get("id", "")).strip()
        }

        for edge_id in list(current_edges):
            if edge_id not in desired_edges:
                graph.delete_edge(edge_id=edge_id)

        for node_id in list(current_nodes):
            if node_id not in desired_nodes:
                graph.delete_node(node_id=node_id)

        for node_id, node_payload in desired_nodes.items():
            tags = [str(tag).strip() for tag in node_payload.get("tags", []) if str(tag).strip()]
            if node_id in current_nodes:
                graph.update_node(
                    node_id=node_id,
                    label=node_payload.get("label"),
                    content=node_payload.get("content"),
                    tags=tags,
                )
                continue
            graph.add_node(
                node_id=node_id,
                label=str(node_payload.get("label", "")).strip(),
                content=str(node_payload.get("content", "")).strip(),
                node_type=NodeType(str(node_payload.get("node_type", "note")).strip() or "note"),
                tags=tags,
                agent_id=str(node_payload.get("agent_id", scope["agent_id"])).strip(),
                project=str(node_payload.get("project", scope["project"])).strip(),
                session_id=str(node_payload.get("session_id", scope["session_id"])).strip(),
            )

        for edge_id, edge_payload in desired_edges.items():
            if edge_id in current_edges:
                graph.update_edge(
                    edge_id=edge_id,
                    source_id=str(edge_payload.get("source_id", "")).strip() or None,
                    target_id=str(edge_payload.get("target_id", "")).strip() or None,
                    relationship=str(edge_payload.get("relationship", "")).strip() or None,
                    weight=float(edge_payload["weight"])
                    if "weight" in edge_payload and edge_payload.get("weight") is not None
                    else None,
                )
                continue
            graph.add_edge(
                edge_id=edge_id,
                source_id=str(edge_payload.get("source_id", "")).strip(),
                target_id=str(edge_payload.get("target_id", "")).strip(),
                relationship=str(edge_payload.get("relationship", "")).strip(),
                weight=float(edge_payload.get("weight", 1.0)),
            )

        ui = payload.get("ui", {}) or {}
        saved = graph.save_ui_state(
            project=scope["project"],
            agent_id=scope["agent_id"],
            session_id=scope["session_id"],
            positions=ui.get("positions"),
            zoom=float(ui["zoom"]) if "zoom" in ui and ui.get("zoom") is not None else None,
            viewport=ui.get("viewport"),
            groups=ui.get("groups"),
            collapsed_groups=ui.get("collapsed_groups"),
            selected_nodes=ui.get("selected_nodes"),
        )
        restored = graph.get_graph_snapshot(**scope)
        return JSONResponse(
            {
                "nodes": restored.get("nodes", []),
                "edges": restored.get("edges", []),
                "ui": saved,
            }
        )

    async def graph_create_node(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        snapshot = graph.get_graph_snapshot(**scope)
        snapshot["nodes"] = [*snapshot.get("nodes", []), _node_snapshot_payload(snapshot=snapshot, payload=payload)]
        _validate_live_snapshot(snapshot)
        created = graph.add_node(
            node_id=str(payload.get("id", "")).strip() or None,
            label=str(payload.get("label", "")).strip(),
            content=str(payload.get("content", "")).strip(),
            node_type=NodeType(str(payload.get("node_type", "note")).strip() or "note"),
            tags=[str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()],
            agent_id=str(payload.get("agent_id", "")).strip(),
            project=str(payload.get("project", "")).strip(),
            session_id=str(payload.get("session_id", "")).strip(),
        )
        return JSONResponse(created.node.model_dump(mode="json"))

    async def graph_update_node(request: Request) -> Response:
        node_id = request.path_params["node_id"]
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        snapshot = graph.get_graph_snapshot()
        existing = next(
            (node for node in snapshot.get("nodes", []) if str(node.get("id", "")).strip() == node_id), None
        )
        if existing is None:
            raise ValidationFailure(f"Node not found: {node_id}")
        snapshot["nodes"] = [
            _node_snapshot_payload(snapshot=snapshot, payload=payload, existing=existing)
            if str(node.get("id", "")).strip() == node_id
            else node
            for node in snapshot.get("nodes", [])
        ]
        _validate_live_snapshot(snapshot)
        updated = graph.update_node(
            node_id=node_id,
            label=payload.get("label"),
            content=payload.get("content"),
            tags=payload.get("tags"),
        )
        return JSONResponse(updated.model_dump(mode="json"))

    async def graph_delete_node(request: Request) -> Response:
        node_id = request.path_params["node_id"]
        graph, _ = _require_http_scope(request, "graph:write")
        deleted = graph.delete_node(node_id=node_id)
        return JSONResponse(deleted.model_dump(mode="json"))

    async def graph_create_edge(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        raw_weight = payload.get("weight", 1.0)
        try:
            weight_value = float(raw_weight)
        except (TypeError, ValueError):
            raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
        if not (0 <= weight_value <= 1):
            raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
        snapshot = graph.get_graph_snapshot()
        snapshot["edges"] = [*snapshot.get("edges", []), _edge_snapshot_payload(snapshot=snapshot, payload=payload)]
        _validate_live_snapshot(snapshot)
        edge = graph.add_edge(
            edge_id=str(payload.get("id", "")).strip() or None,
            source_id=str(payload.get("source_id", "")).strip(),
            target_id=str(payload.get("target_id", "")).strip(),
            relationship=str(payload.get("relationship", "")).strip(),
            weight=float(payload.get("weight", 1.0)),
        )
        return JSONResponse(edge.model_dump(mode="json"))

    async def graph_update_edge(request: Request) -> Response:
        edge_id = request.path_params["edge_id"]
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        if "weight" in payload and payload.get("weight") is not None:
            try:
                weight_value = float(payload["weight"])
            except (TypeError, ValueError):
                raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
            if not (0 <= weight_value <= 1):
                raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
        snapshot = graph.get_graph_snapshot()
        existing = next(
            (item for item in snapshot.get("edges", []) if str(item.get("id", "")).strip() == edge_id), None
        )
        if existing is None:
            raise ValidationFailure(f"Edge not found: {edge_id}")
        snapshot["edges"] = [
            _edge_snapshot_payload(snapshot=snapshot, payload=payload, existing=existing)
            if str(item.get("id", "")).strip() == edge_id
            else item
            for item in snapshot.get("edges", [])
        ]
        _validate_live_snapshot(snapshot)

        edge = graph.update_edge(
            edge_id=edge_id,
            source_id=str(payload.get("source_id", "")).strip() or None,
            target_id=str(payload.get("target_id", "")).strip() or None,
            relationship=str(payload.get("relationship", "")).strip() or None,
            weight=float(payload["weight"]) if "weight" in payload and payload.get("weight") is not None else None,
        )
        return JSONResponse(edge.model_dump(mode="json"))

    async def graph_delete_edge(request: Request) -> Response:
        edge_id = request.path_params["edge_id"]
        graph, _ = _require_http_scope(request, "graph:write")
        edge = graph.delete_edge(edge_id=edge_id)
        return JSONResponse(edge.model_dump(mode="json"))

    async def graph_export(request: Request) -> Response:
        scope = _scope_from_request(request)
        export_format = request.query_params.get("format", "abhi").strip().lower()
        graph = app_server.graph
        if export_format == "abhi":
            exported = graph.export_abhi(**scope)
            content = Path(exported.output_path).read_bytes()
            _emit_http_audit(
                request,
                event_type="export.downloaded",
                resource_type="abhi_export",
                resource_id=exported.output_path,
                action="download",
                metadata={"format": "abhi", "project": scope["project"]},
            )
            return Response(
                content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": 'attachment; filename="waggle-memory.abhi"'},
            )
        if export_format == "json":
            snapshot = graph.get_graph_snapshot(**scope)
            _emit_http_audit(
                request,
                event_type="export.downloaded",
                resource_type="backup",
                action="download",
                metadata={"format": "json", "project": scope["project"]},
            )
            return Response(
                json.dumps(snapshot, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="waggle-backup.json"'},
            )
        raise ValidationFailure("format must be one of: abhi, json.")

    async def graph_import(request: Request) -> Response:
        payload = await request.json()
        import_format = str(payload.get("format", "abhi")).strip().lower()
        content = str(payload.get("content", ""))
        content_base64 = str(payload.get("content_base64", ""))
        suffix = ".abhi" if import_format == "abhi" else ".json"
        if import_format == "abhi" and content_base64:
            data = base64.b64decode(content_base64)
        else:
            data = content.encode("utf-8")
        temp_path = write_secure_temp(data, suffix=suffix)
        try:
            graph, _ = _require_http_scope(request, "graph:write")
            imported_node_ids: list[str] = []
            if import_format == "abhi":
                document = load_abhi_document(temp_path)
                imported_node_ids = [
                    str(node.get("id", "")).strip()
                    for node in abhi_to_snapshot(document, fallback_tenant_id=graph.tenant_id).get("nodes", [])
                    if str(node.get("id", "")).strip()
                ]
                imported = graph.import_abhi(input_path=temp_path)
            elif import_format == "json":
                snapshot = json.loads(content)
                imported_node_ids = [
                    str(node.get("id", "")).strip()
                    for node in snapshot.get("nodes", [])
                    if str(node.get("id", "")).strip()
                ]
                imported = graph.import_graph_backup(input_path=temp_path)
            else:
                raise ValidationFailure("format must be one of: abhi, json.")
            return JSONResponse({**imported.model_dump(mode="json"), "imported_node_ids": imported_node_ids})
        finally:
            temp_path.unlink(missing_ok=True)

    async def graph_import_preview(request: Request) -> Response:
        payload = await request.json()
        import_format = str(payload.get("format", "abhi")).strip().lower()
        content = str(payload.get("content", ""))
        content_base64 = str(payload.get("content_base64", ""))
        suffix = ".abhi" if import_format == "abhi" else ".json"
        if import_format == "abhi" and content_base64:
            data = base64.b64decode(content_base64)
        else:
            data = content.encode("utf-8")
        temp_path = write_secure_temp(data, suffix=suffix)
        try:
            graph, _ = _require_http_scope(request, "graph:read")
            if import_format == "abhi":
                validation = graph.validate_abhi(input_path=temp_path)
                inspect_result = graph.inspect_abhi(input_path=temp_path)
                document = load_abhi_document(temp_path)
                snapshot = abhi_to_snapshot(document, fallback_tenant_id=graph.tenant_id)
                return JSONResponse(
                    {
                        "validation": validation.model_dump(mode="json"),
                        "inspect": inspect_result.model_dump(mode="json"),
                        "snapshot": {
                            "tenant_id": snapshot.get("tenant_id", ""),
                            "nodes": _json_safe_snapshot(snapshot).get("nodes", []),
                            "edges": snapshot.get("edges", []),
                            "ui": snapshot.get("ui", {}),
                        },
                        "imported_node_ids": [
                            str(node.get("id", "")).strip()
                            for node in snapshot.get("nodes", [])
                            if str(node.get("id", "")).strip()
                        ],
                    }
                )
            if import_format == "json":
                snapshot = json.loads(content)
                return JSONResponse(
                    {
                        "validation": {"valid": True, "errors": []},
                        "inspect": {
                            "node_count": len(snapshot.get("nodes", [])),
                            "edge_count": len(snapshot.get("edges", [])),
                        },
                        "snapshot": snapshot,
                        "imported_node_ids": [
                            str(node.get("id", "")).strip()
                            for node in snapshot.get("nodes", [])
                            if str(node.get("id", "")).strip()
                        ],
                    }
                )
            raise ValidationFailure("format must be one of: abhi, json.")
        finally:
            temp_path.unlink(missing_ok=True)

    async def graph_abhi_diff(request: Request) -> Response:
        payload = await request.json()
        content_a = str(payload.get("content_a", ""))
        content_b = str(payload.get("content_b", ""))
        content_a_base64 = str(payload.get("content_a_base64", ""))
        content_b_base64 = str(payload.get("content_b_base64", ""))
        if content_a_base64:
            data_a = base64.b64decode(content_a_base64)
        else:
            data_a = content_a.encode("utf-8")
        if content_b_base64:
            data_b = base64.b64decode(content_b_base64)
        else:
            data_b = content_b.encode("utf-8")
        path_a = write_secure_temp(data_a, suffix=".abhi")
        try:
            path_b = write_secure_temp(data_b, suffix=".abhi")
        except BaseException:
            path_a.unlink(missing_ok=True)
            raise
        try:
            graph, _ = _require_http_scope(request, "graph:read")
            diff = graph.diff_abhi(input_path_a=path_a, input_path_b=path_b)
            snapshot_a = abhi_to_snapshot(load_abhi_document(path_a), fallback_tenant_id=graph.tenant_id)
            snapshot_b = abhi_to_snapshot(load_abhi_document(path_b), fallback_tenant_id=graph.tenant_id)
            return JSONResponse(
                {
                    "diff": diff.model_dump(mode="json"),
                    "left": {
                        "nodes": _json_safe_snapshot(snapshot_a).get("nodes", []),
                        "edges": snapshot_a.get("edges", []),
                    },
                    "right": {
                        "nodes": _json_safe_snapshot(snapshot_b).get("nodes", []),
                        "edges": snapshot_b.get("edges", []),
                    },
                }
            )
        finally:
            path_a.unlink(missing_ok=True)
            path_b.unlink(missing_ok=True)

    async def admin_retention_status(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "admin:read")
        policy = graph.get_retention_policy(
            default_enabled=config.retention_enabled,
            default_retention_days=config.retention_days,
            default_prune_interval_hours=config.retention_prune_interval_hours,
        )
        payload = _serialize_retention_policy(policy)
        payload["recent_runs"] = [_serialize_retention_run(run) for run in graph.list_retention_runs(limit=5)]
        return JSONResponse(payload)

    async def admin_retention_update(request: Request) -> Response:
        payload = await request.json()
        graph, principal = _require_http_scope(
            request, "admin:write", tenant_override=str(payload.get("tenant_id", "") or "")
        )
        policy = graph.update_retention_policy(
            enabled=payload.get("enabled"),
            retention_days=payload.get("retention_days"),
            prune_interval_hours=payload.get("prune_interval_hours"),
            default_enabled=config.retention_enabled,
            default_retention_days=config.retention_days,
            default_prune_interval_hours=config.retention_prune_interval_hours,
        )
        graph.emit_audit_event(
            event_type="retention.policy.updated",
            actor_type="api_key" if principal is not None else "admin",
            actor_id=(principal.name or principal.api_key_id) if principal is not None else "local-http",
            api_key_id=principal.api_key_id if principal is not None else "",
            resource_type="retention_policy",
            resource_id=policy.tenant_id,
            action="update",
            metadata={
                "enabled": policy.enabled,
                "retention_days": policy.retention_days,
                "prune_interval_hours": policy.prune_interval_hours,
            },
            ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
        return JSONResponse(_serialize_retention_policy(policy))

    async def admin_retention_prune(request: Request) -> Response:
        payload = await request.json() if request.method != "GET" else {}
        graph, _ = _require_http_scope(request, "admin:write", tenant_override=str(payload.get("tenant_id", "") or ""))
        run = graph.prune_retention(
            batch_size=int(payload.get("batch_size", 1000) or 1000),
            default_enabled=config.retention_enabled,
            default_retention_days=config.retention_days,
            default_prune_interval_hours=config.retention_prune_interval_hours,
        )
        response = _serialize_retention_run(run)
        response["policy"] = _serialize_retention_policy(
            graph.get_retention_policy(
                default_enabled=config.retention_enabled,
                default_retention_days=config.retention_days,
                default_prune_interval_hours=config.retention_prune_interval_hours,
            )
        )
        return JSONResponse(response)

    async def admin_retention_runs(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "admin:read")
        limit = int(request.query_params.get("limit", "20") or "20")
        runs = graph.list_retention_runs(limit=limit)
        return JSONResponse([_serialize_retention_run(run) for run in runs])

    async def admin_audit_events(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "admin:read")
        limit = int(request.query_params.get("limit", "100") or "100")
        events = graph.list_audit_events(
            limit=limit,
            event_type=request.query_params.get("type", "").strip(),
            actor_id=request.query_params.get("actor_id", "").strip(),
            resource_id=request.query_params.get("resource_id", "").strip(),
            resource_type=request.query_params.get("resource_type", "").strip(),
            status=request.query_params.get("status", "").strip(),
        )
        return JSONResponse([_serialize_audit_event(event) for event in events])

    raw_app = Starlette(
        routes=[
            Route("/health/live", live),
            Route("/health/ready", ready),
            Route("/metrics", metrics_endpoint),
            Route("/graph", graph_editor),
            Route("/api/graph", graph_snapshot, methods=["GET"]),
            Route("/api/graph/transcripts", graph_transcripts, methods=["GET"]),
            Route("/api/graph/retrieval-debug", graph_retrieval_debug, methods=["POST"]),
            Route("/api/graph/abhi", graph_abhi_preview, methods=["GET"]),
            Route("/api/graph/abhi/preview-import", graph_import_preview, methods=["POST"]),
            Route("/api/graph/abhi/diff", graph_abhi_diff, methods=["POST"]),
            Route("/api/graph/query", graph_query, methods=["POST"]),
            Route("/api/graph/diff", graph_diff_feed, methods=["GET"]),
            Route("/api/graph/ui", graph_save_ui, methods=["PATCH"]),
            Route("/api/graph/restore", graph_restore, methods=["POST"]),
            Route("/api/graph/nodes", graph_create_node, methods=["POST"]),
            Route("/api/graph/nodes/{node_id:str}", graph_update_node, methods=["PATCH"]),
            Route("/api/graph/nodes/{node_id:str}", graph_delete_node, methods=["DELETE"]),
            Route("/api/graph/edges", graph_create_edge, methods=["POST"]),
            Route("/api/graph/edges/{edge_id:str}", graph_update_edge, methods=["PATCH"]),
            Route("/api/graph/edges/{edge_id:str}", graph_delete_edge, methods=["DELETE"]),
            Route("/api/graph/export", graph_export, methods=["GET"]),
            Route("/api/graph/import", graph_import, methods=["POST"]),
            Route("/api/admin/retention", admin_retention_status, methods=["GET"]),
            Route("/api/admin/retention", admin_retention_update, methods=["PUT", "PATCH"]),
            Route("/api/admin/retention/prune", admin_retention_prune, methods=["POST"]),
            Route("/api/admin/retention/runs", admin_retention_runs, methods=["GET"]),
            Route("/api/admin/audit-events", admin_audit_events, methods=["GET"]),
            Mount("/graph-assets", app=StaticFiles(packages=[("waggle", "static/graph")], html=False, check_dir=False)),
            Mount("/mcp", app=service.mcp_asgi),
        ],
        lifespan=service.lifespan,
        exception_handlers={WaggleError: waggle_error_handler},
    )
    return _RequestBodySizeMiddleware(raw_app, max_bytes=config.max_payload_bytes)


def _run_graph_editor_command(config: AppConfig, args: argparse.Namespace) -> int:
    host = str(getattr(args, "host", "") or config.http_host or "127.0.0.1")
    port = int(getattr(args, "port", 8686) or 8686)
    should_open = bool(getattr(args, "open", True))
    mode = str(getattr(args, "command", "edit-graph")).strip().lower()
    page_mode = "view" if mode == "view-graph" else "edit"

    config.transport = "http"
    config.http_host = host
    config.http_port = port
    app_server = WaggleServer(config=config)
    http_app = create_http_application(app_server, config)
    url = f"http://{host}:{port}/graph?mode={page_mode}"

    print(f"Launching Waggle Graph Studio at {url}")
    print("Use Ctrl+C in this terminal to stop the editor server.")

    if should_open:

        def _open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("graph_editor_open_failed", extra={"error": str(exc), "url": url})

        timer = threading.Timer(0.4, _open_browser)
        timer.daemon = True
        timer.start()

    uvicorn.run(http_app, host=host, port=port, log_level=config.log_level.lower())
    return 0


_APP: WaggleServer | None = None


def _default_graph(config: AppConfig | None = None) -> Any:
    try:
        return _build_backend(config or AppConfig.from_env())
    except ValidationFailure as exc:
        raise RuntimeError(str(exc)) from exc


def get_app(config: AppConfig | None = None) -> WaggleServer:
    global _APP
    if _APP is None:
        _APP = WaggleServer(config=config or AppConfig.from_env())
    return _APP


async def run_stdio(config: AppConfig) -> None:
    app = get_app(config)
    graph = app._root_graph
    em = graph.embedding_model
    is_bundled_runtime = os.environ.get("WAGGLE_BUNDLED_RUNTIME", "").strip() in {"1", "true", "yes"}
    if (
        not is_bundled_runtime
        and not config.is_fast_mode
        and hasattr(em, "start_background_warmup")
        and not getattr(em, "_warmup_started", False)
    ):
        # Kick off background warmup so the first semantic call is fast.
        em.start_background_warmup()
    if config.is_strict_mode:
        # Block until the model is ready before accepting any requests.
        LOGGER.info("stdio_strict_mode_waiting_for_embedding", extra={"model": em.model_name})
        if hasattr(em, "_ready_event"):
            em._ready_event.wait(timeout=120.0)
        LOGGER.info(
            "stdio_strict_mode_embedding_status",
            extra={"status": getattr(em, "warmup_status", "unknown"), "error": getattr(em, "warmup_error", "")},
        )
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.server.run(read_stream, write_stream, app.initialization_options())


def run_http(config: AppConfig) -> None:
    app_server = get_app(config)
    http_app = create_http_application(app_server, config)
    uvicorn.run(http_app, host=config.http_host, port=config.http_port, log_level=config.log_level.lower())


_FEATURES_GUIDE = """\
waggle-mcp feature guide
========================

Core graph workflow
-------------------
1. Ingest memory
   - observe_conversation : extract structured facts/decisions from a finished turn
   - decompose_and_store  : split long content into linked atomic nodes
   - store_node           : save one standalone node directly
   - store_edge           : connect nodes explicitly when relationship is known

2. Retrieve context
   - query_graph          : semantic retrieval over nodes plus graph/replay/fusion context
   - get_related          : walk outward from one node to inspect connected context
   - get_node_history     : inspect one node's evidence, validity, and linked context
   - timeline             : see recent graph events chronologically
   - prime_context        : build a compact briefing for a new model/session

3. Resolve conflicts
   - list_conflicts       : list contradiction/update edges
   - resolve_conflict     : mark a conflict as resolved without deleting history

4. Export / handoff  (git-vocabulary)
   - commit                : snapshot memory to a portable .abhi file (waggle commit)
   - checkpoint-context    : scoped handoff checkpoint for session/app switches
   - pull                  : load a .abhi file into the graph (waggle pull)
   - push                  : upload to Google Drive (waggle push)
   - diff                  : compare two .abhi files (waggle diff)
   - merge                 : three-way merge .abhi files (waggle merge)
   - fsck                  : validate an .abhi file (waggle fsck)
   - show                  : inspect an .abhi file without importing (waggle show)
   - grep                  : query an .abhi file (waggle grep)
   - export_markdown_vault : export one-file-per-node markdown for manual editing
   - import_markdown_vault : re-import edited markdown vault files
   - export_graph_html     : interactive graph visualization

5. Inspect the graph
   - get_stats            : node/edge counts and high-level graph stats
   - list_context_scopes  : available project / agent / session scopes
   - get_topics           : topic clusters via community detection
   - graph_diff           : recently changed nodes and edges

Important behavior
------------------
- store_node alone does not create edges.
- Edges come from:
  - explicit store_edge calls
  - observe_conversation extraction
  - decompose_and_store inferred structure
  - automatic contradiction/update detection in some cases
- The graph-aware tools are what bring connected context back to the model:
  - query_graph
  - get_related
  - get_node_history
  - prime_context
  - commit (waggle commit — snapshot memory to .abhi)

Common workflows
----------------
- Quick setup:
  waggle-mcp setup --yes

- Start the MCP server:
  waggle-mcp serve

- Export a handoff bundle:
  waggle-mcp export-context-bundle --mode query --query "why did we choose PostgreSQL?"

- Checkpoint the current context before switching sessions/apps:
  waggle-mcp checkpoint-context --project MCP --session-id thread-123 --output ./handoff.abhi

- Resume from a portable checkpoint when scoped DB recall is cold:
  waggle-mcp pull ./handoff.abhi

- Edit memory as markdown:
  waggle-mcp export-markdown-vault --root-path ./vault
  waggle-mcp import-markdown-vault --root-path ./vault
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waggle-mcp",
        description=(
            "Persistent graph memory for MCP-compatible AI clients. "
            "Use 'waggle-mcp features' for a detailed guide to ingestion, graph retrieval, "
            "conflict handling, and export workflows."
        ),
        epilog="Examples: 'waggle-mcp setup --yes', 'waggle-mcp serve', 'waggle-mcp features'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="Run the MCP server using the configured stdio or HTTP transport.")
    serve.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="Override WAGGLE_TRANSPORT for this run.",
    )
    graph_editor = subparsers.add_parser(
        "edit-graph",
        help="Launch the visual graph editor in a browser window.",
        description=(
            "Start the Waggle HTTP app for local graph editing and open /graph in the browser by default. "
            "Use this for mouse-and-keyboard graph editing: add, remove, connect, reposition, import, and export."
        ),
    )
    graph_editor.add_argument("--host", default="127.0.0.1")
    graph_editor.add_argument("--port", type=int, default=8686)
    graph_editor.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    graph_viewer = subparsers.add_parser(
        "view-graph",
        help="Launch the visual graph viewer/editor in a browser window.",
        description="Alias for edit-graph. Starts the local graph UI and opens it in the browser by default.",
    )
    graph_viewer.add_argument("--host", default="127.0.0.1")
    graph_viewer.add_argument("--port", type=int, default=8686)
    graph_viewer.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    graph_ui = subparsers.add_parser(
        "ui",
        help="Launch the local graph UI in the browser.",
        description="Alias for edit-graph. Starts the localhost Graph Studio and opens it by default.",
    )
    graph_ui.add_argument("--host", default="127.0.0.1")
    graph_ui.add_argument("--port", type=int, default=8686)
    graph_ui.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    graph_studio = subparsers.add_parser(
        "graph-studio",
        help="Alias for the local Graph Studio browser UI.",
        description="Alias for ui/edit-graph. Starts the localhost Graph Studio and opens it by default.",
    )
    graph_studio.add_argument("--host", default="127.0.0.1")
    graph_studio.add_argument("--port", type=int, default=8686)
    graph_studio.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    open_studio = subparsers.add_parser(
        "open-studio",
        help="Alias for graph-studio.",
        description="Alias for graph-studio/ui/edit-graph. Starts the localhost Graph Studio and opens it by default.",
    )
    open_studio.add_argument("--host", default="127.0.0.1")
    open_studio.add_argument("--port", type=int, default=8686)
    open_studio.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    create_tenant = subparsers.add_parser(
        "create-tenant", help="Create or update a tenant record in the active backend."
    )
    create_tenant.add_argument("--tenant-id", required=True)
    create_tenant.add_argument("--name", default="")

    create_api_key = subparsers.add_parser("create-api-key", help="Issue an API key for a tenant.")
    create_api_key.add_argument("--tenant-id", required=True)
    create_api_key.add_argument("--name", default="")
    create_api_key.add_argument("--expires-in-days", type=int, default=0)
    create_api_key.add_argument("--created-by", default="")
    create_api_key.add_argument("--scopes", default="graph:read,graph:write,admin:read,admin:write")

    list_api_keys = subparsers.add_parser("list-api-keys", help="List API keys for a tenant.")
    list_api_keys.add_argument("--tenant-id", required=True)

    revoke_api_key = subparsers.add_parser("revoke-api-key", help="Revoke an API key.")
    revoke_api_key.add_argument("--api-key-id", required=True)
    revoke_api_key.add_argument("--tenant-id", default="")

    retention_status = subparsers.add_parser("retention-status", help="Show the active retention policy for a tenant.")
    retention_status.add_argument("--tenant-id", default="")

    set_retention = subparsers.add_parser("set-retention", help="Create or update the retention policy for a tenant.")
    set_retention.add_argument("--tenant-id", default="")
    set_retention.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    set_retention.add_argument("--days", type=int, default=None)
    set_retention.add_argument("--interval-hours", type=int, default=None)

    prune_retention = subparsers.add_parser("prune-retention", help="Run retention pruning immediately for a tenant.")
    prune_retention.add_argument("--tenant-id", default="")
    prune_retention.add_argument("--batch-size", type=int, default=1000)

    list_retention_runs = subparsers.add_parser(
        "list-retention-runs", help="List recent retention prune runs for a tenant."
    )
    list_retention_runs.add_argument("--tenant-id", default="")
    list_retention_runs.add_argument("--limit", type=int, default=20)

    list_audit_events = subparsers.add_parser("list-audit-events", help="List recent audit events for a tenant.")
    list_audit_events.add_argument("--tenant-id", default="")
    list_audit_events.add_argument("--limit", type=int, default=100)
    list_audit_events.add_argument("--type", dest="event_type", default="")
    list_audit_events.add_argument("--actor-id", default="")
    list_audit_events.add_argument("--resource-id", default="")
    list_audit_events.add_argument("--resource-type", default="")
    list_audit_events.add_argument("--status", default="")

    migrate_sqlite = subparsers.add_parser(
        "migrate-sqlite", help="Export a SQLite graph and import it into the configured Neo4j backend."
    )
    migrate_sqlite.add_argument("--db-path", required=True)
    migrate_sqlite.add_argument("--tenant-id", required=True)

    export_abhi = subparsers.add_parser(
        "export",
        help="Export the current memory graph in a portable format. (alias: commit)",
    )
    export_abhi.add_argument("--output", dest="output_path", default=None)
    export_abhi.add_argument("--project", default="")
    export_abhi.add_argument("--agent-id", default="")
    export_abhi.add_argument("--session-id", default="")
    export_abhi.add_argument("--scope", choices=["all", "project", "session", "since-date"], default="all")
    export_abhi.add_argument("--since-date", default="")
    export_abhi.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    export_abhi.add_argument("--encrypt", action="store_true")
    export_abhi.add_argument("--sign", action="store_true")
    export_abhi.add_argument("--signing-key-dir", default="~/.waggle/keys")
    export_abhi.add_argument("--redact", dest="redact_patterns", action="append", default=[])
    export_abhi.add_argument("--passphrase-env", default="")
    export_abhi.add_argument(
        "--force", action="store_true", help="Export even if transcript secret scan finds likely credentials or tokens."
    )

    # git-vocabulary alias: waggle commit == waggle export
    commit_abhi = subparsers.add_parser(
        "commit",
        help="Snapshot the current memory graph to a portable .abhi file. (waggle commit)",
        description=(
            "Save the current memory graph as a portable .abhi file — like git commit for your AI context. "
            "The output file can be shared, diffed, merged, and pulled into any Waggle-enabled client."
        ),
    )
    commit_abhi.add_argument("--output", dest="output_path", default=None)
    commit_abhi.add_argument("--project", default="")
    commit_abhi.add_argument("--agent-id", default="")
    commit_abhi.add_argument("--session-id", default="")
    commit_abhi.add_argument("--scope", choices=["all", "project", "session", "since-date"], default="all")
    commit_abhi.add_argument("--since-date", default="")
    commit_abhi.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    commit_abhi.add_argument("--encrypt", action="store_true")
    commit_abhi.add_argument("--sign", action="store_true")
    commit_abhi.add_argument("--signing-key-dir", default="~/.waggle/keys")
    commit_abhi.add_argument("--redact", dest="redact_patterns", action="append", default=[])
    commit_abhi.add_argument("--passphrase-env", default="")
    commit_abhi.add_argument(
        "--force", action="store_true", help="Commit even if transcript secret scan finds likely credentials or tokens."
    )

    checkpoint_context = subparsers.add_parser(
        "checkpoint-context",
        help="Create a scoped .abhi checkpoint for context-switch handoff.",
        description=(
            "Checkpoint the current project/session context to a portable .abhi file before switching "
            "sessions or apps. This is a thin wrapper around the existing scoped export flow: keep SQLite "
            "as the live store, use .abhi as the portable handoff artifact."
        ),
    )
    checkpoint_context.add_argument("--output", dest="output_path", default=None)
    checkpoint_context.add_argument("--project", default="")
    checkpoint_context.add_argument("--agent-id", default="")
    checkpoint_context.add_argument("--session-id", default="")
    checkpoint_context.add_argument("--scope", choices=["project", "session", "all", "since-date"], default="")
    checkpoint_context.add_argument("--since-date", default="")
    checkpoint_context.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    checkpoint_context.add_argument("--encrypt", action="store_true")
    checkpoint_context.add_argument("--sign", action="store_true")
    checkpoint_context.add_argument("--signing-key-dir", default="~/.waggle/keys")
    checkpoint_context.add_argument("--redact", dest="redact_patterns", action="append", default=[])
    checkpoint_context.add_argument("--passphrase-env", default="")
    checkpoint_context.add_argument(
        "--force",
        action="store_true",
        help="Checkpoint even if transcript secret scan finds likely credentials or tokens.",
    )

    clear_session = subparsers.add_parser(
        "clear-session",
        help="Delete all graph memory data for one session.",
    )
    clear_session.add_argument("--session-id", required=True)
    clear_session.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_session.add_argument(
        "--dry-run", action="store_true", help="Preview the clear operation without deleting data."
    )

    clear_project = subparsers.add_parser(
        "clear-project",
        help="Delete all graph memory data for one project/repository scope.",
    )
    clear_project.add_argument("--project", required=True)
    clear_project.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_project.add_argument(
        "--dry-run", action="store_true", help="Preview the clear operation without deleting data."
    )

    clear_all = subparsers.add_parser(
        "clear-all",
        help="Delete all graph memory data for the current tenant.",
    )
    clear_all.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_all.add_argument("--dry-run", action="store_true", help="Preview the clear operation without deleting data.")

    import_abhi = subparsers.add_parser(
        "import",
        help="Import a portable memory file into the active backend. (alias: pull <local-file>)",
    )
    import_abhi.add_argument("input_path", nargs="?")
    import_abhi.add_argument("--input", dest="input_path_flag", default="")
    import_abhi.add_argument("--namespace", default="")
    import_abhi.add_argument(
        "--merge-strategy", choices=["skip-existing", "overwrite", "branch"], default="skip-existing"
    )
    import_abhi.add_argument("--verify-signature", action="store_true")
    import_abhi.add_argument("--read-only", action="store_true")
    import_abhi.add_argument("--reembed-on-mismatch", action="store_true")
    import_abhi.add_argument("--passphrase-env", default="")

    validate_abhi = subparsers.add_parser(
        "validate",
        help="Validate a portable .abhi memory file. (alias: fsck)",
    )
    validate_abhi.add_argument("--input", dest="input_path", required=True)
    validate_abhi.add_argument("--passphrase-env", default="")

    # git-vocabulary alias: waggle fsck == waggle validate
    fsck_abhi = subparsers.add_parser(
        "fsck",
        help="Verify integrity of an .abhi memory file. (waggle fsck)",
        description=(
            "Verify the integrity hash, schema compliance, and constraint satisfaction of an .abhi file "
            "without importing it — like git fsck for your memory graph."
        ),
    )
    fsck_abhi.add_argument("--input", dest="input_path", required=True)
    fsck_abhi.add_argument("--passphrase-env", default="")

    inspect_abhi = subparsers.add_parser(
        "inspect",
        help="Inspect an .abhi memory file without importing it. (alias: show)",
    )
    inspect_abhi.add_argument("--input", dest="input_path", required=True)
    inspect_abhi.add_argument("--passphrase-env", default="")

    # git-vocabulary alias: waggle show == waggle inspect
    show_abhi = subparsers.add_parser(
        "show",
        help="Inspect an .abhi memory file without importing it. (waggle show)",
        description=(
            "Show summary stats, node/edge type breakdowns, and metadata counts for an .abhi file "
            "without loading it into the graph — like git show for a commit object."
        ),
    )
    show_abhi.add_argument("--input", dest="input_path", required=True)
    show_abhi.add_argument("--passphrase-env", default="")

    diff_abhi = subparsers.add_parser(
        "diff",
        help="Compare two .abhi memory files.",
    )
    diff_abhi.add_argument("input_path_a", nargs="?")
    diff_abhi.add_argument("input_path_b", nargs="?")
    diff_abhi.add_argument("--file-a", dest="input_path_a_flag", default="")
    diff_abhi.add_argument("--file-b", dest="input_path_b_flag", default="")

    merge_abhi = subparsers.add_parser(
        "merge",
        help="Three-way merge .abhi memory files.",
    )
    merge_abhi.add_argument("left_input_path", nargs="?")
    merge_abhi.add_argument("right_input_path", nargs="?")
    merge_abhi.add_argument("--base", dest="base_input_path", default="")
    merge_abhi.add_argument("--left", dest="left_input_path_flag", default="")
    merge_abhi.add_argument("--right", dest="right_input_path_flag", default="")
    merge_abhi.add_argument("--output", dest="output_path", required=True)
    merge_abhi.add_argument(
        "--merge-strategy", choices=["prefer_right", "prefer_left", "last_write_wins"], default="prefer_right"
    )

    query_abhi = subparsers.add_parser(
        "query",
        help="Execute a query against an .abhi memory file. (alias: grep)",
    )
    query_abhi.add_argument("--input", dest="input_path", required=True)
    query_abhi.add_argument("--query-id", default="")
    query_abhi.add_argument("--query-text", default="")
    query_abhi.add_argument("--passphrase-env", default="")

    # git-vocabulary alias: waggle grep == waggle query
    grep_abhi = subparsers.add_parser(
        "grep",
        help="Search an .abhi memory file with a query. (waggle grep)",
        description=(
            "Execute a saved or ad hoc query against an .abhi file and return matching nodes — "
            "like git grep but for your memory graph."
        ),
    )
    grep_abhi.add_argument("--input", dest="input_path", required=True)
    grep_abhi.add_argument("--query-id", default="")
    grep_abhi.add_argument("--query-text", default="")
    grep_abhi.add_argument("--passphrase-env", default="")

    load_abhi_chunks = subparsers.add_parser(
        "load-chunks",
        help="Load selected or query-relevant chunks from an .abhi memory file.",
    )
    load_abhi_chunks.add_argument("--input", dest="input_path", required=True)
    load_abhi_chunks.add_argument("--chunk-id", dest="chunk_ids", action="append", default=[])
    load_abhi_chunks.add_argument("--query-id", default="")
    load_abhi_chunks.add_argument("--query-text", default="")
    load_abhi_chunks.add_argument("--passphrase-env", default="")

    push_abhi = subparsers.add_parser(
        "push",
        help="Export the current graph to .abhi and upload it to Google Drive.",
    )
    push_abhi.add_argument("--drive", action="store_true")
    push_abhi.add_argument("--output", dest="output_path", default=None)
    push_abhi.add_argument("--project", default="")
    push_abhi.add_argument("--agent-id", default="")
    push_abhi.add_argument("--session-id", default="")
    push_abhi.add_argument("--scope", choices=["all", "project", "session", "since-date"], default="all")
    push_abhi.add_argument("--since-date", default="")
    push_abhi.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    push_abhi.add_argument("--encrypt", action=argparse.BooleanOptionalAction, default=True)
    push_abhi.add_argument("--passphrase-env", default="")
    push_abhi.add_argument(
        "--force",
        action="store_true",
        help="Export/upload even if transcript secret scan finds likely credentials or tokens.",
    )
    push_abhi.add_argument("--folder-id", default="")
    push_abhi.add_argument("--remote-name", default="")
    push_abhi.add_argument("--client-secret-path", default="~/.waggle/google-client-secret.json")
    push_abhi.add_argument("--token-path", default="")
    push_abhi.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)

    pull_abhi = subparsers.add_parser(
        "pull",
        help="Download a Google Drive .abhi file and merge it into the current graph.",
    )
    pull_abhi.add_argument("file_ref")
    pull_abhi.add_argument("--folder-id", default="")
    pull_abhi.add_argument("--download-path", default="")
    pull_abhi.add_argument("--merged-output", default="")
    pull_abhi.add_argument("--passphrase-env", default="")
    pull_abhi.add_argument(
        "--client-secret-path",
        default="~/.waggle/google-client-secret.json",
    )
    pull_abhi.add_argument("--token-path", default="")
    pull_abhi.add_argument(
        "--open-browser",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    pull_abhi.add_argument("--namespace", default="")
    pull_abhi.add_argument(
        "--merge-strategy",
        choices=[
            *ABHI_MERGE_STRATEGIES,
            "skip-existing",
            "overwrite",
            "branch",
        ],
        default=None,
        help=(
            "Conflict strategy used when merging local memory with the "
            "downloaded Drive document. Legacy import values are deprecated; "
            "use --import-strategy for those."
        ),
    )
    pull_abhi.add_argument(
        "--import-strategy",
        choices=["skip-existing", "overwrite", "branch"],
        default=None,
        help="Strategy used to import the merged document into the active graph.",
    )
    pull_abhi.add_argument("--verify-signature", action="store_true")
    pull_abhi.add_argument("--read-only", action="store_true")
    pull_abhi.add_argument("--reembed-on-mismatch", action="store_true")

    share_abhi = subparsers.add_parser(
        "share",
        help="Create an anyone-with-link share URL for a Google Drive file.",
    )
    share_abhi.add_argument("file_ref")
    share_abhi.add_argument("--folder-id", default="")
    share_abhi.add_argument("--client-secret-path", default="~/.waggle/google-client-secret.json")
    share_abhi.add_argument("--token-path", default="")
    share_abhi.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)

    export_context_bundle = subparsers.add_parser(
        "export-context-bundle",
        help="Export a markdown/json context package for another model or conversation.",
        description=(
            "Build a portable context bundle from the graph. "
            "Use mode=query for question-focused handoff, mode=prime for a fresh-session brief, "
            "and mode=graph for a broader graph export."
        ),
    )
    export_context_bundle.add_argument("--mode", choices=["prime", "query", "graph"], default="prime")
    export_context_bundle.add_argument("--query", default="")
    export_context_bundle.add_argument("--project", default="")
    export_context_bundle.add_argument("--agent-id", default="")
    export_context_bundle.add_argument("--session-id", default="")
    export_context_bundle.add_argument("--max-nodes", type=int, default=25)
    export_context_bundle.add_argument("--max-depth", type=int, default=2)
    export_context_bundle.add_argument("--retrieval-mode", choices=["graph", "verbatim", "hybrid"], default="graph")
    export_context_bundle.add_argument("--format", choices=["markdown", "json", "both"], default="both")
    export_context_bundle.add_argument("--output-path", default=None)
    export_context_bundle.add_argument("--include-edges", action=argparse.BooleanOptionalAction, default=True)
    export_context_bundle.add_argument("--include-timestamps", action=argparse.BooleanOptionalAction, default=True)
    export_context_bundle.add_argument("--include-source-prompt", action=argparse.BooleanOptionalAction, default=False)
    export_context_bundle.add_argument("--audience", choices=["llm", "human"], default="llm")

    export_markdown_vault = subparsers.add_parser(
        "export-markdown-vault",
        help="Export graph memory as an Obsidian-style markdown vault.",
    )
    export_markdown_vault.add_argument("--root-path", required=True)
    export_markdown_vault.add_argument("--project", default="")
    export_markdown_vault.add_argument("--agent-id", default="")
    export_markdown_vault.add_argument("--session-id", default="")

    import_markdown_vault = subparsers.add_parser(
        "import-markdown-vault",
        help="Import an edited markdown vault back into the graph.",
    )
    import_markdown_vault.add_argument("--root-path", required=True)

    backfill_windows = subparsers.add_parser(
        "backfill-windows",
        help="Retroactively assign legacy nodes to context windows grouped by project/session.",
    )
    backfill_windows.add_argument(
        "--dry-run",
        action="store_true",
        help="Show backfill stats without assigning nodes or creating windows.",
    )

    ingest_transcript_handoff = subparsers.add_parser(
        "ingest-transcript-handoff",
        help="Ingest a full session transcript as a rollover handoff, extract memory, export a context bundle, and emit a session checkpoint.",
        description=(
            "Client-triggered rollover handoff: pass the full ordered transcript as JSON, "
            "Waggle stores all messages as transcript provenance, extracts durable memory from "
            "logical user->assistant turns, exports a session-scoped context bundle, and emits a session-scoped .abhi checkpoint. "
            "Supported backend: SQLite only in v1. Neo4j support is deferred."
        ),
    )
    ingest_transcript_handoff.add_argument(
        "--input",
        default="-",
        metavar="PATH_OR_DASH",
        help="Path to the JSON transcript file, or '-' to read from stdin (default: stdin).",
    )
    ingest_transcript_handoff.add_argument("--project", default="", help="Scope override: project name.")
    ingest_transcript_handoff.add_argument("--agent-id", default="", help="Scope override: agent identifier.")
    ingest_transcript_handoff.add_argument("--session-id", default="", help="Scope override: session identifier.")
    ingest_transcript_handoff.add_argument("--output-path", default=None, help="Optional export output path prefix.")
    ingest_transcript_handoff.add_argument(
        "--export-format",
        choices=["markdown", "json", "both"],
        default="both",
        help="Export format for the post-ingestion context bundle (default: both).",
    )
    ingest_transcript_handoff.add_argument(
        "--max-nodes",
        type=int,
        default=25,
        help="Maximum nodes included in the exported context bundle (default: 25).",
    )
    ingest_transcript_handoff.add_argument(
        "--max-input-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="Hard input-size cap in bytes (default: 16 MiB). Oversized payloads fail with exit code 1.",
    )

    setup = subparsers.add_parser(
        "setup",
        help="Non-interactive one-line setup — auto-patch supported MCP clients.",
        description=(
            "Patch supported MCP client config files without prompts. "
            "By default, --clients auto updates detected clients; use --yes to run from install scripts."
        ),
    )
    setup.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactive setup. Required unless --dry-run is used.",
    )
    setup.add_argument(
        "--clients",
        default="auto",
        help=(
            "Comma-separated clients to configure, or 'auto'. "
            "Supported: codex, claude-desktop, cursor, gemini, antigravity, other."
        ),
    )
    setup.add_argument("--db", default="", help="Database path. Default: ~/.waggle/waggle.db")
    setup.add_argument(
        "--model",
        default="all-MiniLM-L6-v2",
        help="Embedding model for client env. Use 'deterministic' for offline-safe startup.",
    )
    setup.add_argument(
        "--project-instructions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write supported project instruction files, currently Codex AGENTS.md.",
    )
    setup.add_argument("--dry-run", action="store_true", help="Show what would change without writing files.")
    setup.add_argument("--run-doctor", action=argparse.BooleanOptionalAction, default=True)
    setup.add_argument(
        "--hooks",
        default="auto",
        help=(
            "Which hook-capable tools to install automatic memory hooks for: "
            "'auto' (all detected, currently Claude Code), 'none', or a comma-separated "
            "list (currently supported: claude-code)."
        ),
    )
    setup.add_argument(
        "--no-hooks",
        action="store_true",
        help="Deprecated alias for --hooks none. Skip all hook installation.",
    )

    subparsers.add_parser("init", help="Interactive setup wizard — configure an MCP client to use waggle-mcp.")
    subparsers.add_parser(
        "features",
        help="Explain the main tools, graph workflows, and how connected context reaches the model.",
        description="Print a detailed guide to the waggle-mcp feature surface.",
    )
    doctor = subparsers.add_parser(
        "doctor",
        help="Check your Waggle installation: config files, embedding model status, DB path, and common mistakes.",
        description=(
            "Inspect the waggle-mcp environment and surface any configuration issues before they become runtime errors. "
            "Checks: config file locations per MCP client, embedding model cache, DB path writability, "
            "WAGGLE_STARTUP_MODE, stdout encoding (Windows), and known API gotchas."
        ),
    )
    doctor.add_argument(
        "--fix", action="store_true", help="Re-embed stale transcript/node rows to the current model ID."
    )
    doctor.add_argument(
        "--json",
        "--as-json",
        dest="json_output",
        action="store_true",
        help="Print a machine-readable JSON report instead of the human-readable doctor output.",
    )

    demo_cmd = subparsers.add_parser(
        "demo",
        help="Run a 60-second local demo with a pre-loaded example graph. No MCP client required.",
        description=(
            "Import the bundled example graph (examples/demo.abhi) into a temporary SQLite DB, "
            "run 4 scripted queries, and print results. Uses WAGGLE_MODEL=deterministic by default "
            "for instant startup. Pass --with-embeddings to use the real sentence-transformers model."
        ),
    )
    demo_cmd.add_argument(
        "--with-embeddings",
        action="store_true",
        help="Use the real sentence-transformers model instead of deterministic mode.",
    )

    subparsers.add_parser(
        "uninstall-hooks",
        help="Remove the Waggle managed hooks block from Claude Code settings.",
        description="Idempotent: removes the waggle-managed block from ~/.claude/settings.json if present.",
    )

    return parser


def _normalize_pull_strategy_args(
    args: argparse.Namespace,
) -> argparse.Namespace:
    """Normalize current and legacy strategy arguments for Drive pull."""
    if getattr(args, "command", "") != "pull":
        return args

    raw_merge_strategy = getattr(args, "merge_strategy", None)
    explicit_import_strategy = getattr(args, "import_strategy", None)

    if raw_merge_strategy in _ABHI_IMPORT_STRATEGIES:
        if explicit_import_strategy is None:
            args.import_strategy = raw_merge_strategy
            warning_message = (
                f"`--merge-strategy {raw_merge_strategy}` is deprecated for "
                "selecting the graph import strategy. Use "
                f"`--import-strategy {raw_merge_strategy}` instead."
            )
        else:
            warning_message = (
                f"Legacy `--merge-strategy {raw_merge_strategy}` is deprecated "
                "and has been ignored because `--import-strategy` was also "
                "supplied."
            )

        args.merge_strategy = DEFAULT_ABHI_MERGE_STRATEGY
        warnings.warn(
            warning_message,
            FutureWarning,
            stacklevel=2,
        )
    else:
        args.merge_strategy = raw_merge_strategy or DEFAULT_ABHI_MERGE_STRATEGY
        if explicit_import_strategy is None:
            args.import_strategy = "skip-existing"

    return args


def _run_admin_command(config: AppConfig, args: argparse.Namespace) -> int:
    # Normalise git-vocabulary CLI aliases to their canonical command names.
    _CLI_COMMAND_ALIASES: dict[str, str] = {
        "commit": "export",
        "fsck": "validate",
        "show": "inspect",
        "grep": "query",
    }
    if args.command in _CLI_COMMAND_ALIASES:
        args.command = _CLI_COMMAND_ALIASES[args.command]
    if args.command == "doctor":
        return _run_doctor_command(config, args)
    backend = _build_backend(config)
    if args.command == "create-tenant":
        tenant = backend.ensure_tenant(args.tenant_id, args.name)
        print(json.dumps(tenant.model_dump(), indent=2))
        return 0
    if args.command == "create-api-key":
        expires_in_days = int(getattr(args, "expires_in_days", 0) or 0)
        expires_at = utc_now() + timedelta(days=expires_in_days) if expires_in_days > 0 else None
        tenant_backend = backend.for_tenant(args.tenant_id)
        created = tenant_backend.create_api_key(
            args.tenant_id,
            args.name,
            expires_at=expires_at,
            created_by=str(getattr(args, "created_by", "") or "").strip(),
            scopes=_parse_api_key_scopes(getattr(args, "scopes", "")) or None,
        )
        tenant_backend.emit_audit_event(
            event_type="api_key.created",
            actor_type="admin",
            actor_id=str(getattr(args, "created_by", "") or "").strip() or "local-cli",
            resource_type="api_key",
            resource_id=created.record.api_key_id,
            action="create",
            metadata={
                "name": created.record.name,
                "prefix": created.record.prefix,
                "expires_at": created.record.expires_at.isoformat() if created.record.expires_at else None,
                "scopes": created.record.scopes,
            },
        )
        print(
            json.dumps(
                {
                    "api_key_id": created.record.api_key_id,
                    "tenant_id": created.record.tenant_id,
                    "prefix": created.record.prefix,
                    "name": created.record.name,
                    "status": created.record.status,
                    "expires_at": created.record.expires_at.isoformat() if created.record.expires_at else None,
                    "created_by": created.record.created_by,
                    "scopes": created.record.scopes,
                    "raw_api_key": created.raw_api_key,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "list-api-keys":
        print(
            json.dumps(
                [_serialize_api_key_record(record) for record in backend.list_api_keys(args.tenant_id)], indent=2
            )
        )
        return 0
    if args.command == "revoke-api-key":
        tenant_backend = backend.for_tenant(getattr(args, "tenant_id", "") or config.default_tenant_id)
        tenant_backend.revoke_api_key(args.api_key_id)
        tenant_backend.emit_audit_event(
            event_type="api_key.revoked",
            actor_type="admin",
            actor_id="local-cli",
            resource_type="api_key",
            resource_id=args.api_key_id,
            action="revoke",
        )
        print(json.dumps({"revoked": args.api_key_id}))
        return 0
    if args.command in {
        "retention-status",
        "set-retention",
        "prune-retention",
        "list-retention-runs",
        "list-audit-events",
    }:
        retention_backend = backend.for_tenant(getattr(args, "tenant_id", "") or config.default_tenant_id)
        policy_kwargs = {
            "default_enabled": config.retention_enabled,
            "default_retention_days": config.retention_days,
            "default_prune_interval_hours": config.retention_prune_interval_hours,
        }
        if args.command == "retention-status":
            policy = retention_backend.get_retention_policy(**policy_kwargs)
            payload = _serialize_retention_policy(policy)
            payload["recent_runs"] = [
                _serialize_retention_run(run) for run in retention_backend.list_retention_runs(limit=5)
            ]
            print(json.dumps(payload, indent=2))
            return 0
        if args.command == "set-retention":
            policy = retention_backend.update_retention_policy(
                enabled=getattr(args, "enabled", None),
                retention_days=getattr(args, "days", None),
                prune_interval_hours=getattr(args, "interval_hours", None),
                **policy_kwargs,
            )
            retention_backend.emit_audit_event(
                event_type="retention.policy.updated",
                actor_type="admin",
                actor_id="local-cli",
                resource_type="retention_policy",
                resource_id=policy.tenant_id,
                action="update",
                metadata={
                    "enabled": policy.enabled,
                    "retention_days": policy.retention_days,
                    "prune_interval_hours": policy.prune_interval_hours,
                },
            )
            print(json.dumps(_serialize_retention_policy(policy), indent=2))
            return 0
        if args.command == "prune-retention":
            run = retention_backend.prune_retention(
                batch_size=getattr(args, "batch_size", 1000),
                **policy_kwargs,
            )
            payload = _serialize_retention_run(run)
            payload["policy"] = _serialize_retention_policy(retention_backend.get_retention_policy(**policy_kwargs))
            print(json.dumps(payload, indent=2))
            return 0
        if args.command == "list-audit-events":
            events = retention_backend.list_audit_events(
                limit=getattr(args, "limit", 100),
                event_type=getattr(args, "event_type", ""),
                actor_id=getattr(args, "actor_id", ""),
                resource_id=getattr(args, "resource_id", ""),
                resource_type=getattr(args, "resource_type", ""),
                status=getattr(args, "status", ""),
            )
            print(json.dumps([_serialize_audit_event(event) for event in events], indent=2))
            return 0
        runs = retention_backend.list_retention_runs(limit=getattr(args, "limit", 20))
        print(json.dumps([_serialize_retention_run(run) for run in runs], indent=2))
        return 0
    if args.command == "migrate-sqlite":
        if config.backend != "neo4j":
            raise ValidationFailure("migrate-sqlite requires WAGGLE_BACKEND=neo4j for the target environment.")
        source = MemoryGraph(
            args.db_path,
            EmbeddingModel(
                config.model_name,
                embedding_backend=config.embedding_backend,
            ),
            tenant_id=args.tenant_id,
            export_dir=config.export_dir,
        )
        target = backend.for_tenant(args.tenant_id)
        with secure_temp_path(suffix=".json") as temp_path:
            backup = source.export_graph_backup(output_path=temp_path)
            imported = target.import_graph_backup(input_path=temp_path)
            print(
                json.dumps(
                    {
                        "backup": backup.model_dump(),
                        "import": imported.model_dump(),
                    },
                    indent=2,
                )
            )
        return 0
    if args.command == "export":
        _assert_export_safe(
            backend,
            force=bool(getattr(args, "force", False)),
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
        )
        exported = backend.export_abhi(
            output_path=args.output_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
            include_embeddings=bool(getattr(args, "include_embeddings", True)),
            passphrase=_resolve_passphrase(args),
            redact_patterns=list(getattr(args, "redact_patterns", []) or []),
            sign=bool(getattr(args, "sign", False)),
            signing_key_dir=getattr(args, "signing_key_dir", "~/.waggle/keys"),
        )
        print(json.dumps(exported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "checkpoint-context":
        scope = getattr(args, "scope", "") or ""
        if not scope:
            if getattr(args, "session_id", ""):
                scope = "session"
            elif getattr(args, "project", ""):
                scope = "project"
            elif getattr(args, "since_date", ""):
                scope = "since-date"
            else:
                scope = "all"
        _assert_export_safe(
            backend,
            force=bool(getattr(args, "force", False)),
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=scope,
            since_date=getattr(args, "since_date", ""),
        )
        exported = backend.export_abhi(
            output_path=args.output_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=scope,
            since_date=getattr(args, "since_date", ""),
            include_embeddings=bool(getattr(args, "include_embeddings", True)),
            passphrase=_resolve_passphrase(args),
            redact_patterns=list(getattr(args, "redact_patterns", []) or []),
            sign=bool(getattr(args, "sign", False)),
            signing_key_dir=getattr(args, "signing_key_dir", "~/.waggle/keys"),
        )
        payload = exported.model_dump(mode="json")
        payload["checkpoint_scope"] = scope
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "clear-session":
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not bool(getattr(args, "yes", False)):
            raise ValidationFailure("clear-session is destructive and requires --yes.")
        cleared = backend.clear_session(session_id=args.session_id, dry_run=dry_run)
        print(json.dumps(cleared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "clear-project":
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not bool(getattr(args, "yes", False)):
            raise ValidationFailure("clear-project is destructive and requires --yes.")
        cleared = backend.clear_project(project=args.project, dry_run=dry_run)
        print(json.dumps(cleared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "clear-all":
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not bool(getattr(args, "yes", False)):
            raise ValidationFailure("clear-all is destructive and requires --yes.")
        cleared = backend.clear_all(dry_run=dry_run)
        print(json.dumps(cleared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "import":
        input_path = getattr(args, "input_path", None) or getattr(args, "input_path_flag", "")
        imported = backend.import_abhi(
            input_path=input_path,
            passphrase=_resolve_passphrase(args),
            namespace=getattr(args, "namespace", ""),
            merge_strategy=getattr(args, "merge_strategy", "skip-existing"),
            verify_signature=bool(getattr(args, "verify_signature", False)),
            read_only=bool(getattr(args, "read_only", False)),
            reembed_on_mismatch=bool(getattr(args, "reembed_on_mismatch", False)),
        )
        print(json.dumps(imported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "validate":
        validation = backend.validate_abhi(input_path=args.input_path, passphrase=_resolve_passphrase(args))
        print(json.dumps(validation.model_dump(mode="json"), indent=2))
        return 0 if validation.valid else 1
    if args.command == "inspect":
        inspected = backend.inspect_abhi(input_path=args.input_path, passphrase=_resolve_passphrase(args))
        print(json.dumps(inspected.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "diff":
        input_path_a = getattr(args, "input_path_a", None) or getattr(args, "input_path_a_flag", "")
        input_path_b = getattr(args, "input_path_b", None) or getattr(args, "input_path_b_flag", "")
        diff = backend.diff_abhi(input_path_a=input_path_a, input_path_b=input_path_b)
        print(json.dumps(diff.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "merge":
        left_input_path = getattr(args, "left_input_path_flag", "") or args.left_input_path
        right_input_path = getattr(args, "right_input_path_flag", "") or args.right_input_path
        merged = backend.merge_abhi(
            base_input_path=args.base_input_path or left_input_path,
            left_input_path=left_input_path,
            right_input_path=right_input_path,
            output_path=args.output_path,
            merge_strategy=args.merge_strategy,
        )
        print(json.dumps(merged.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "query":
        queried = backend.query_abhi(
            input_path=args.input_path,
            query_id=getattr(args, "query_id", ""),
            query_text=getattr(args, "query_text", ""),
            passphrase=_resolve_passphrase(args),
        )
        print(json.dumps(queried.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "load-chunks":
        loaded = backend.load_abhi_chunks(
            input_path=args.input_path,
            chunk_ids=getattr(args, "chunk_ids", []),
            query_id=getattr(args, "query_id", ""),
            query_text=getattr(args, "query_text", ""),
            passphrase=_resolve_passphrase(args),
        )
        print(json.dumps(loaded.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "push":
        _require_drive_sync()
        passphrase = _resolve_passphrase(args)
        _assert_export_safe(
            backend,
            force=bool(getattr(args, "force", False)),
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
        )
        exported = backend.export_abhi(
            output_path=args.output_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
            include_embeddings=bool(getattr(args, "include_embeddings", True)),
            passphrase=passphrase,
        )
        credentials = ensure_drive_credentials(
            client_secret_path=args.client_secret_path,
            token_path=_resolve_drive_token_path(args, config),
            open_browser=bool(getattr(args, "open_browser", True)),
        )
        pushed = push_file_to_drive(
            local_path=exported.output_path,
            folder_id=str(getattr(args, "folder_id", "") or ""),
            credentials=credentials,
            remote_name=str(getattr(args, "remote_name", "") or ""),
            encrypted=bool(passphrase),
        )
        print(json.dumps(pushed.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "pull":
        _require_drive_sync()
        passphrase = _resolve_passphrase(args)
        credentials = ensure_drive_credentials(
            client_secret_path=args.client_secret_path,
            token_path=_resolve_drive_token_path(args, config),
            open_browser=bool(getattr(args, "open_browser", True)),
        )
        remote_file_id, remote_name = resolve_drive_file_id(
            file_ref=args.file_ref,
            credentials=credentials,
            folder_id=str(getattr(args, "folder_id", "") or ""),
        )
        download_path = Path(getattr(args, "download_path", "") or backend.export_dir / f"{remote_file_id}.abhi")
        _, resolved_name = download_drive_file(
            file_id=remote_file_id,
            destination_path=download_path,
            credentials=credentials,
        )
        local_document = build_abhi_document(backend.get_graph_snapshot())
        remote_document = load_abhi_document(download_path, passphrase=passphrase)
        merged_output = Path(getattr(args, "merged_output", "") or backend.export_dir / f"merged-{remote_file_id}.abhi")
        selected_merge_strategy = getattr(args, "merge_strategy", None) or DEFAULT_ABHI_MERGE_STRATEGY
        selected_import_strategy = getattr(args, "import_strategy", None) or "skip-existing"

        merged_path = merge_downloaded_abhi(
            local_document=local_document,
            remote_document=remote_document,
            output_path=merged_output,
            merge_strategy=selected_merge_strategy,
        )

        imported = backend.import_abhi(
            input_path=merged_path,
            passphrase=passphrase,
            namespace=getattr(args, "namespace", ""),
            merge_strategy=selected_import_strategy,
            verify_signature=bool(getattr(args, "verify_signature", False)),
            read_only=bool(getattr(args, "read_only", False)),
            reembed_on_mismatch=bool(getattr(args, "reembed_on_mismatch", False)),
        )
        result = DrivePullResult(
            remote_file_id=remote_file_id,
            remote_name=resolved_name or remote_name,
            downloaded_path=str(download_path),
            merged_output_path=merged_path,
            merge_strategy=selected_merge_strategy,
            nodes_created=imported.nodes_created,
            nodes_updated=imported.nodes_updated,
            edges_created=imported.edges_created,
            edges_updated=imported.edges_updated,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "share":
        _require_drive_sync()
        credentials = ensure_drive_credentials(
            client_secret_path=args.client_secret_path,
            token_path=_resolve_drive_token_path(args, config),
            open_browser=bool(getattr(args, "open_browser", True)),
        )
        remote_file_id, _ = resolve_drive_file_id(
            file_ref=args.file_ref,
            credentials=credentials,
            folder_id=str(getattr(args, "folder_id", "") or ""),
        )
        shared = share_drive_file(file_id=remote_file_id, credentials=credentials)
        print(json.dumps(shared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "export-context-bundle":
        exported = backend.export_context_bundle(
            mode=args.mode,
            query=args.query,
            project=args.project,
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            max_nodes=args.max_nodes,
            max_depth=args.max_depth,
            retrieval_mode=getattr(args, "retrieval_mode", "graph"),
            format=args.format,
            output_path=args.output_path,
            include_edges=args.include_edges,
            include_timestamps=args.include_timestamps,
            include_source_prompt=args.include_source_prompt,
            audience=args.audience,
        )
        print(json.dumps(exported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "export-markdown-vault":
        exported = backend.export_markdown_vault(
            root_path=args.root_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
        )
        print(json.dumps(exported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "import-markdown-vault":
        imported = backend.import_markdown_vault(root_path=args.root_path)
        print(json.dumps(imported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "backfill-windows":
        from waggle.backfill import backfill_context_windows

        if not isinstance(backend, MemoryGraph):
            raise ValidationFailure("backfill-windows is currently supported only for the SQLite backend.")
        stats = backfill_context_windows(backend, dry_run=bool(args.dry_run))
        print(json.dumps(stats.model_dump(mode="json"), indent=2))
        return 1 if stats.errors else 0
    if args.command == "ingest-transcript-handoff":
        return _run_ingest_transcript_handoff(config, args)
    if args.command == "features":
        print(_FEATURES_GUIDE)
        return 0
    if args.command == "doctor":
        return _run_doctor_command(config, args)
    raise ValidationFailure(f"Unknown command: {args.command}")


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------


def _run_doctor_command(config: AppConfig, args: argparse.Namespace) -> int:
    return _run_doctor(
        config,
        fix=bool(getattr(args, "fix", False)),
        json_output=bool(getattr(args, "json_output", False)),
    )


_KNOWN_CONFIG_PATHS: list[tuple[str, str]] = [
    # (label, path_template)
    # %APPDATA% and ~ are expanded at runtime
    ("Claude Desktop (macOS/Linux)", "~/.config/claude/claude_desktop_config.json"),
    ("Claude Desktop (macOS alt)", "~/Library/Application Support/Claude/claude_desktop_config.json"),
    ("Claude Desktop (Windows)", "%APPDATA%\\Claude\\claude_desktop_config.json"),
    ("Cursor (macOS/Linux)", "~/.cursor/mcp.json"),
    ("Cursor (Windows)", "%APPDATA%\\Cursor\\User\\mcp.json"),
    ("Antigravity AI agent (macOS/Linux)", "~/.gemini/antigravity/mcp_config.json"),
    ("Antigravity AI agent (Windows)", "%USERPROFILE%\\.gemini\\antigravity\\mcp_config.json"),
    ("VS Code extension (Windows)", "%APPDATA%\\Antigravity\\User\\mcp.json"),
    ("Codex", "~/.codex/config.toml"),
]

_DOCTOR_KNOWN_GOTCHAS = """\
Known API gotchas (from the Waggle field error log):
  • observe_conversation requires fields 'user_message' and 'assistant_response'.
    Do NOT use 'user_text' / 'assistant_text' — those will be rejected.
  • get_topics does not support scope filtering. Passing 'project' etc. used to
    cause "additional properties" errors. It now accepts (and ignores) scope fields.
  • Use the official 'mcp' Python package for stdio clients, not hand-rolled JSON-RPC.
    Install: pip install mcp
    Use:     from mcp import ClientSession, StdioServerParameters
             from mcp.client.stdio import stdio_client
  • On Windows, stdio framing requires the official mcp client; raw subprocess I/O
    with manual \\n line endings will produce garbled or dropped messages.
  • Set WAGGLE_MODEL=deterministic for offline/offline-first environments.
    The default (all-MiniLM-L6-v2) downloads ~420 MB on first run and will
    block store_node indefinitely if no network is available.
"""


def _run_doctor(config: AppConfig, *, fix: bool = False, json_output: bool = False) -> int:
    """waggle-mcp doctor — surface configuration and environment issues."""
    issues: list[str] = []
    warnings: list[str] = []
    ok_items: list[str] = []
    checks: dict[str, dict[str, Any]] = {}

    def emit(*args: object, **kwargs: object) -> None:
        if not json_output:
            print(*args, **kwargs)

    def ok(message: str) -> None:
        if not json_output:
            _ok(message)

    def fail(message: str) -> None:
        if not json_output:
            _fail(message)

    emit()
    emit(_c(_BOLD, "waggle-mcp doctor"))
    emit(_c(_CYAN, "─" * 50))

    # ── 1. Config file locations ─────────────────────────────────────────────
    emit(_c(_BOLD, "\n[1] MCP client config files"))
    waggle_found_in: list[str] = []
    for label, template in _KNOWN_CONFIG_PATHS:
        raw = template.replace("%APPDATA%", os.environ.get("APPDATA", "")).replace("%USERPROFILE%", str(Path.home()))
        path = Path(raw).expanduser()
        exists = path.exists()
        has_waggle = False
        if exists:
            try:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
                if path.suffix == ".toml":
                    has_waggle = "[mcp_servers.waggle]" in raw_text
                else:
                    data = json.loads(raw_text)
                    servers = data.get("mcpServers", data.get("tools", {}) if isinstance(data, dict) else {})
                    has_waggle = isinstance(servers, dict) and "waggle" in servers
                if has_waggle:
                    waggle_found_in.append(label)
                    ok(f"{label}\n     {path}  [waggle entry found]")
                else:
                    emit(f"  {_c(_CYAN, chr(0x2022))} {label}\n     {path}  [exists, no waggle entry]")
            except Exception:
                emit(f"  {_c(_CYAN, chr(0x2022))} {label}\n     {path}  [exists, could not parse]")
        # only show missing paths that are plausible for this platform
        elif (
            (
                sys.platform == "darwin"
                and ("macOS" in label or "Cursor" in label or "Antigravity" in label or "Codex" in label)
            )
            or (sys.platform == "win32" and "Windows" in label)
            or (sys.platform.startswith("linux") and ("Linux" in label or "Cursor" in label))
        ):
            emit(f"  {_c(_CYAN, chr(0x2022))} {label}\n     {path}  [not found]")

    if not waggle_found_in:
        issues.append(
            "No MCP client config file contains a 'waggle' server entry. "
            "Run 'waggle-mcp setup --yes' to create one, or add it manually."
        )
        checks["mcp_config"] = {
            "status": "fail",
            "reason": "No MCP client config file contains a 'waggle' server entry.",
        }
    else:
        ok_items.append(f"Waggle found in: {', '.join(waggle_found_in)}")
        checks["mcp_config"] = {"status": "ok", "found_in": waggle_found_in}

    # ── 2. DB path ───────────────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[2] Database path"))
    db_path = Path(config.db_path)
    db_dir = db_path.parent
    db_exists = db_path.exists()
    db_dir_exists = db_dir.exists()
    if db_exists:
        ok(f"DB file exists: {db_path}")
        ok_items.append("DB file found")
        checks["db_connection"] = {"status": "ok", "path": str(db_path)}
    elif db_dir_exists:
        ok(f"DB directory exists (file will be created on first run): {db_path}")
        ok_items.append("DB directory writable")
        checks["db_connection"] = {"status": "ok", "path": str(db_path)}
    else:
        issues.append(f"DB directory does not exist: {db_dir}. Create it with: mkdir -p <dir>")
        fail(f"DB directory missing: {db_dir}")
        checks["db_connection"] = {
            "status": "fail",
            "reason": f"DB directory does not exist: {db_dir}. Create it with: mkdir -p <dir>",
        }

    # ── 3. Embedding model ───────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[3] Embedding model"))
    model_name = config.model_name
    is_deterministic = model_name.strip().lower() in {"fake", "fake-model", "deterministic", "offline-demo"}
    is_cached = False
    hf_home = (
        os.environ.get("HF_HOME")
        or os.environ.get("SENTENCE_TRANSFORMERS_HOME")
        or str(Path.home() / ".cache" / "huggingface")
    )
    st_cache = Path(os.environ.get("SENTENCE_TRANSFORMERS_HOME", Path(hf_home) / "hub"))

    if is_deterministic:
        ok(f"Model: {model_name!r}  (deterministic — no download, always offline-safe)")
        ok_items.append("Deterministic model — no download needed")
        checks["embedding_model"] = {"status": "ok", "model_id": model_name}
    else:
        # Heuristic: look for a cached sentence-transformers directory
        safe_name = model_name.replace("/", "_").replace("\\", "_")
        possible_dirs = [
            st_cache / f"models--{safe_name.replace('_', '--', 1)}",
            st_cache / safe_name,
            Path(hf_home) / "hub" / f"models--{safe_name.replace('_', '--', 1)}",
        ]
        is_cached = any(p.exists() for p in possible_dirs)
        if is_cached:
            ok(f"Model: {model_name!r}  (cached locally — fast startup)")
            ok_items.append("Embedding model cached")
            checks["embedding_model"] = {"status": "ok", "model_id": model_name}
        else:
            emit(
                f"  {_c(_CYAN, chr(0x2139))} Model: {model_name!r}  — NOT found in local cache.\n"
                f"    First store_node/query_graph call will download ~420 MB from HuggingFace.\n"
                f"    To avoid: set WAGGLE_MODEL=deterministic, or pre-download with:\n"
                f"      python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('{model_name}')\""
            )
            reason = (
                f"Embedding model '{model_name}' not found in cache. "
                "First semantic call will block for a network download. "
                "Set WAGGLE_MODEL=deterministic for offline-safe mode."
            )
            warnings.append(reason)
            checks["embedding_model"] = {"status": "warn", "model_id": model_name, "reason": reason}

    # ── 4. Embedding store ───────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[4] Embedding store"))
    checks["graph_schema"] = {"status": "ok"}
    try:
        graph = _default_graph(config)
        if isinstance(graph, MemoryGraph):
            store_health = graph.get_embedding_store_health()
            if fix and (store_health["transcript_stale_rows"] or store_health["node_stale_rows"]):
                repair = graph.reembed_stale_embeddings(batch_size=100)
                ok(
                    "Re-embedded stale rows: "
                    f"{repair['transcript_rows_updated']} transcript rows, {repair['node_rows_updated']} node rows."
                )
                ok_items.append("Stale embeddings re-embedded")
                store_health = graph.get_embedding_store_health()

            checksum_failures = (
                store_health["node_checksum_failures"]
                + store_health["transcript_checksum_failures"]
                + store_health["window_checksum_failures"]
            )
            if fix and checksum_failures:
                cleared = graph.clear_corrupt_embeddings()
                rebuilt = graph.reembed_stale_embeddings(batch_size=100)
                windows_rebuilt = graph.recompute_stale_window_embeddings()
                ok(
                    "Cleared corrupt embeddings: "
                    f"{cleared['transcript_records']} transcript, {cleared['nodes']} node, "
                    f"{cleared['context_windows']} window rows; rebuilt "
                    f"{rebuilt['transcript_rows_updated']} transcript, {rebuilt['node_rows_updated']} node and "
                    f"{windows_rebuilt} window embeddings."
                )
                ok_items.append("Corrupt embeddings cleared and rebuilt")
                store_health = graph.get_embedding_store_health()
                checksum_failures = (
                    store_health["node_checksum_failures"]
                    + store_health["transcript_checksum_failures"]
                    + store_health["window_checksum_failures"]
                )
            if fix and store_health["window_stale_rows"]:
                # Repair stale window embeddings even when nothing failed its checksum
                # (membership changes flag windows stale without corrupting anything);
                # the corrupt-clear path above only runs on failures.
                windows_rebuilt = graph.recompute_stale_window_embeddings()
                ok(f"Recomputed stale context-window embeddings: {windows_rebuilt} window rows.")
                if windows_rebuilt:
                    ok_items.append("Stale context-window embeddings recomputed")
                store_health = graph.get_embedding_store_health()
                checksum_failures = (
                    store_health["node_checksum_failures"]
                    + store_health["transcript_checksum_failures"]
                    + store_health["window_checksum_failures"]
                )

            legacy_rows = (
                store_health["node_legacy_rows"]
                + store_health["transcript_legacy_rows"]
                + store_health["window_legacy_rows"]
            )
            transcript_models = store_health["transcript_model_counts"]
            node_models = store_health["node_model_counts"]
            emit(f"  Current model id: {store_health['current_model_id']}")
            emit(f"  Transcript model ids: {transcript_models or '{}'}")
            emit(f"  Node model ids: {node_models or '{}'}")
            emit(f"  Stale transcript rows: {store_health['transcript_stale_rows']}")
            emit(f"  Stale node rows: {store_health['node_stale_rows']}")

            store_status = "ok"
            store_reason: str | None = None
            if store_health["mixed_models"]:
                issues.append("Mixed embedding_model_id values detected in the store.")
                fail("Mixed embedding model IDs detected across transcript_records/nodes.")
                store_status = "fail"
                store_reason = "Mixed embedding_model_id values detected across transcript_records/nodes."
            else:
                ok("Store model IDs are consistent.")
                ok_items.append("Embedding store model IDs consistent")

            if checksum_failures:
                issues.append(f"{checksum_failures} embedding row(s) fail the integrity checksum (corruption).")
                fail(
                    "Embedding checksum failures: "
                    f"{store_health['transcript_checksum_failures']} transcript rows, "
                    f"{store_health['node_checksum_failures']} node rows, "
                    f"{store_health['window_checksum_failures']} window rows. "
                    "Run 'waggle-mcp doctor --fix' to clear and rebuild them."
                )
                store_status = "fail"
                if store_reason is None:
                    store_reason = f"{checksum_failures} embedding row(s) fail the integrity checksum (corruption)."
            elif legacy_rows:
                emit("  No checksum failures in checksummed rows; legacy rows cannot be checksum-verified yet.")
                warnings.append(
                    f"{legacy_rows} embedding row(s) use the legacy pre-checksum format "
                    "and cannot be checksum-verified until rewritten."
                )
                if store_status == "ok":
                    store_status = "warn"
                    store_reason = (
                        f"{legacy_rows} embedding row(s) use the legacy pre-checksum format "
                        "and cannot be checksum-verified until rewritten."
                    )
            else:
                ok("All stored embeddings pass the integrity checksum.")
                ok_items.append("Embedding checksums verified")
            if legacy_rows:
                emit(
                    "  Pre-checksum (legacy) rows: "
                    f"{store_health['transcript_legacy_rows']} transcript, {store_health['node_legacy_rows']} node, "
                    f"{store_health['window_legacy_rows']} window (upgraded automatically on next write)."
                )

            graph_schema_check: dict[str, Any] = {
                "status": store_status,
                "current_model_id": store_health["current_model_id"],
                "transcript_model_counts": transcript_models,
                "node_model_counts": node_models,
                "transcript_stale_rows": store_health["transcript_stale_rows"],
                "node_stale_rows": store_health["node_stale_rows"],
                "window_stale_rows": store_health["window_stale_rows"],
                "mixed_models": store_health["mixed_models"],
                "checksum_failures": checksum_failures,
                "transcript_checksum_failures": store_health["transcript_checksum_failures"],
                "node_checksum_failures": store_health["node_checksum_failures"],
                "window_checksum_failures": store_health["window_checksum_failures"],
                "legacy_rows": legacy_rows,
            }
            if store_reason is not None:
                graph_schema_check["reason"] = store_reason
            checks["graph_schema"] = graph_schema_check
    except Exception as exc:
        message = f"Embedding store check failed: {type(exc).__name__}: {exc}"
        issues.append(message)
        fail(message)
        checks["graph_schema"] = {"status": "fail", "reason": message}

    # ── 5. WAGGLE_STARTUP_MODE ───────────────────────────────────────────────
    emit(_c(_BOLD, "\n[5] Startup mode"))
    emit(f"  WAGGLE_STARTUP_MODE = {config.startup_mode!r}")
    if config.is_fast_mode:
        ok("fast mode: zero ML overhead. Schema/tool listing only. Semantic tools return 'unavailable'.")
        ok_items.append("Startup mode: fast")
        checks["startup_mode"] = {"status": "ok", "mode": "fast"}
    elif config.is_strict_mode:
        ok("strict mode: server blocks on startup until embedding model is ready.")
        ok_items.append("Startup mode: strict")
        checks["startup_mode"] = {"status": "ok", "mode": "strict"}
    else:
        ok("normal mode: embedding loads in background. First semantic call may wait up to ~30 s.")
        ok_items.append("Startup mode: normal")
        checks["startup_mode"] = {"status": "ok", "mode": "normal"}

    # ── 6. Windows stdout encoding ───────────────────────────────────────────
    if sys.platform == "win32":
        emit(_c(_BOLD, "\n[6] Windows stdout encoding"))
        enc = getattr(sys.stdout, "encoding", "unknown")
        is_utf8 = enc.lower().replace("-", "") in ("utf8", "utf8")
        if is_utf8:
            ok(f"stdout encoding: {enc}")
            ok_items.append("Windows stdout is UTF-8")
            checks["stdout_encoding"] = {"status": "ok", "encoding": enc}
        else:
            fail(
                f"stdout encoding is {enc!r} (not UTF-8). "
                "Unicode characters (emoji, accented text) will cause UnicodeEncodeError.\n"
                "    Fix: run with 'python -X utf8' or add at script top:\n"
                "      import sys; sys.stdout.reconfigure(encoding='utf-8')"
            )
            reason = f"Windows stdout encoding is {enc!r} — set PYTHONUTF8=1 or use python -X utf8."
            issues.append(reason)
            checks["stdout_encoding"] = {"status": "fail", "encoding": enc, "reason": reason}
    else:
        checks["stdout_encoding"] = {"status": "ok"}

    # ── 7. Known gotchas ─────────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[7] Known API gotchas"))
    emit(_DOCTOR_KNOWN_GOTCHAS)

    # ── Summary ──────────────────────────────────────────────────────────────
    if json_output:
        summary = {"ok": 0, "warn": 0, "fail": 0}
        for check in checks.values():
            summary[check["status"]] += 1
        result = {
            "version": __version__,
            "checks": checks,
            "summary": summary,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if summary["fail"] else 0

    print(_c(_BOLD, "─" * 50))
    if issues:
        print(_c(_RED, f"Found {len(issues)} issue(s):"))
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        if warnings:
            print()
            print(_c(_CYAN, f"Warnings ({len(warnings)}):"))
            for i, warning in enumerate(warnings, 1):
                print(f"  {i}. {warning}")
        print()
        return 1
    if warnings:
        print(_c(_CYAN, f"Warnings ({len(warnings)}):"))
        for i, warning in enumerate(warnings, 1):
            print(f"  {i}. {warning}")
        print()
        return 0
    else:
        print(_c(_GREEN, f"All checks passed ({len(ok_items)} OK). Waggle looks healthy."))
        print()
        return 0


# ---------------------------------------------------------------------------
# ingest-transcript-handoff CLI helpers
# ---------------------------------------------------------------------------


def _emit_cli_error(code: str, message: str, details: dict[str, Any]) -> None:
    """Write a structured failure JSON object to stderr."""
    payload = {"code": code, "message": message, "details": details}
    sys.stderr.write(json.dumps(payload) + "\n")
    sys.stderr.flush()


def _run_ingest_transcript_handoff(config: AppConfig, args: argparse.Namespace) -> int:
    """Execute the ingest-transcript-handoff CLI command.

    Exit codes:
      0  — success
      1  — input or validation failure
      2  — backend / graph / export failure
      3  — unexpected internal error
    """
    max_bytes: int = args.max_input_bytes

    # ── Load raw input ──────────────────────────────────────────────────────
    try:
        input_arg: str = args.input
        if input_arg == "-":
            raw = sys.stdin.buffer.read(max_bytes + 1)
        else:
            path = Path(input_arg)
            if not path.exists():
                _emit_cli_error("input_not_found", f"Input file not found: {input_arg}", {})
                return 1
            raw = path.read_bytes()[: max_bytes + 1]
    except OSError as exc:
        _emit_cli_error("input_read_error", str(exc), {})
        return 1

    if len(raw) > max_bytes:
        _emit_cli_error(
            "payload_too_large",
            f"Input exceeds --max-input-bytes ({max_bytes} bytes).",
            {"max_input_bytes": max_bytes},
        )
        return 1

    # ── Parse JSON ──────────────────────────────────────────────────────────
    try:
        payload_dict = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        _emit_cli_error("malformed_json", f"JSON parse error: {exc}", {})
        return 1

    if not isinstance(payload_dict, dict):
        _emit_cli_error("invalid_format", "Input must be a JSON object.", {})
        return 1

    if "messages" not in payload_dict:
        _emit_cli_error("missing_field", "Input JSON must contain a 'messages' key.", {})
        return 1

    # ── Validate via Pydantic ───────────────────────────────────────────────
    try:
        payload = TranscriptIngestionInput.model_validate(payload_dict)
    except Exception as exc:
        _emit_cli_error("validation_error", str(exc), {})
        return 1

    # ── CLI scope flags override JSON fields ────────────────────────────────
    if getattr(args, "project", ""):
        payload.project = args.project.strip()
    if getattr(args, "agent_id", ""):
        payload.agent_id = args.agent_id.strip()
    if getattr(args, "session_id", ""):
        payload.session_id = args.session_id.strip()

    # ── Run ingestion ───────────────────────────────────────────────────────
    try:
        backend = _build_backend(config)
        result = backend.ingest_transcript_handoff(
            payload,
            export_format=args.export_format,
            output_path=args.output_path,
            max_nodes=args.max_nodes,
        )
    except ValidationFailure as exc:
        _emit_cli_error("validation_error", str(exc), {})
        return 1
    except WaggleError as exc:
        _emit_cli_error(exc.code, str(exc), {"status_code": exc.status_code})
        return 2
    except Exception as exc:
        _emit_cli_error("backend_error", str(exc), {"type": type(exc).__name__})
        return 2

    # ── Emit success JSON to stdout ─────────────────────────────────────────
    output: dict[str, Any] = {
        "scope": {
            "project": result.project,
            "agent_id": result.agent_id,
            "session_id": result.session_id,
        },
        "input_message_count": result.input_message_count,
        "transcript_records_written": result.transcript_records_written,
        "transcript_records_skipped": result.transcript_records_skipped,
        "logical_turns_processed": result.logical_turns_processed,
        "unpaired_trailing_blocks": result.unpaired_trailing_blocks,
        "nodes_created": result.nodes_created,
        "nodes_reused": result.nodes_reused,
        "conflicts": result.conflicts,
    }
    if result.export_skipped:
        output["export_skipped"] = True
        output["export_skipped_reason"] = result.export_skipped_reason
    else:
        output["export_skipped"] = False
        output["markdown_path"] = result.markdown_path
        output["json_path"] = result.json_path
        output["export_node_count"] = result.export_node_count
        output["export_edge_count"] = result.export_edge_count
    if result.checkpoint_path:
        output["checkpoint_path"] = result.checkpoint_path
        output["checkpoint_scope"] = result.checkpoint_scope

    print(json.dumps(output, indent=2))
    return 0


# ---------------------------------------------------------------------------
# init wizard
# ---------------------------------------------------------------------------

_GREEN = "\033[92m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_NO_COLOR = not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    """Apply ANSI colour code, or return plain text when not a tty."""
    return text if _NO_COLOR else f"{code}{text}{_RESET}"


def _prompt_choice(question: str, choices: list[str]) -> str:
    """Render an arrow-key style menu (keyboard fallback: number entry)."""
    print(f"\n{_c(_BOLD, question)}")
    for i, choice in enumerate(choices, 1):
        print(f"  {_c(_CYAN, str(i) + '.')} {choice}")
    while True:
        raw = input(f"  Enter number [1-{len(choices)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            selected = choices[int(raw) - 1]
            print(f"  {_c(_GREEN, '>')} {selected}")
            return selected
        print(f"  {_c(_RED, 'Invalid choice, try again.')}")


def _prompt_path(question: str, default: str) -> str:
    """Prompt for a file path, showing the default."""
    print(f"\n{_c(_BOLD, question)}")
    raw = input(f"  [{default}]: ").strip()
    result = raw or default
    print(f"  {_c(_GREEN, '>')} {result}")
    return result


def _ok(msg: str) -> None:
    print(f"  {_c(_GREEN, chr(0x2705))} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_c(_RED, chr(0x274C))} {msg}")


def _python_exe() -> str:
    """Return the Python executable used to run this process."""
    return sys.executable


def _default_stdio_command() -> str:
    """Return the preferred packaged command name for public MCP client configs."""
    return "waggle-mcp"


# ── client config writers ────────────────────────────────────────────────────


def _write_claude_desktop(db_path: str, python_exe: str) -> Path:
    """Write ~/.config/claude/claude_desktop_config.json (macOS/Linux)."""
    if sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        config_dir = Path.home() / ".config" / "claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "claude_desktop_config.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_cursor(db_path: str, python_exe: str) -> Path:
    """Write ~/.cursor/mcp.json."""
    config_dir = Path.home() / ".cursor"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_gemini(db_path: str, python_exe: str) -> Path:
    """Write or update ~/.gemini/settings.json."""
    config_dir = Path.home() / ".gemini"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "settings.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
        "trust": False,
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_antigravity(db_path: str, python_exe: str) -> Path:
    """Write or update the Antigravity AI agent MCP config."""
    if sys.platform == "win32":
        config_dir = Path.home() / ".gemini" / "antigravity"
    else:
        config_dir = Path.home() / ".gemini" / "antigravity"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp_config.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_codex(db_path: str, python_exe: str) -> Path:
    """Write or update the Waggle MCP server block in ~/.codex/config.toml."""
    config_dir = Path.home() / ".codex"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.toml"
    # Escape backslashes in Windows paths for TOML (and regex replacement)
    db_path_escaped = db_path.replace("\\", "\\\\")

    toml_block = (
        "[mcp_servers.waggle]\n"
        f'command = "{_default_stdio_command()}"\n'
        'args = ["serve", "--transport", "stdio"]\n'
        "\n"
        "[mcp_servers.waggle.env]\n"
        'WAGGLE_BACKEND = "sqlite"\n'
        f'WAGGLE_DB_PATH = "{db_path_escaped}"\n'
        'WAGGLE_DEFAULT_TENANT_ID = "local-default"\n'
        'WAGGLE_MODEL = "all-MiniLM-L6-v2"\n'
    )
    existing = config_file.read_text() if config_file.exists() else ""
    pattern = re.compile(r"(?ms)^\[mcp_servers\.waggle\]\n.*?(?=^\[(?!mcp_servers\.waggle(?:\.env)?\])[^\n]+\]\n|\Z)")
    replacement = toml_block.rstrip() + "\n"
    if pattern.search(existing):
        updated = pattern.sub(lambda m: replacement, existing, count=1)
    else:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + replacement
    config_file.write_text(updated)
    return config_file


def _write_codex_agents(root_dir: Path | None = None) -> Path:
    """Write or update a Waggle-managed automatic-memory block in AGENTS.md."""
    project_root = (root_dir or Path.cwd()).resolve()
    agents_file = project_root / "AGENTS.md"
    existing = agents_file.read_text() if agents_file.exists() else ""
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(_AGENTS_MEMORY_BLOCK_HEADER)}\n.*?^\s*{re.escape(_AGENTS_MEMORY_BLOCK_FOOTER)}\n?"
    )

    block = _AGENTS_MEMORY_BLOCK
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    else:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + block
    agents_file.write_text(updated.rstrip() + "\n")
    return agents_file


def _write_other(db_path: str, python_exe: str) -> Path:
    """Write a generic JSON snippet to ~/waggle-mcp-config.json."""
    config_file = Path.home() / "waggle-mcp-config.json"
    snippet = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(snippet, indent=2))
    return config_file


_CLIENT_WRITERS = {
    "Claude Desktop": _write_claude_desktop,
    "Cursor": _write_cursor,
    "Gemini CLI": _write_gemini,
    "Antigravity": _write_antigravity,
    "Codex": _write_codex,
    "Other": _write_other,
}

_RESTART_HINTS = {
    "Claude Desktop": "Restart Claude Desktop to activate.",
    "Cursor": "Reload the Cursor window (Cmd/Ctrl+Shift+P → 'Reload Window') to activate.",
    "Gemini CLI": "Restart Gemini CLI, then run /mcp to confirm Waggle is connected.",
    "Antigravity": "Restart Antigravity to activate the AI agent MCP config.",
    "Codex": "Restart Codex to activate.",
    "Other": "Add the JSON config to your MCP client's server list, then restart it.",
}

_CLIENT_ALIASES = {
    "claude": "Claude Desktop",
    "claude-desktop": "Claude Desktop",
    "claude_desktop": "Claude Desktop",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "gemini-cli": "Gemini CLI",
    "gemini_cli": "Gemini CLI",
    "antigravity": "Antigravity",
    "codex": "Codex",
    "other": "Other",
}


def _client_config_probe_paths() -> dict[str, list[Path]]:
    return {
        "Claude Desktop": [
            Path.home() / ".config" / "claude" / "claude_desktop_config.json",
            Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        ],
        "Cursor": [
            Path.home() / ".cursor" / "mcp.json",
            Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "mcp.json",
        ],
        "Gemini CLI": [Path.home() / ".gemini" / "settings.json"],
        "Antigravity": [Path.home() / ".gemini" / "antigravity" / "mcp_config.json"],
        "Codex": [Path.home() / ".codex" / "config.toml"],
    }


def _normalize_setup_clients(raw_clients: str) -> list[str]:
    clients: list[str] = []
    for raw_client in raw_clients.split(","):
        key = raw_client.strip().lower()
        if not key:
            continue
        client = _CLIENT_ALIASES.get(key)
        if client is None:
            supported = ", ".join(sorted(_CLIENT_ALIASES))
            raise ValidationFailure(f"Unsupported setup client: {raw_client!r}. Supported values: auto, {supported}.")
        if client not in clients:
            clients.append(client)
    if not clients:
        raise ValidationFailure("No setup clients were provided.")
    return clients


def _detect_setup_clients() -> list[str]:
    detected: list[str] = []
    for client, paths in _client_config_probe_paths().items():
        if any(path.exists() for path in paths):
            detected.append(client)
    if not detected and (Path.cwd() / "AGENTS.md").exists():
        detected.append("Codex")
    return detected


def _setup_clients_from_args(raw_clients: str) -> list[str]:
    if raw_clients.strip().lower() == "auto":
        detected = _detect_setup_clients()
        return detected or ["Codex"]
    return _normalize_setup_clients(raw_clients)


# ── Hook-capable tool selection ──────────────────────────────────
# Hooks are currently Claude-Code-specific (written to ~/.claude/settings.json),
# separate from the MCP client config writers above. This map is the extension
# point for future hook-capable tools.
_HOOK_TOOL_ALIASES = {
    "claude-code": "Claude Code",
    "claude_code": "Claude Code",
    "claudecode": "Claude Code",
}
_ALL_HOOK_TOOLS = ["Claude Code"]


def _hook_tools_from_args(raw_hooks: str, no_hooks: bool) -> list[str]:
    """Resolve which hook-capable tools should receive automatic memory hooks.

    'auto' selects all hook-capable tools (currently Claude Code); 'none' selects
    none. --no-hooks is a deprecated alias for --hooks none. A comma-separated
    list selects specific tools by alias.
    """
    raw = (raw_hooks or "auto").strip().lower()
    if no_hooks:
        raw = "none"
    if raw == "none":
        return []
    if raw == "auto":
        return list(_ALL_HOOK_TOOLS)
    tools: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        tool = _HOOK_TOOL_ALIASES.get(key)
        if tool is None:
            supported = ", ".join(sorted(_HOOK_TOOL_ALIASES))
            raise ValidationFailure(f"Unsupported hook target: {part!r}. Supported values: auto, none, {supported}.")
        if tool not in tools:
            tools.append(tool)
    if not tools:
        raise ValidationFailure("No hook targets were provided.")
    return tools


# ── Claude Code hook constants ────────────────────────────────────────────────
_CLAUDE_HOOKS_BLOCK_HEADER = "# >>> waggle-managed >>>"
_CLAUDE_HOOKS_BLOCK_FOOTER = "# <<< waggle-managed <<<"


def _hooks_block(hook_dir: Path) -> str:
    """Build the managed hooks JSON block for Claude Code settings."""
    str(hook_dir / "pre_response.py")
    str(hook_dir / "post_response.py")
    str(hook_dir / "pre_compact.py")
    return (
        f"{_CLAUDE_HOOKS_BLOCK_HEADER}\n"
        "# Waggle automatic memory hooks — do not edit this block manually.\n"
        "# Run 'waggle-mcp setup --yes' to update or 'waggle-mcp uninstall-hooks' to remove.\n"
        f"{_CLAUDE_HOOKS_BLOCK_FOOTER}"
    )


def _hooks_json_block(hook_dir: Path) -> dict[str, Any]:
    """Return the hooks dict to merge into Claude Code settings."""
    pre_response = str(hook_dir / "pre_response.py")
    post_response = str(hook_dir / "post_response.py")
    pre_compact = str(hook_dir / "pre_compact.py")
    python_exe = _python_exe()
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_exe} {pre_response}",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_exe} {post_response}",
                        }
                    ],
                }
            ],
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_exe} {pre_compact}",
                        }
                    ],
                }
            ],
        }
    }


def _find_claude_settings() -> Path | None:
    """Return the Claude Code settings.json path if it exists."""
    candidates = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".config" / "claude" / "settings.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Return the primary path even if it doesn't exist (for creation)
    return Path.home() / ".claude" / "settings.json"


def _install_claude_hooks(hook_dir: Path, *, dry_run: bool = False) -> Path | None:
    """Write the waggle-managed hooks block into Claude Code settings.json.

    Returns the settings path if written, None if Claude Code not detected.
    """
    settings_path = _find_claude_settings()
    if settings_path is None:
        return None

    hook_data = _hooks_json_block(hook_dir)

    if dry_run:
        return settings_path

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = settings_path.read_text() if settings_path.exists() else "{}"
    try:
        existing = json.loads(existing_text)
    except json.JSONDecodeError:
        existing = {}

    # Merge hooks block (idempotent — overwrite waggle keys)
    existing_hooks = existing.get("hooks", {})
    for event, entries in hook_data["hooks"].items():
        # Remove any existing waggle entries for this event
        existing_entries = [
            e
            for e in existing_hooks.get(event, [])
            if not any("waggle" in str(h.get("command", "")) for h in e.get("hooks", []))
        ]
        existing_entries.extend(entries)
        existing_hooks[event] = existing_entries
    existing["hooks"] = existing_hooks

    settings_path.write_text(json.dumps(existing, indent=2))
    return settings_path


def _uninstall_claude_hooks() -> Path | None:
    """Remove waggle hook entries from Claude Code settings.json.

    Returns the settings path if modified, None if nothing to do.
    """
    settings_path = _find_claude_settings()
    if settings_path is None or not settings_path.exists():
        return None

    try:
        existing = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return None

    hooks = existing.get("hooks", {})
    changed = False
    for event in list(hooks.keys()):
        filtered = [
            e for e in hooks[event] if not any("waggle" in str(h.get("command", "")) for h in e.get("hooks", []))
        ]
        if len(filtered) != len(hooks[event]):
            changed = True
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    if not changed:
        return None

    existing["hooks"] = hooks
    settings_path.write_text(json.dumps(existing, indent=2))
    return settings_path


def _run_uninstall_hooks() -> int:
    """Remove the waggle-managed hooks block from Claude Code settings."""
    result = _uninstall_claude_hooks()
    if result is None:
        print("No Waggle hooks found in Claude Code settings (nothing to remove).")
    else:
        _ok(f"Waggle hooks removed from {result}")
    return 0


def _run_demo(args: argparse.Namespace) -> int:
    """Run the 60-second local demo with the bundled example graph."""

    with_embeddings = bool(getattr(args, "with_embeddings", False))
    model_name = "all-MiniLM-L6-v2" if with_embeddings else "deterministic"

    # Locate the bundled demo.abhi
    demo_abhi = Path(__file__).resolve().parent.parent.parent / "examples" / "demo.abhi"
    if not demo_abhi.exists():
        # Try relative to the installed package location
        demo_abhi = Path(__file__).resolve().parent / "examples" / "demo.abhi"
    if not demo_abhi.exists():
        # Try from cwd
        demo_abhi = Path.cwd() / "examples" / "demo.abhi"
    if not demo_abhi.exists():
        _fail(
            "Could not find examples/demo.abhi. "
            "Run 'python3 examples/generate_demo_abhi.py' from the repo root to regenerate it."
        )
        return 1

    # Create a temp directory (NOT in the user's home)
    tmp_dir = Path(tempfile.mkdtemp(prefix="waggle-demo-"))
    demo_db = tmp_dir / "demo.db"

    print()
    print(_c(_BOLD, "waggle-mcp demo"))
    print(_c(_CYAN, "─" * 50))
    print(f"  graph:   {demo_abhi}")
    print(f"  db:      {demo_db}")
    print(f"  model:   {model_name}")
    print()

    try:
        # Import the demo graph
        embedding_model = EmbeddingModel(model_name)
        graph = MemoryGraph(
            str(demo_db),
            embedding_model,
            tenant_id="local-default",
            enable_dedup=False,
        )
        imported = graph.import_abhi(input_path=demo_abhi, merge_strategy="skip-existing")
        print(f"  Imported {imported.nodes_created} nodes, {imported.edges_created} edges from demo.abhi")
        print()

        # ── Query 1: What database did we choose? ─────────────────────────────
        print(_c(_BOLD, "Query 1: What database did we choose?"))
        result1 = graph.query(query="What database did we choose?", max_nodes=6, max_depth=2)
        if result1.nodes:
            for node in result1.nodes[:4]:
                marker = "  [decision]" if node.node_type.value == "decision" else "  [note]    "
                print(f"{marker} {node.label}")
        else:
            print("  (no results)")
        print()

        # ── Query 2: What changed about the database decision? ────────────────
        print(_c(_BOLD, "Query 2: What changed about the database decision?"))
        result2 = graph.query(
            query="What changed about the database decision? contradiction superseded", max_nodes=8, max_depth=2
        )
        # Show contradiction/update edges
        contradiction_edges = [e for e in result2.edges if e.relationship in ("contradicts", "updates")]
        if contradiction_edges:
            for edge in contradiction_edges[:3]:
                src = next((n for n in result2.nodes if n.id == edge.source_id), None)
                tgt = next((n for n in result2.nodes if n.id == edge.target_id), None)
                if src and tgt:
                    print(f"  [{edge.relationship}] {src.label}")
                    print(f"    → {tgt.label}")
        elif result2.nodes:
            for node in result2.nodes[:3]:
                print(f"  {node.label}")
        else:
            print("  (no results)")
        print()

        # ── Query 3: What are our team's preferences? ─────────────────────────
        print(_c(_BOLD, "Query 3: What are our team's preferences?"))
        result3 = graph.query(query="team preferences", max_nodes=8, max_depth=1)
        pref_nodes = [n for n in result3.nodes if n.node_type.value == "preference"]
        if pref_nodes:
            for node in pref_nodes[:3]:
                print(f"  [preference] {node.label}")
        elif result3.nodes:
            for node in result3.nodes[:3]:
                print(f"  {node.label}")
        else:
            print("  (no results)")
        print()

        # ── Query 4: Show decisions and their reasons ─────────────────────────
        print(_c(_BOLD, "Query 4: Show decisions and their reasons"))
        result4 = graph.query(query="decisions reasons why", max_nodes=10, max_depth=2)
        decision_nodes = [n for n in result4.nodes if n.node_type.value == "decision"]
        reason_edges = [e for e in result4.edges if e.relationship == "derived_from"]
        if decision_nodes:
            for dec in decision_nodes[:3]:
                print(f"  [decision] {dec.label}")
                # Find reason nodes connected via derived_from
                reason_ids = {e.target_id for e in reason_edges if e.source_id == dec.id}
                for node in result4.nodes:
                    if node.id in reason_ids:
                        print(f"    ↳ [reason] {node.label}")
        else:
            print("  (no results)")
        print()

        # ── Graph Studio URL ──────────────────────────────────────────────────
        studio_url = "http://127.0.0.1:8686/graph?mode=view"
        print(_c(_CYAN, "─" * 50))
        print(f"  Graph Studio: {studio_url}")
        print(f"  (run 'WAGGLE_DB_PATH={demo_db} waggle-mcp ui' to open it)")
        print()
        print(f"  Cleanup: rm -rf {tmp_dir}")
        print()

    except Exception as exc:
        _fail(f"Demo failed: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


def _run_setup(args: argparse.Namespace) -> int:
    """Non-interactive setup command for one-line installs."""
    if not args.yes and not args.dry_run:
        _fail("Refusing to patch config without --yes. Use --dry-run to preview changes.")
        return 1

    db_path_raw = args.db or DEFAULT_DB_PATH
    db_path = str(Path(db_path_raw).expanduser().resolve())
    python_exe = _python_exe()
    clients = _setup_clients_from_args(args.clients)
    hook_tools = _hook_tools_from_args(getattr(args, "hooks", "auto"), bool(getattr(args, "no_hooks", False)))

    print()
    print(_c(_BOLD, "waggle-mcp setup"))
    print(_c(_CYAN, "─" * 40))
    print(f"  clients: {', '.join(clients)}")
    print(f"  database: {db_path}")
    print(f"  model: {args.model}")
    if args.dry_run:
        print("  mode: dry-run")
        for client in clients:
            _ok(f"Would configure {client}")
        if "Claude Code" in hook_tools:
            hooks_path = _find_claude_settings()
            _ok(f"Would install Claude Code hooks in {hooks_path}")
        if args.project_instructions and "Codex" in clients:
            agents_path = (Path.cwd() / "AGENTS.md").resolve()
            _ok(f"Would write Codex automatic-memory instructions to {agents_path}")
        print()
        return 0

    for client in clients:
        writer = _CLIENT_WRITERS[client]
        try:
            config_file = writer(db_path, python_exe)
            if args.model != "all-MiniLM-L6-v2":
                _replace_model_in_client_config(config_file, args.model)
            _ok(f"{client} config written to {config_file}")
        except OSError as exc:
            _fail(f"Could not write {client} config: {exc}")
            return 1

    if args.project_instructions and "Codex" in clients:
        try:
            agents_file = _write_codex_agents()
            _ok(f"Codex automatic-memory instructions written to {agents_file}")
        except OSError as exc:
            _fail(f"Could not write AGENTS.md instructions: {exc}")
            return 1

    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _ok(f"Database directory ready at {Path(db_path).parent}")
    except OSError as exc:
        _fail(f"Could not create database directory: {exc}")
        return 1

    for client in clients:
        print(f"  {_c(_CYAN, chr(0x27A1))}  {_RESTART_HINTS[client]}")
    print()

    # Install automatic memory hooks for the selected hook-capable tools
    if hook_tools and not args.dry_run:
        if "Claude Code" in hook_tools:
            hook_dir = Path(__file__).resolve().parent / "hooks" / "claude_code"
            if hook_dir.exists():
                try:
                    hooks_path = _install_claude_hooks(hook_dir)
                    if hooks_path is not None:
                        _ok(f"Claude Code hooks installed in {hooks_path}")
                except OSError as exc:
                    # Non-fatal: hooks are optional
                    LOGGER.warning("claude_hooks_install_failed", extra={"error": str(exc)})

    if args.run_doctor:
        doctor_config = AppConfig.from_env()
        doctor_config.db_path = db_path
        doctor_config.model_name = args.model
        doctor_exit = _run_doctor_command(doctor_config, args)
        if doctor_exit:
            print(_c(_CYAN, "Setup completed; doctor reported follow-up warnings above."))
        return 0
    return 0


def _replace_model_in_client_config(config_file: Path, model_name: str) -> None:
    """Patch WAGGLE_MODEL after using the existing config writers."""
    if config_file.suffix == ".toml":
        text = config_file.read_text()
        text = re.sub(r'WAGGLE_MODEL = "[^"]*"', f'WAGGLE_MODEL = "{model_name}"', text)
        config_file.write_text(text)
        return

    try:
        payload = json.loads(config_file.read_text())
    except json.JSONDecodeError:
        return

    if config_file.name == "waggle-mcp-config.json":
        env = payload.setdefault("env", {})
        if isinstance(env, dict):
            env["WAGGLE_MODEL"] = model_name
    else:
        servers = payload.get("mcpServers")
        if isinstance(servers, dict) and isinstance(servers.get("waggle"), dict):
            env = servers["waggle"].setdefault("env", {})
            if isinstance(env, dict):
                env["WAGGLE_MODEL"] = model_name
    config_file.write_text(json.dumps(payload, indent=2))


def _run_init() -> int:
    """Interactive setup wizard for waggle-mcp."""
    print()
    print(_c(_BOLD, "waggle-mcp setup wizard"))
    print(_c(_CYAN, "─" * 40))

    clients = list(_CLIENT_WRITERS.keys())
    client = _prompt_choice("Which MCP client are you using?", clients)

    default_db = DEFAULT_DB_PATH
    db_path_raw = _prompt_path("Where should the database be stored?", default_db)
    db_path = str(Path(db_path_raw).expanduser().resolve())

    python_exe = _python_exe()

    print()

    # Write client config
    writer = _CLIENT_WRITERS[client]
    try:
        config_file = writer(db_path, python_exe)
        _ok(f"Config written to {config_file}")
    except OSError as exc:
        _fail(f"Could not write config: {exc}")
        return 1

    if client == "Codex":
        try:
            agents_file = _write_codex_agents()
            _ok(f"Automatic memory instructions written to {agents_file}")
        except OSError as exc:
            _fail(f"Could not write AGENTS.md instructions: {exc}")
            return 1

    # Create database directory
    db_dir = Path(db_path).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        _ok(f"Database directory ready at {db_dir}")
    except OSError as exc:
        _fail(f"Could not create database directory: {exc}")
        return 1

    # Restart hint
    print(f"  {_c(_CYAN, chr(0x27A1))}  {_RESTART_HINTS[client]}")
    print()
    return 0


def main() -> None:
    # ── Windows UTF-8 guard (Error 3 from field bug log) ────────────────────
    # Windows consoles default to cp1252. Unicode log lines / emoji cause
    # UnicodeEncodeError that crashes test scripts or corrupts stdio framing.
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # best-effort; don't break startup over encoding
    _assert_runtime_feature_parity()
    parser = _build_parser()
    args = _normalize_pull_strategy_args(parser.parse_args())
    command = args.command or "serve"

    # Commands that should work before full backend/app initialization.
    if command == "setup":
        try:
            sys.exit(_run_setup(args))
        except ValidationFailure as exc:
            _emit_cli_error("validation_error", str(exc), {})
            sys.exit(1)
    if command == "demo":
        try:
            sys.exit(_run_demo(args))
        except Exception as exc:
            _emit_cli_error("internal_error", str(exc), {"type": type(exc).__name__})
            sys.exit(1)
    if command == "uninstall-hooks":
        sys.exit(_run_uninstall_hooks())
    if command == "init":
        sys.exit(_run_init())
    if command == "features":
        print(_FEATURES_GUIDE)
        return
    if command == "doctor":
        # Doctor only needs the config — not a live backend connection.
        config = AppConfig.from_env()
        sys.exit(_run_admin_command(config, args))

    config = AppConfig.from_env()
    if command == "serve" and getattr(args, "transport", None):
        config.transport = str(args.transport).strip().lower()
        config.validate()
    log_stream = sys.stderr if config.transport == "stdio" else sys.stdout
    configure_logging(config.log_level, stream=log_stream)
    LOGGER.info("waggle_startup")
    if command in {"edit-graph", "view-graph", "ui", "graph-studio", "open-studio"}:
        try:
            exit_code = _run_graph_editor_command(config, args)
        except ValidationFailure as exc:
            _emit_cli_error("validation_error", str(exc), {})
            sys.exit(1)
        except WaggleError as exc:
            _emit_cli_error(exc.code, str(exc), {"status_code": exc.status_code})
            sys.exit(2)
        except Exception as exc:
            _emit_cli_error("internal_error", str(exc), {"type": type(exc).__name__})
            sys.exit(3)
        sys.exit(exit_code or 0)
    if command != "serve":
        try:
            exit_code = _run_admin_command(config, args)
        except ValidationFailure as exc:
            _emit_cli_error("validation_error", str(exc), {})
            sys.exit(1)
        except WaggleError as exc:
            _emit_cli_error(exc.code, str(exc), {"status_code": exc.status_code})
            sys.exit(2)
        except Exception as exc:
            _emit_cli_error("internal_error", str(exc), {"type": type(exc).__name__})
            sys.exit(3)
        sys.exit(exit_code or 0)
        return
    if config.transport == "http":
        run_http(config)
        return
    if config.backend == "sqlite":
        Path(config.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_stdio(config))


if __name__ == "__main__":
    main()
