from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.models import InitializationOptions

from waggle import __version__
from waggle.config import AppConfig
from waggle.runtime_info import SERVER_NAME, WAGGLE_SERVER_INFO
from waggle.embeddings import EMBEDDING_FREE_TOOLS, STATUS_DISABLED, STATUS_READY
from waggle.errors import ValidationFailure
from waggle.metrics import MetricsRegistry
from waggle.models import (
    NodeType,
    RelationType,
    Node,
)
from waggle.rate_limit import RateLimiter
from waggle.recursive_context import (
    RECURSIVE_CONTEXT_ENABLED,
    RecursiveContextController,
)
from waggle.runtime_context import runtime_context
from waggle.abhi import serialize_abhi_diff
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

from .utils import (
    WRITE_HEAVY_TOOLS,
    _assert_export_safe,
    _build_backend,
    _object_input_schema,
    _scope_properties,
)

LOGGER = logging.getLogger(__name__)

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


class WaggleServer:
    """MCP server wrapper with tenant-aware graph resolution."""

    def __init__(
        self,
        graph: Any | None = None,
        *,
        config: AppConfig | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        from starlette.requests import Request
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

    def _get_request(self) -> Any:
        from starlette.requests import Request
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
            LOGGER.info(
                "startup_fast_mode",
                extra={"startup_mode": self.config.startup_mode},
            )
        elif self.config.is_strict_mode:
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
                    topics = graph.get_topics()
                    result = self._tool_result(serialize_topics(topics), self._topic_payload(topics))
                elif name == "get_stats":
                    stats = graph.get_stats()
                    em = graph.embedding_model
                    stats_payload = self._stats_payload(stats)
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
                    diff = graph.diff_abhi(
                        input_path_a=arguments["input_path_a"],
                        input_path_b=arguments["input_path_b"],
                    )
                    result = self._tool_result(
                        serialize_abhi_diff(diff),
                        diff.model_dump(mode="json"),
                    )
                elif name == "merge":
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
                    validation = graph.validate_abhi(input_path=arguments["input_path"])
                    result = self._tool_result(
                        serialize_abhi_validation(validation),
                        validation.model_dump(mode="json"),
                    )
                elif name == "show":
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
                    status="failure",
                    tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
                )
                self.metrics.observe("waggle_tool_latency_seconds", elapsed, tool=name)
                LOGGER.exception("tool_call_failed")
                return self._error_result(exc)

    @staticmethod
    def _tool_result(text: str, data: dict[str, Any] | list[Any]) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=data,
            isError=False,
        )

    def _error_result(self, exc: Exception) -> types.CallToolResult:
        from waggle.errors import WaggleError
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

    def _subgraph_payload(self, result: Any) -> dict[str, Any]:
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

    def _observation_payload(self, result: Any) -> dict[str, Any]:
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

    def _node_history_payload(self, result: Any) -> dict[str, Any]:
        return {
            "node": self._node_payload(result.node),
            "related_nodes": [self._node_payload(node) for node in result.related_nodes],
            "edges": [self._edge_payload(edge) for edge in result.edges],
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
            "conflicts": [self._conflict_entry_payload(entry) for entry in result.conflicts],
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
            "added_nodes": [self._node_payload(node) for node in result.added_nodes],
            "updated_nodes": [self._node_payload(node) for node in result.updated_nodes],
            "created_edges": [self._edge_payload(edge) for edge in result.created_edges],
            "contradiction_edges": [self._edge_payload(edge) for edge in result.contradiction_edges],
        }

    def _prime_context_payload(self, result: Any) -> dict[str, Any]:
        return {
            "project": result.project,
            "summary": result.summary,
            "total_nodes_in_graph": result.total_nodes_in_graph,
            "nodes": [self._node_payload(node) for node in result.nodes],
            "edges": [self._edge_payload(edge) for edge in result.edges],
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
                    "nodes": [self._node_payload(node) for node in cluster.nodes],
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
            from waggle.errors import PayloadTooLargeError
            raise PayloadTooLargeError(f"{field_name} exceeds the configured payload limit.")

    def _record_graph_size(self, graph: Any) -> None:
        stats = graph.get_stats()
        tenant_id = getattr(graph, "tenant_id", self.config.default_tenant_id)
        self.metrics.set_gauge("waggle_graph_nodes", stats.total_nodes, tenant_id=tenant_id)
        self.metrics.set_gauge("waggle_graph_edges", stats.total_edges, tenant_id=tenant_id)
