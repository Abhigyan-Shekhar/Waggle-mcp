"""Protocol-independent tool dispatcher for Waggle.

This module contains the complete tool dispatch logic extracted from
``WaggleServer``.  No MCP-specific types appear here.  The MCP adapter
(v1 or v2) converts ``WaggleToolResult`` to the appropriate wire type.

Usage
-----
::

    dispatcher = WaggleToolDispatcher(graph, config, metrics)
    ctx = WaggleRequestContext(request_id="...", tenant_id="...", transport="stdio")
    result = dispatcher.call_tool("query_graph", {"query": "user's name"}, ctx)
    # result.text  → human-readable text
    # result.structured  → machine-readable dict
    # result.is_error  → bool

"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

from waggle.abhi import serialize_abhi_diff
from waggle.config import AppConfig
from waggle.embeddings import EMBEDDING_FREE_TOOLS, STATUS_DISABLED, STATUS_READY
from waggle.errors import ValidationFailure, WaggleError
from waggle.metrics import MetricsRegistry
from waggle.models import Node, NodeType, RelationType
from waggle.recursive_context import RECURSIVE_CONTEXT_ENABLED, RecursiveContextController
from waggle.runtime_context import runtime_context
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
    serialize_stats,
    serialize_subgraph,
    serialize_timeline,
    serialize_topics,
)

from .catalog import WaggleToolDefinition
from .context import WaggleRequestContext
from .results import WaggleToolResult
from .validation import validate_against_schema, validate_tool_payload

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers (previously private methods of WaggleServer)
# ---------------------------------------------------------------------------


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


_READ_ONLY_TOOLS = frozenset(
    {
        "dedup_candidates",
        "debug_retrieval",
        "get_related",
        "get_node_history",
        "list_context_scopes",
        "list_context_windows",
        "get_context_window",
        "timeline",
        "list_conflicts",
        "graph_diff",
        "prime_context",
        "get_topics",
        "get_stats",
        "edge_quality_report",
        "build_context",
        "diff",
        "grep",
        "load_abhi_chunks",
        "fsck",
        "show",
    }
)

_DESTRUCTIVE_TOOLS = frozenset(
    {
        "canonicalize_node",
        "resolve_conflict",
        "update_node",
        "delete_node",
        "clear_session",
        "clear_project",
        "clear_all",
        "import_markdown_vault",
    }
)


def _tool_annotations(name: str) -> dict[str, bool]:
    read_only = name in _READ_ONLY_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": name in _DESTRUCTIVE_TOOLS,
        "idempotentHint": read_only,
    }


# ---------------------------------------------------------------------------
# Tool alias table (unchanged from WaggleServer._TOOL_ALIASES)
# ---------------------------------------------------------------------------

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
    "recursive_context": ("build_context", {}),
    "assemble_context": ("build_context", {}),
    "rlm_context": ("build_context", {}),
}

_GRAPH_SIZE_TOOLS = {
    "store_node",
    "store_edge",
    "canonicalize_node",
    "resolve_conflict",
    "clear_session",
    "clear_project",
    "clear_all",
    "decompose_and_store",
    "observe_conversation",
    "pull",
    "merge",
    "import_markdown_vault",
}


def _recursive_context_enabled() -> bool:
    server_module = sys.modules.get("waggle.server")
    if server_module is not None and "RECURSIVE_CONTEXT_ENABLED" in getattr(server_module, "__dict__", {}):
        return bool(server_module.RECURSIVE_CONTEXT_ENABLED)
    return bool(RECURSIVE_CONTEXT_ENABLED)


def _parse_as_of(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationFailure("as_of must be a valid ISO-8601 datetime.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# WaggleToolDispatcher
# ---------------------------------------------------------------------------


class WaggleToolDispatcher:
    """Protocol-independent dispatcher for all Waggle memory tools.

    Accepts a *graph* (``MemoryGraph`` or ``Neo4jMemoryGraph``) that has
    already been resolved to the correct tenant.  For multi-tenant HTTP
    deployments the tenant graph is resolved by the HTTP middleware *before*
    calling this class.
    """

    def __init__(
        self,
        graph: Any,
        config: AppConfig,
        metrics: MetricsRegistry,
    ) -> None:
        self._graph = graph
        self.config = config
        self.metrics = metrics
        # Cache the tool catalog once so call_tool() can look up schemas without
        # re-building the list on every request (list_tools() is pure but not free).
        self._tool_catalog: dict[str, WaggleToolDefinition] = {t.name: t for t in self.list_tools()}

    # ── Tool catalogue ────────────────────────────────────────────────────

    def list_tools(self) -> list[WaggleToolDefinition]:
        """Return the complete Waggle tool catalogue."""
        tools: list[WaggleToolDefinition] = [
            WaggleToolDefinition(
                name="store_node",
                description=(
                    "Store a piece of knowledge as a node in the persistent memory graph. "
                    "Call this whenever you learn something important from the user: facts, "
                    "preferences, decisions, entities, concepts, or questions. Prefer atomic facts."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="store_edge",
                description=(
                    "Create a relationship between two stored nodes. Use this immediately after "
                    "storing related nodes so the memory graph preserves structure, updates, and conflicts."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="canonicalize_node",
                description=(
                    "Manually merge multiple nodes into a single canonical node. "
                    "All aliases from the merged nodes flow into the canonical node's aliases. "
                    "All edges pointing to/from merged nodes are re-pointed to the canonical node. "
                    "Merged nodes are deleted.  Idempotent: merging an already-merged node is a no-op. "
                    "Use this after reviewing dedup_candidates to resolve ambiguous duplicates."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="dedup_candidates",
                description=(
                    "Return pairs of nodes whose embeddings are above a threshold but below the "
                    "auto-merge threshold.  Intended for human review before calling canonicalize_node. "
                    "Returns pairs sorted by descending similarity so the most likely duplicates appear first."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="aggregate_graph",
                description=(
                    "Retrieve a broad set of nodes bypassing standard semantic limits, optimized for "
                    "global aggregation and map-reduce tasks. Supports filtering by node_type and tags."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="query_graph",
                description=(
                    "Automatically search the memory graph before answering questions that may depend on prior context, "
                    "user preferences, project decisions, constraints, or earlier conversation state. "
                    "Returns a serialized subgraph with matching nodes and their connected neighborhood. "
                    "Uses hybrid retrieval (transcript + graph) by default for robust fallback. "
                    "Understands temporal references such as 'recently', 'latest', 'originally', and 'last week'. "
                    "Benchmark modes: use retrieval_mode='graph' for graph-only (no verbatim fallback), 'verbatim' for transcript-only."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="debug_retrieval",
                description=(
                    "Diagnose memory retrieval ranking for a query. Returns query embedding preview, context-window "
                    "routing scores, selected windows, flat top nodes, and tiered top nodes for comparison."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="get_related",
                description=(
                    "Fetch the neighborhood around a specific memory node. Use when you already have a node ID "
                    "and need its connected context. Returns matching nodes and edges as a serialized subgraph."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="get_node_history",
                description=(
                    "Inspect one memory node's evidence, validity window, and connected context. Use when auditing "
                    "why a memory exists or how it changed. Returns the node, evidence records, related nodes, and edges."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="list_context_scopes",
                description=(
                    "List known agent, project, and session scope values stored in the current tenant graph. "
                    "Use before filtering memory by scope. Returns arrays of scope identifiers."
                ),
                input_schema=_object_input_schema(),
            ),
            WaggleToolDefinition(
                name="list_context_windows",
                description=(
                    "List context windows for a project. Use to inspect chat/session-level memory containers, "
                    "their status, node counts, and update times."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="get_context_window",
                description=(
                    "Inspect one context window, including its nodes and links to other context windows. "
                    "Use when auditing what a conversation/session contributed to memory."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="close_context_window",
                description=(
                    "Close a context window, recompute its final graph embedding, refresh node counts, "
                    "and derive cross-window edges. Use when a chat/session is complete."
                ),
                input_schema=_object_input_schema(
                    {"window_id": {"type": "string", "description": "ID of the context window to close."}},
                    required=["window_id"],
                ),
            ),
            WaggleToolDefinition(
                name="timeline",
                description=(
                    "Build a chronological view of memory changes for a node, a query result, or the whole tenant. "
                    "Use when order and evidence matter. Returns timestamped timeline items."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="list_conflicts",
                description=(
                    "List contradiction and update edges, with unresolved conflicts shown by default. "
                    "Use to review memory disagreements before resolving them. Returns conflict entries with source and target nodes."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="resolve_conflict",
                description=(
                    "Mark a contradiction or update edge as resolved without deleting the underlying history. "
                    "Use after deciding how competing memories should be interpreted. Returns the resolved conflict entry. "
                    "When winner is provided and the edge is CONTRADICTS or UPDATES, the losing node's valid_to is set to now, "
                    "excluding it from future default queries."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="update_node",
                description=(
                    "Update an existing memory node's content, label, or tags. Use when a stored memory needs correction "
                    "without deleting its identity. Returns the updated node."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="delete_node",
                description="Delete a node and all connected edges from persistent memory.",
                input_schema=_object_input_schema(
                    {"node_id": {"type": "string", "description": "ID of the node to delete."}},
                    required=["node_id"],
                ),
            ),
            WaggleToolDefinition(
                name="clear_session",
                description=(
                    "Delete all memory data for one session/context window stream, including nodes, transcripts, "
                    "context windows, and connected edges. Requires confirm=true."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="clear_project",
                description=(
                    "Delete all memory data for one project/repository, including nodes, transcripts, repos, "
                    "context windows, and connected edges. Requires confirm=true."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="clear_all",
                description=(
                    "Delete all graph memory data for the current tenant. Requires confirm=true. "
                    "This does not remove API keys or tenant metadata."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="decompose_and_store",
                description=(
                    "Break long or complex content into atomic memory nodes, store them automatically, and create inferred edges. "
                    "Use for notes, summaries, or multi-fact passages. Returns the stored subgraph."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
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
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="graph_diff",
                description=(
                    "Show what changed in the memory graph recently, including added nodes, updated nodes, created edges, "
                    "and contradiction edges. Use for review or handoff. Returns a serialized graph diff."
                ),
                input_schema=_object_input_schema(
                    {
                        "since": {
                            "type": "string",
                            "default": "24h",
                            "description": "Lookback window such as '24h', '7d', or an ISO-like timestamp.",
                        }
                    }
                ),
            ),
            WaggleToolDefinition(
                name="prime_context",
                description=(
                    "Automatically build a compact context brief at the start of a scoped conversation or before work that needs continuity. "
                    "Use to hydrate an assistant with the most relevant scoped memories. Returns summary text plus nodes and edges."
                ),
                input_schema=_object_input_schema(_scope_properties()),
            ),
            WaggleToolDefinition(
                name="get_topics",
                description=(
                    "Detect topic clusters in the graph using community detection. Use to understand the main themes "
                    "in memory. Returns labeled clusters with representative nodes and tags. "
                    "Note: scope filtering (project, agent_id, session_id) is optional and silently ignored — "
                    "topic detection always runs across the full tenant graph."
                ),
                input_schema=_object_input_schema(_scope_properties()),
            ),
            WaggleToolDefinition(
                name="get_stats",
                description=(
                    "Return high-level statistics about the current memory graph. Use for health checks or quick summaries. "
                    "Returns node and edge counts, node type breakdowns, and recent or highly connected nodes."
                ),
                input_schema=_object_input_schema(),
            ),
            WaggleToolDefinition(
                name="export_graph_html",
                description=(
                    "Export the current memory graph as an interactive HTML visualization. "
                    "Use when a human needs to inspect the graph visually. Returns the output path and graph counts."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="window_graph_viz",
                description=(
                    "Export the context-window graph as an interactive HTML visualization. "
                    "Each node is a chat/session window and edges show overlap, supersession, temporal order, or shared scope."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="commit",
                description=(
                    "Snapshot the current memory graph to a portable file (waggle commit). "
                    "Exports a JSON backup for migration, restore drills, or offline archive. "
                    "Use commit_format='abhi' (default) for a full .abhi export, or 'backup' for a raw JSON backup. "
                    "Returns the output path, schema version, and object counts."
                ),
                input_schema=_object_input_schema(
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
                        "mode": {
                            "type": "string",
                            "enum": ["prime", "query"],
                            "default": "prime",
                            "description": "Bundle mode: prime exports scoped memory, query exports query-focused context.",
                        },
                        "query": {
                            "type": "string",
                            "default": "",
                            "description": "Optional query used when commit_format='bundle' and mode='query'.",
                        },
                        "max_nodes": {
                            "type": "integer",
                            "default": 25,
                            "minimum": 1,
                            "description": "Maximum number of nodes to include in a context bundle.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 0,
                            "description": "Relationship traversal depth for context bundle retrieval.",
                        },
                        "retrieval_mode": {
                            "type": "string",
                            "enum": ["graph", "verbatim", "hybrid"],
                            "default": "hybrid",
                            "description": "Retrieval strategy for query-mode context bundles.",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "json", "both"],
                            "default": "both",
                            "description": "Context bundle output format.",
                        },
                        "include_edges": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether context bundles should include graph edges.",
                        },
                        "include_timestamps": {
                            "type": "boolean",
                            "default": True,
                            "description": "Whether context bundles should include timestamps.",
                        },
                        "include_source_prompt": {
                            "type": "boolean",
                            "default": False,
                            "description": "Whether context bundles should include stored source prompts.",
                        },
                        "audience": {
                            "type": "string",
                            "enum": ["llm", "human"],
                            "default": "llm",
                            "description": "Target audience for bundle formatting.",
                        },
                        **_scope_properties(),
                    }
                ),
            ),
            WaggleToolDefinition(
                name="pull",
                description=(
                    "Load a memory file into the current graph (waggle pull). "
                    "Accepts a .abhi file (default) or a raw JSON backup. "
                    "Runs integrity verification, schema validation, and constraint checks before merging. "
                    "Returns counts for created and updated nodes and edges."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="diff",
                description=(
                    "Compare two .abhi memory files (waggle diff). "
                    "Reports structural graph changes — added/removed/updated nodes and edges — "
                    "plus lightweight semantic changes. The output is the screenshot that goes on the homepage."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="merge",
                description=(
                    "Three-way merge branching .abhi memory files (waggle merge). "
                    "Merges left and right branches against a common base into one output file. "
                    "Conflicts surface as CONTRADICTS edges — nobody else can do this. "
                    "Use --merge-strategy to control winner selection when both sides changed the same object."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="grep",
                description=(
                    "Execute a saved or ad hoc query against an .abhi file (waggle grep). "
                    "Triggers the file's on_query event actions and returns matching nodes."
                ),
                input_schema=_object_input_schema(
                    {
                        "input_path": {"type": "string", "description": "Path to the .abhi file to query."},
                        "query_id": {"type": "string", "description": "Optional saved query id from the file."},
                        "query_text": {"type": "string", "description": "Optional ad hoc query text to execute."},
                    },
                    required=["input_path"],
                ),
            ),
            WaggleToolDefinition(
                name="load_abhi_chunks",
                description=(
                    "Load only selected or query-relevant chunks from an .abhi file for partial graph inspection."
                ),
                input_schema=_object_input_schema(
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
            WaggleToolDefinition(
                name="fsck",
                description=(
                    "Validate an .abhi memory file without importing it (waggle fsck). "
                    "Verifies integrity hash, schema compliance, and constraint satisfaction. "
                    "Like git fsck — run this before trusting a file you received."
                ),
                input_schema=_object_input_schema(
                    {"input_path": {"type": "string", "description": "Path to the .abhi file to validate."}},
                    required=["input_path"],
                ),
            ),
            WaggleToolDefinition(
                name="show",
                description=(
                    "Inspect an .abhi memory file without loading it into the graph (waggle show). "
                    "Returns summary stats, node/edge type breakdowns, and metadata counts. "
                    "Like git show — quick read-only inspection of a commit object."
                ),
                input_schema=_object_input_schema(
                    {"input_path": {"type": "string", "description": "Path to the .abhi file to inspect."}},
                    required=["input_path"],
                ),
            ),
            WaggleToolDefinition(
                name="export_markdown_vault",
                description=(
                    "Export the current graph as an Obsidian-compatible Markdown vault. "
                    "Use when a human wants browsable note files with graph links. Returns written files and graph counts."
                ),
                input_schema=_object_input_schema(
                    {
                        "root_path": {"type": "string", "description": "Destination directory for the Markdown vault."},
                        **_scope_properties(),
                    },
                    required=["root_path"],
                ),
            ),
            WaggleToolDefinition(
                name="import_markdown_vault",
                description=(
                    "Import an Obsidian-compatible Markdown vault into the current graph non-destructively. "
                    "Use to sync edited vault notes back into memory. Returns created, updated, deleted-edge, and conflict counts."
                ),
                input_schema=_object_input_schema(
                    {
                        "root_path": {
                            "type": "string",
                            "description": "Source directory of the Markdown vault to import.",
                        }
                    },
                    required=["root_path"],
                ),
            ),
            WaggleToolDefinition(
                name="edge_quality_report",
                description=(
                    "Audit the quality of relationship edges in the memory graph. "
                    "Returns counts per edge type, average edge_confidence per type, and the top-10 "
                    "highest- and lowest-confidence edges for each type. "
                    "Useful for diagnosing graph health and identifying noisy RELATES_TO edges."
                ),
                input_schema=_object_input_schema(
                    {
                        **_scope_properties(),
                    }
                ),
            ),
        ]

        # build_context: only included when the feature flag is on.
        if _recursive_context_enabled():
            tools.append(
                WaggleToolDefinition(
                    name="build_context",
                    description=(
                        "Recursively retrieves and compresses relevant Waggle memory for the current task, "
                        "using graph, hybrid, transcript, update, and conflict-aware retrieval. "
                        "Decomposes the query into targeted subqueries, expands the graph around key nodes, "
                        "resolves contradictions and superseded memories, and returns a compact context pack "
                        "under a configurable token budget. "
                        "Aliases: recursive_context, assemble_context, rlm_context."
                    ),
                    input_schema=_object_input_schema(
                        {
                            "query": {
                                "type": "string",
                                "description": "Current user task or question to build context for.",
                            },
                            **_scope_properties(),
                            "context_window_id": {
                                "type": "string",
                                "description": "Optional context window ID to focus retrieval within an existing window.",
                            },
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
            )

        for tool in tools:
            tool.annotations = _tool_annotations(tool.name)

        return tools

    # ── Main dispatch ─────────────────────────────────────────────────────

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: WaggleRequestContext,
        graph_override: Any | None = None,
    ) -> WaggleToolResult:
        """Execute a tool by name and return a ``WaggleToolResult``.

        This method is called from a thread (via ``anyio.to_thread.run_sync``)
        by both the v1 MCP adapter and the v2 MCP adapter.  It is completely
        synchronous and has no awareness of the MCP protocol version.
        """
        graph = graph_override or self._graph

        # Fast-mode guard: return early if embedding not ready.
        fast_mode_result = self._check_embedding_available(name, graph, arguments)
        if fast_mode_result is not None:
            return fast_mode_result

        started = time.perf_counter()
        tenant_id = getattr(graph, "tenant_id", self.config.default_tenant_id)

        with runtime_context(
            request_id=context.request_id,
            tenant_id=tenant_id,
            transport=context.transport,
            backend=self.config.backend,
            api_key_id=context.api_key_id or "",
            tool_name=name,
        ):
            try:
                # Resolve aliases before validation so alias-provided defaults
                # satisfy the canonical tool schema.
                if name in _TOOL_ALIASES:
                    canonical_name, default_args = _TOOL_ALIASES[name]
                    arguments = {**default_args, **arguments}
                    name = canonical_name

                validate_tool_payload(name, arguments, self.config.max_payload_bytes)

                # JSON Schema structural validation (PR 3).
                tool_def = self._tool_catalog.get(name)
                if tool_def is None:
                    self._tool_catalog = {t.name: t for t in self.list_tools()}
                    tool_def = self._tool_catalog.get(name)
                if tool_def is not None:
                    validate_against_schema(name, arguments, tool_def.input_schema)

                # build_context feature-flag guard.
                if name == "build_context" and not _recursive_context_enabled():
                    return self._error_result(
                        ValueError("build_context is disabled by WAGGLE_RECURSIVE_CONTEXT_ENABLED=false.")
                    )

                LOGGER.info("tool_call_started")
                result = self._dispatch(name, arguments, graph)

                elapsed = time.perf_counter() - started
                self.metrics.increment(
                    "waggle_tool_requests_total",
                    tool=name,
                    status="success",
                    tenant_id=tenant_id,
                )
                self.metrics.observe("waggle_tool_latency_seconds", elapsed, tool=name)
                if name in _GRAPH_SIZE_TOOLS:
                    self._record_graph_size(graph)
                LOGGER.info("tool_call_completed")
                return result

            except Exception as exc:
                elapsed = time.perf_counter() - started
                self.metrics.increment(
                    "waggle_tool_requests_total",
                    tool=name,
                    status="failure",
                    tenant_id=tenant_id,
                )
                self.metrics.observe("waggle_tool_latency_seconds", elapsed, tool=name)
                LOGGER.exception("tool_call_failed")
                return self._error_result(exc)

    def _dispatch(self, name: str, arguments: dict[str, Any], graph: Any) -> WaggleToolResult:
        """Inner dispatch switch.  All cases return a ``WaggleToolResult``."""
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
            return self._ok(
                text,
                {
                    **self._node_payload(node),
                    "created": store_result.created,
                    "dedup_reason": store_result.dedup_reason,
                    "similarity": store_result.similarity,
                    "conflicts": [
                        {
                            "other_node_id": c.other_node_id,
                            "other_node_label": c.other_node_label,
                            "relationship": c.relationship,
                            "reason": c.reason,
                        }
                        for c in store_result.conflicts
                    ],
                },
            )

        if name == "store_edge":
            edge = graph.add_edge(
                source_id=arguments["source_id"],
                target_id=arguments["target_id"],
                relationship=arguments["relationship"],
                weight=float(arguments.get("weight", 1.0)),
            )
            return self._ok(
                f"Created edge {edge.id} linking {edge.source_id} to {edge.target_id} as {edge.relationship}.",
                self._edge_payload(edge),
            )

        if name == "canonicalize_node":
            result_obj = graph.canonicalize_node(
                node_ids=arguments["node_ids"],
                canonical_id=arguments["canonical_id"],
            )
            return self._ok(
                f"Merged {len(result_obj.merged_node_ids)} node(s) into canonical node "
                f"'{result_obj.canonical_node.label}' ({result_obj.canonical_node.id}). "
                f"Repointed {result_obj.edges_repointed} edge(s). Added {len(result_obj.aliases_added)} new alias(es).",
                {
                    "canonical_node": self._node_payload(result_obj.canonical_node),
                    "merged_node_ids": result_obj.merged_node_ids,
                    "edges_repointed": result_obj.edges_repointed,
                    "aliases_added": result_obj.aliases_added,
                },
            )

        if name == "dedup_candidates":
            _scope = {
                "project": arguments.get("project", ""),
                "agent_id": arguments.get("agent_id", ""),
                "session_id": arguments.get("session_id", ""),
            }
            threshold = float(arguments.get("threshold", 0.85))
            result_obj = graph.dedup_candidates(scope=_scope, threshold=threshold)
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
            return self._ok(
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

        if name == "aggregate_graph":
            _as_of_raw = arguments.get("as_of")
            _as_of = _parse_as_of(_as_of_raw)
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
            return self._ok(serialize_subgraph(subgraph), self._subgraph_payload(subgraph))

        if name == "query_graph":
            _as_of_raw = arguments.get("as_of")
            _as_of = _parse_as_of(_as_of_raw)
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
            return self._ok(serialize_subgraph(subgraph), self._subgraph_payload(subgraph))

        if name == "debug_retrieval":
            debug = graph.debug_retrieval(
                query=arguments["query"],
                max_nodes=int(arguments.get("max_nodes", 10)),
                max_depth=int(arguments.get("max_depth", 2)),
                agent_id=arguments.get("agent_id", ""),
                project=arguments.get("project", ""),
                session_id=arguments.get("session_id", ""),
                retrieval_mode=arguments.get("retrieval_mode", "hybrid"),
            )
            return self._ok(json.dumps(debug, indent=2), debug)

        if name == "list_context_scopes":
            scopes = graph.list_context_scopes()
            return self._ok(
                f"Known scopes: {len(scopes.agent_ids)} agents, {len(scopes.projects)} projects, {len(scopes.session_ids)} sessions.",
                self._context_scope_payload(scopes),
            )

        if name == "list_context_windows":
            windows = graph.list_context_windows(
                project=arguments.get("project", ""),
                status=arguments.get("status", ""),
                limit=int(arguments.get("limit", 20)),
            )
            return self._ok(
                f"Context windows: {len(windows)}",
                {"windows": [self._context_window_payload(w) for w in windows]},
            )

        if name == "get_context_window":
            window = graph.get_context_window(arguments["window_id"])
            edges = graph.get_context_window_edges(window.id)
            nodes = graph.get_window_nodes(window.id) if bool(arguments.get("include_nodes", True)) else []
            return self._ok(
                f"Context window {window.id}: {window.node_count} nodes, {len(edges)} connected window edge(s).",
                {
                    "window": self._context_window_payload(window),
                    "nodes": [self._node_payload(n) for n in nodes],
                    "window_edges": [self._context_window_edge_payload(e) for e in edges],
                },
            )

        if name == "close_context_window":
            window = graph.close_context_window(arguments["window_id"])
            edges = graph.get_context_window_edges(window.id)
            return self._ok(
                f"Closed context window {window.id} with {window.node_count} nodes and {len(edges)} connected window edge(s).",
                {
                    "window": self._context_window_payload(window),
                    "window_edges": [self._context_window_edge_payload(e) for e in edges],
                },
            )

        if name == "get_related":
            subgraph = graph.get_related(node_id=arguments["node_id"], max_depth=int(arguments.get("max_depth", 2)))
            return self._ok(serialize_subgraph(subgraph), self._subgraph_payload(subgraph))

        if name == "get_node_history":
            history = graph.get_node_history(node_id=arguments["node_id"], max_depth=int(arguments.get("max_depth", 2)))
            return self._ok(serialize_node_history(history), self._node_history_payload(history))

        if name == "timeline":
            timeline = graph.timeline(
                node_id=arguments.get("node_id", ""),
                query=arguments.get("query", ""),
                limit=int(arguments.get("limit", 25)),
                max_depth=int(arguments.get("max_depth", 2)),
                include_evidence=bool(arguments.get("include_evidence", True)),
            )
            return self._ok(serialize_timeline(timeline), self._timeline_payload(timeline))

        if name == "list_conflicts":
            conflicts = graph.list_conflicts(
                include_resolved=bool(arguments.get("include_resolved", False)),
                limit=int(arguments.get("limit", 25)),
            )
            return self._ok(serialize_conflicts(conflicts), self._conflict_list_payload(conflicts))

        if name == "resolve_conflict":
            resolved = graph.resolve_conflict(
                edge_id=arguments["edge_id"],
                resolution_note=arguments.get("resolution_note", ""),
                winner=arguments.get("winner"),
            )
            return self._ok(serialize_conflict_entry(resolved), self._conflict_entry_payload(resolved))

        if name == "update_node":
            node = graph.update_node(
                node_id=arguments["node_id"],
                content=arguments.get("content"),
                label=arguments.get("label"),
                tags=arguments.get("tags"),
            )
            return self._ok(f"Updated node '{node.label}' ({node.id}).", self._node_payload(node))

        if name == "delete_node":
            node = graph.delete_node(node_id=arguments["node_id"])
            return self._ok(
                f"Deleted node '{node.label}' ({node.id}) and its connected edges.",
                {"id": node.id, "label": node.label, "tenant_id": node.tenant_id},
            )

        if name == "clear_session":
            dry_run = bool(arguments.get("dry_run", False))
            if not dry_run:
                self._require_clear_confirmation(arguments, "clear_session")
            cleared = graph.clear_session(session_id=arguments["session_id"], dry_run=dry_run)
            prefix = "[Preview] Would clear" if dry_run else "Cleared"
            verb = "Would delete" if dry_run else "Deleted"
            return self._ok(
                f"{prefix} session '{cleared.session_id}'. {verb} {cleared.deleted_nodes} node(s), "
                f"{cleared.deleted_edges} edge(s), and {cleared.deleted_transcripts} transcript record(s).",
                self._clear_scope_payload(cleared),
            )

        if name == "clear_project":
            dry_run = bool(arguments.get("dry_run", False))
            if not dry_run:
                self._require_clear_confirmation(arguments, "clear_project")
            cleared = graph.clear_project(project=arguments["project"], dry_run=dry_run)
            prefix = "[Preview] Would clear" if dry_run else "Cleared"
            verb = "Would delete" if dry_run else "Deleted"
            return self._ok(
                f"{prefix} project '{cleared.project}'. {verb} {cleared.deleted_nodes} node(s), "
                f"{cleared.deleted_edges} edge(s), and {cleared.deleted_transcripts} transcript record(s).",
                self._clear_scope_payload(cleared),
            )

        if name == "clear_all":
            dry_run = bool(arguments.get("dry_run", False))
            if not dry_run:
                self._require_clear_confirmation(arguments, "clear_all")
            cleared = graph.clear_all(dry_run=dry_run)
            prefix = "[Preview] Would clear" if dry_run else "Cleared"
            verb = "Would delete" if dry_run else "Deleted"
            return self._ok(
                f"{prefix} all graph memory data for tenant '{graph.tenant_id}'. {verb} {cleared.deleted_nodes} node(s), "
                f"{cleared.deleted_edges} edge(s), and {cleared.deleted_transcripts} transcript record(s).",
                self._clear_scope_payload(cleared),
            )

        if name == "decompose_and_store":
            subgraph = graph.decompose_and_store(content=arguments["content"], context=arguments.get("context", ""))
            return self._ok(serialize_subgraph(subgraph), self._subgraph_payload(subgraph))

        if name == "observe_conversation":
            observation = graph.observe_conversation(
                user_message=arguments["user_message"],
                assistant_response=arguments["assistant_response"],
                agent_id=arguments.get("agent_id", ""),
                project=arguments.get("project", ""),
                session_id=arguments.get("session_id", ""),
            )
            return self._ok(serialize_observation_result(observation), self._observation_payload(observation))

        if name == "graph_diff":
            diff = graph.graph_diff(since=arguments.get("since", "24h"))
            return self._ok(serialize_graph_diff(diff), self._graph_diff_payload(diff))

        if name == "prime_context":
            context_result = graph.prime_context(
                project=arguments.get("project", ""),
                agent_id=arguments.get("agent_id", ""),
                session_id=arguments.get("session_id", ""),
            )
            return self._ok(serialize_prime_context(context_result), self._prime_context_payload(context_result))

        if name == "get_topics":
            topics = graph.get_topics()
            return self._ok(serialize_topics(topics), self._topic_payload(topics))

        if name == "get_stats":
            stats = graph.get_stats()
            em = graph.embedding_model
            stats_payload = self._stats_payload(stats)
            embedding_status = getattr(em, "warmup_status", "unknown")
            embedding_error = getattr(em, "warmup_error", "")
            stats_payload["embedding_status"] = embedding_status
            if embedding_error:
                stats_payload["embedding_error"] = embedding_error
            stats_payload["startup_mode"] = self.config.startup_mode
            return self._ok(
                serialize_stats(stats)
                + f"\nEmbedding status: {embedding_status}"
                + (f" (error: {embedding_error}" + ")" if embedding_error else ""),
                stats_payload,
            )

        if name == "export_graph_html":
            output_path = graph.export_graph_html(
                output_path=arguments.get("output_path"),
                include_physics=bool(arguments.get("include_physics", True)),
            )
            stats = graph.get_stats()
            return self._ok(
                f"Exported graph visualization to {output_path}.",
                {
                    "output_path": str(output_path),
                    "tenant_id": graph.tenant_id,
                    "total_nodes": stats.total_nodes,
                    "total_edges": stats.total_edges,
                },
            )

        if name == "window_graph_viz":
            output_path = graph.export_window_graph_html(
                project=arguments.get("project", ""),
                output_path=arguments.get("output_path"),
                include_physics=bool(arguments.get("include_physics", True)),
            )
            windows = graph.list_context_windows(project=arguments.get("project", ""), limit=10_000)
            edge_count = sum(len(graph.get_context_window_edges(w.id)) for w in windows)
            return self._ok(
                f"Exported context-window graph visualization to {output_path}.",
                {
                    "output_path": str(output_path),
                    "tenant_id": graph.tenant_id,
                    "project": arguments.get("project", ""),
                    "total_context_windows": len(windows),
                    "total_context_window_edges": edge_count,
                },
            )

        if name == "commit":
            commit_format = arguments.get("commit_format", "abhi")
            if commit_format == "backup":
                backup = graph.export_graph_backup(output_path=arguments.get("output_path"))
                return self._ok(
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
            if commit_format == "bundle":
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
                return self._ok(serialize_context_bundle_export(exported), self._context_bundle_payload(exported))
            # abhi format (default)
            from waggle.server.utils import _assert_export_safe

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
                    filter_summary = (
                        f" ({filtered_count} low-confidence RELATES_TO edges filtered from {total_count} total)"
                    )
            return self._ok(
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

        if name == "pull":
            pull_format = arguments.get("pull_format", "abhi")
            if pull_format == "backup":
                imported = graph.import_graph_backup(input_path=arguments["input_path"])
                return self._ok(
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
            imported = graph.import_abhi(input_path=arguments["input_path"])
            return self._ok(
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

        if name == "diff":
            diff = graph.diff_abhi(
                input_path_a=arguments["input_path_a"],
                input_path_b=arguments["input_path_b"],
            )
            return self._ok(serialize_abhi_diff(diff), diff.model_dump(mode="json"))

        if name == "merge":
            merged = graph.merge_abhi(
                base_input_path=arguments["base_input_path"],
                left_input_path=arguments["left_input_path"],
                right_input_path=arguments["right_input_path"],
                output_path=arguments["output_path"],
                merge_strategy=arguments.get("merge_strategy", "prefer_right"),
            )
            return self._ok(serialize_abhi_merge(merged), merged.model_dump(mode="json"))

        if name == "grep":
            queried = graph.query_abhi(
                input_path=arguments["input_path"],
                query_id=arguments.get("query_id", ""),
                query_text=arguments.get("query_text", ""),
            )
            return self._ok(serialize_abhi_query(queried), queried.model_dump(mode="json"))

        if name == "load_abhi_chunks":
            loaded = graph.load_abhi_chunks(
                input_path=arguments["input_path"],
                chunk_ids=list(arguments.get("chunk_ids", [])),
                query_id=arguments.get("query_id", ""),
                query_text=arguments.get("query_text", ""),
            )
            return self._ok(serialize_abhi_chunk_load(loaded), loaded.model_dump(mode="json"))

        if name == "fsck":
            validation = graph.validate_abhi(input_path=arguments["input_path"])
            return self._ok(serialize_abhi_validation(validation), validation.model_dump(mode="json"))

        if name == "show":
            inspection = graph.inspect_abhi(input_path=arguments["input_path"])
            return self._ok(serialize_abhi_inspect(inspection), inspection.model_dump(mode="json"))

        if name == "export_markdown_vault":
            exported = graph.export_markdown_vault(
                root_path=arguments["root_path"],
                project=arguments.get("project", ""),
                agent_id=arguments.get("agent_id", ""),
                session_id=arguments.get("session_id", ""),
            )
            return self._ok(
                f"Exported Markdown vault to {exported.root_path}.",
                self._markdown_vault_export_payload(exported),
            )

        if name == "import_markdown_vault":
            imported = graph.import_markdown_vault(root_path=arguments["root_path"])
            return self._ok(
                f"Imported Markdown vault from {imported.root_path}.",
                self._markdown_vault_import_payload(imported),
            )

        if name == "edge_quality_report":
            report = graph.edge_quality_report(
                agent_id=arguments.get("agent_id", ""),
                project=arguments.get("project", ""),
                session_id=arguments.get("session_id", ""),
            )
            lines = [f"Edge quality report: {report['total_edges']} edges across {report['total_edge_types']} type(s)."]
            for rel, stats in sorted(report.get("by_type", {}).items()):
                lines.append(f"  {rel}: count={stats['count']} avg_confidence={stats['avg_confidence']:.3f}")
            return self._ok("\n".join(lines), report)

        if name == "build_context":
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
                    (t.model_dump() if hasattr(t, "model_dump") else str(t)) for t in ctx_result.transcript_evidence
                ],
                "conflicts": ctx_result.conflicts,
                "token_estimate": ctx_result.token_estimate,
                "debug": ctx_result.debug,
            }
            return self._ok(ctx_result.context_pack, payload)

        raise ValidationFailure(f"Unknown tool: {name}")

    # ── Result constructors ───────────────────────────────────────────────

    @staticmethod
    def _ok(text: str, structured: dict[str, Any] | list[Any]) -> WaggleToolResult:
        return WaggleToolResult(text=text, structured=structured, is_error=False)

    @staticmethod
    def _error_result(exc: Exception) -> WaggleToolResult:
        if isinstance(exc, WaggleError):
            return WaggleToolResult(
                text=f"Error [{exc.code}]: {exc}",
                structured={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "error_code": exc.code,
                    "status_code": exc.status_code,
                },
                is_error=True,
                error_code=exc.code,
                status_code=exc.status_code,
            )
        return WaggleToolResult(
            text=f"Error: {exc}",
            structured={"error": str(exc), "error_type": type(exc).__name__},
            is_error=True,
        )

    @staticmethod
    def _require_clear_confirmation(arguments: dict[str, Any], command_name: str) -> None:
        if bool(arguments.get("confirm", False)):
            return
        raise ValidationFailure(f"{command_name} is destructive and requires confirm=true.")

    # ── Fast-mode guard ───────────────────────────────────────────────────

    def _check_embedding_available(self, name: str, graph: Any, arguments: dict[str, Any]) -> WaggleToolResult | None:
        if not self.config.is_fast_mode:
            return None
        if name in EMBEDDING_FREE_TOOLS:
            return None
        em = graph.embedding_model
        if em.warmup_status in (STATUS_READY,):
            return None
        retrieval_mode = (
            arguments.get("retrieval_mode", "") if name in ("query_graph", "export_context_bundle", "commit") else ""
        )
        if retrieval_mode in ("verbatim", "lexical"):
            return None
        return self._ok(
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

    # ── Payload serialization helpers ─────────────────────────────────────

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

    def _subgraph_payload(self, result: Any) -> dict[str, Any]:
        return {
            "query": result.query,
            "retrieval_mode": result.retrieval_mode,
            "total_nodes_in_graph": result.total_nodes_in_graph,
            "nodes": [self._node_payload(n) for n in result.nodes],
            "edges": [self._edge_payload(e) for e in result.edges],
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

    def _observation_payload(self, result: Any) -> dict[str, Any]:
        return {
            "turn_id": result.turn_id,
            "verbatim_stored": result.verbatim_stored,
            "nodes_extracted": result.nodes_extracted,
            "edges_inferred": result.edges_inferred,
            "extraction_errors": result.extraction_errors,
            "stored_nodes": [self._node_payload(n) for n in result.stored_nodes],
            "created_count": result.created_count,
            "reused_count": result.reused_count,
            "conflicts": [
                {
                    "other_node_id": c.other_node_id,
                    "other_node_label": c.other_node_label,
                    "relationship": c.relationship,
                    "reason": c.reason,
                }
                for c in result.conflicts
            ],
        }

    def _node_history_payload(self, result: Any) -> dict[str, Any]:
        return {
            "node": self._node_payload(result.node),
            "related_nodes": [self._node_payload(n) for n in result.related_nodes],
            "edges": [self._edge_payload(e) for e in result.edges],
        }

    def _timeline_payload(self, result: Any) -> dict[str, Any]:
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

    def _conflict_entry_payload(self, entry: Any) -> dict[str, Any]:
        return {
            "edge": self._edge_payload(entry.edge),
            "source_node": self._node_payload(entry.source_node),
            "target_node": self._node_payload(entry.target_node),
            "resolved": entry.resolved,
            "resolution_note": entry.resolution_note,
            "resolved_at": entry.resolved_at.isoformat() if entry.resolved_at is not None else None,
        }

    def _conflict_list_payload(self, result: Any) -> dict[str, Any]:
        return {
            "include_resolved": result.include_resolved,
            "conflicts": [self._conflict_entry_payload(e) for e in result.conflicts],
        }

    def _context_scope_payload(self, result: Any) -> dict[str, Any]:
        return {
            "agent_ids": result.agent_ids,
            "projects": result.projects,
            "session_ids": result.session_ids,
        }

    def _clear_scope_payload(self, result: Any) -> dict[str, Any]:
        return result.model_dump(mode="json")

    def _context_window_payload(self, window: Any) -> dict[str, Any]:
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

    def _context_window_edge_payload(self, edge: Any) -> dict[str, Any]:
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

    def _graph_diff_payload(self, result: Any) -> dict[str, Any]:
        return {
            "since": result.since,
            "generated_at": result.generated_at.isoformat(),
            "added_nodes": [self._node_payload(n) for n in result.added_nodes],
            "updated_nodes": [self._node_payload(n) for n in result.updated_nodes],
            "created_edges": [self._edge_payload(e) for e in result.created_edges],
            "contradiction_edges": [self._edge_payload(e) for e in result.contradiction_edges],
        }

    def _prime_context_payload(self, result: Any) -> dict[str, Any]:
        return {
            "project": result.project,
            "summary": result.summary,
            "total_nodes_in_graph": result.total_nodes_in_graph,
            "nodes": [self._node_payload(n) for n in result.nodes],
            "edges": [self._edge_payload(e) for e in result.edges],
        }

    def _context_bundle_payload(self, result: Any) -> dict[str, Any]:
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

    def _topic_payload(self, result: Any) -> dict[str, Any]:
        return {
            "total_clusters": result.total_clusters,
            "clusters": [
                {
                    "cluster_id": cluster.cluster_id,
                    "label": cluster.label,
                    "node_count": cluster.node_count,
                    "top_tags": cluster.top_tags,
                    "nodes": [self._node_payload(n) for n in cluster.nodes],
                }
                for cluster in result.clusters
            ],
        }

    def _stats_payload(self, stats: Any) -> dict[str, Any]:
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
                    "id": n.id,
                    "label": n.label,
                    "node_type": n.node_type.value,
                    "connection_count": n.connection_count,
                }
                for n in stats.most_connected_nodes
            ],
            "most_recent_nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "node_type": n.node_type.value,
                    "updated_at": n.updated_at.isoformat(),
                }
                for n in stats.most_recent_nodes
            ],
        }

    def _markdown_vault_export_payload(self, result: Any) -> dict[str, Any]:
        return {
            "root_path": result.root_path,
            "tenant_id": result.tenant_id,
            "project": result.project,
            "node_count": result.node_count,
            "edge_count": result.edge_count,
            "files_written": result.files_written,
        }

    def _markdown_vault_import_payload(self, result: Any) -> dict[str, Any]:
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

    def _record_graph_size(self, graph: Any) -> None:
        try:
            stats = graph.get_stats()
            tenant_id = getattr(graph, "tenant_id", self.config.default_tenant_id)
            self.metrics.set_gauge("waggle_graph_nodes", stats.total_nodes, tenant_id=tenant_id)
            self.metrics.set_gauge("waggle_graph_edges", stats.total_edges, tenant_id=tenant_id)
        except Exception:
            LOGGER.debug("graph_size_metrics_failed", exc_info=True)
