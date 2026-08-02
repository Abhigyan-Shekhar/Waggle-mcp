"""Protocol-independent tool definition used to build the tool catalogue.

``WaggleToolDefinition`` stores the tool metadata in Waggle-native form.
MCP adapters leave ``input_schema`` in SDK v2 snake-case. The legacy
compatibility shell translates it to ``inputSchema`` for older callers.
No MCP types appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

READ_ONLY_TOOLS = frozenset(
    {
        "aggregate_graph",
        "query_graph",
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
        "diff",
        "grep",
        "load_abhi_chunks",
        "fsck",
        "show",
        "edge_quality_report",
        "dedup_candidates",
        "build_context",
        "recursive_context",
        "assemble_context",
        "rlm_context",
    }
)

DESTRUCTIVE_TOOLS = frozenset(
    {
        "delete_node",
        "clear_session",
        "clear_project",
        "clear_all",
        "pull",
        "merge",
        "import_markdown_vault",
    }
)

IDEMPOTENT_WRITE_TOOLS = frozenset(
    {
        "canonicalize_node",
        "close_context_window",
        "resolve_conflict",
        "update_node",
    }
)


def title_for_tool_name(name: str) -> str:
    """Return a human-readable title for an MCP tool name."""
    overrides = {
        "fsck": "Validate Memory File",
        "grep": "Query Memory File",
    }
    if name in overrides:
        return overrides[name]
    return name.replace("_", " ").title()


def annotations_for_tool_name(name: str) -> dict[str, Any]:
    """Return Claude/MCP review annotations for a tool.

    MCP SDK v2 uses snake_case field names and serializes them to the wire
    aliases expected by clients, such as ``readOnlyHint``.
    """
    read_only = name in READ_ONLY_TOOLS
    destructive = name in DESTRUCTIVE_TOOLS
    return {
        "title": title_for_tool_name(name),
        "read_only_hint": read_only,
        "destructive_hint": destructive if not read_only else False,
        "idempotent_hint": name in IDEMPOTENT_WRITE_TOOLS,
        "open_world_hint": False,
    }


@dataclass(slots=True)
class WaggleToolDefinition:
    """A single tool's metadata as known to the Waggle dispatcher."""

    name: str
    description: str
    input_schema: dict[str, Any]
    """JSON Schema for the tool's arguments.

    Uses Waggle-native naming (``input_schema``). The SDK v2 adapter emits it
    as ``input_schema``; the legacy compatibility shell emits it as
    ``inputSchema``. Waggle's own internal logic never reads these field names
    from the wire.
    """
    title: str = ""
    """Human-readable display title for MCP clients and connector directories."""
    annotations: dict[str, Any] | None = None
    """MCP tool annotations such as read-only/destructive hints."""

    def __post_init__(self) -> None:
        if not self.title:
            self.title = title_for_tool_name(self.name)
        if self.annotations is None:
            self.annotations = annotations_for_tool_name(self.name)
