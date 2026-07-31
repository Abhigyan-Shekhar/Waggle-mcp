"""MCP v2 resource and prompt surface for Waggle.

The v2 server path owns this surface directly so it does not depend on the
legacy server compatibility module.
"""
from __future__ import annotations

from typing import Any

import mcp.types as types

from waggle.errors import ValidationFailure
from waggle.serializer import serialize_recent_nodes, serialize_stats

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


def build_resources() -> types.ListResourcesResult:
    """Return Waggle's static MCP resource catalogue."""
    return types.ListResourcesResult(
        resources=[
            types.Resource(
                uri="graph://stats",
                name="Graph Stats",
                description="Current graph statistics.",
                mime_type="text/plain",
            ),
            types.Resource(
                uri="graph://recent",
                name="Recent Graph Nodes",
                description="The 10 most recently updated nodes.",
                mime_type="text/plain",
            ),
            types.Resource(
                uri="graph://windows",
                name="Context Windows",
                description="Recent context windows grouped by project/session.",
                mime_type="text/plain",
            ),
            types.Resource(
                uri="graph://memory-policy",
                name="Automatic Memory Policy",
                description="Policy for when assistants should retrieve and write Waggle memory automatically.",
                mime_type="text/plain",
            ),
        ]
    )


def read_resource_text(graph: Any, uri: str) -> str:
    """Render one Waggle resource using the provided tenant graph."""
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
                f"- {window.id} [{window.status}] session={window.session_id} "
                f"nodes={window.node_count} updated={window.updated_at.isoformat()}"
            )
        lines.append("=== End Context Windows ===")
        return "\n".join(lines)
    if uri == "graph://memory-policy":
        return MEMORY_AUTOMATION_POLICY
    raise ValidationFailure(f"Unknown resource: {uri}")


def build_prompts() -> list[types.Prompt]:
    """Return Waggle's static MCP prompt catalogue."""
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


def get_prompt_result(name: str, arguments: dict[str, str]) -> types.GetPromptResult:
    """Render one Waggle prompt."""
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
