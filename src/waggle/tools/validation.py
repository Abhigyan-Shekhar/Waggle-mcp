"""Payload validation helpers for Waggle tool arguments.

Extracted verbatim from ``WaggleServer._validate_tool_payload`` and
``WaggleServer._assert_payload_size``.  No MCP types appear here.

PR 3 adds ``validate_against_schema()`` — JSON Schema 2020-12 structural
validation of tool arguments using ``jsonschema``.
"""

from __future__ import annotations

import logging
from typing import Any

from waggle.errors import PayloadTooLargeError, ValidationFailure

LOGGER = logging.getLogger(__name__)


# ── JSON Schema validation (added PR 3) ──────────────────────────────────────


def validate_against_schema(
    tool_name: str,
    arguments: dict[str, Any],
    input_schema: dict[str, Any],
) -> None:
    """Validate ``arguments`` against the tool's JSON Schema ``input_schema``.

    Uses ``jsonschema.Draft202012Validator`` (JSON Schema 2020-12) which is
    what MCP 2026-07-28 specifies for ``input_schema``.

    Raises ``waggle.errors.ValidationFailure`` if validation fails.
    Does nothing if ``jsonschema`` is not installed (fails gracefully).
    """
    try:
        from jsonschema import Draft202012Validator
        from jsonschema import ValidationError as JsonSchemaError
    except ImportError:
        LOGGER.debug("jsonschema not installed; skipping schema validation for %s", tool_name)
        return

    try:
        Draft202012Validator(input_schema).validate(arguments)
    except JsonSchemaError as exc:
        raise ValidationFailure(f"Invalid arguments for tool '{tool_name}': {exc.message}") from exc


# ── Field-level payload-size validation ────────────────────────────────────────


def assert_payload_size(value: Any, limit: int, field_name: str) -> None:
    """Raise ``PayloadTooLargeError`` if ``value`` exceeds ``limit`` bytes when encoded as UTF-8."""
    if value is None:
        return
    size = len(str(value).encode("utf-8"))
    if size > limit:
        raise PayloadTooLargeError(f"{field_name} exceeds the configured payload limit.")


def validate_tool_payload(name: str, arguments: dict[str, Any], max_payload_bytes: int) -> None:
    """Run tool-specific payload-size guards.

    This validates *field sizes* only.  JSON Schema structural validation
    (PR 3) is a separate concern handled by ``validate_against_schema()``
    which is called by ``WaggleToolDispatcher.call_tool()`` before this.
    """
    limit = max_payload_bytes
    if name == "store_node":
        assert_payload_size(arguments.get("label", ""), limit, "store_node.label")
        assert_payload_size(arguments.get("content", ""), limit, "store_node.content")
        assert_payload_size(arguments.get("source_prompt", ""), limit, "store_node.source_prompt")
        assert_payload_size(arguments.get("agent_id", ""), limit, "store_node.agent_id")
        assert_payload_size(arguments.get("project", ""), limit, "store_node.project")
        assert_payload_size(arguments.get("session_id", ""), limit, "store_node.session_id")
        return
    if name == "decompose_and_store":
        assert_payload_size(arguments.get("content", ""), limit, "decompose_and_store.content")
        assert_payload_size(arguments.get("context", ""), limit, "decompose_and_store.context")
        return
    if name == "observe_conversation":
        assert_payload_size(arguments.get("user_message", ""), limit, "observe_conversation.user_message")
        assert_payload_size(arguments.get("assistant_response", ""), limit, "observe_conversation.assistant_response")
        assert_payload_size(arguments.get("agent_id", ""), limit, "observe_conversation.agent_id")
        assert_payload_size(arguments.get("project", ""), limit, "observe_conversation.project")
        assert_payload_size(arguments.get("session_id", ""), limit, "observe_conversation.session_id")
        return
    if name == "aggregate_graph":
        assert_payload_size(arguments.get("query", ""), limit, "aggregate_graph.query")
        assert_payload_size(arguments.get("agent_id", ""), limit, "aggregate_graph.agent_id")
        assert_payload_size(arguments.get("project", ""), limit, "aggregate_graph.project")
        assert_payload_size(arguments.get("session_id", ""), limit, "aggregate_graph.session_id")
        return
    if name == "query_graph":
        assert_payload_size(arguments.get("query", ""), limit, "query_graph.query")
        assert_payload_size(arguments.get("agent_id", ""), limit, "query_graph.agent_id")
        assert_payload_size(arguments.get("project", ""), limit, "query_graph.project")
        assert_payload_size(arguments.get("session_id", ""), limit, "query_graph.session_id")
        return
    if name == "debug_retrieval":
        assert_payload_size(arguments.get("query", ""), limit, "debug_retrieval.query")
        assert_payload_size(arguments.get("agent_id", ""), limit, "debug_retrieval.agent_id")
        assert_payload_size(arguments.get("project", ""), limit, "debug_retrieval.project")
        assert_payload_size(arguments.get("session_id", ""), limit, "debug_retrieval.session_id")
        return
    if name in ("export_context_bundle", "commit"):
        assert_payload_size(arguments.get("query", ""), limit, "commit.query")
        assert_payload_size(arguments.get("project", ""), limit, "commit.project")
        assert_payload_size(arguments.get("agent_id", ""), limit, "commit.agent_id")
        assert_payload_size(arguments.get("session_id", ""), limit, "commit.session_id")
        assert_payload_size(arguments.get("output_path", ""), limit, "commit.output_path")
        return
    if name == "window_graph_viz":
        assert_payload_size(arguments.get("project", ""), limit, "window_graph_viz.project")
        assert_payload_size(arguments.get("output_path", ""), limit, "window_graph_viz.output_path")
        return
    if name == "timeline":
        assert_payload_size(arguments.get("query", ""), limit, "timeline.query")
        assert_payload_size(arguments.get("node_id", ""), limit, "timeline.node_id")
        return
    if name == "resolve_conflict":
        assert_payload_size(arguments.get("edge_id", ""), limit, "resolve_conflict.edge_id")
        assert_payload_size(arguments.get("resolution_note", ""), limit, "resolve_conflict.resolution_note")
        assert_payload_size(arguments.get("winner", ""), limit, "resolve_conflict.winner")
