"""Protocol-independent tool result returned by WaggleToolDispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class WaggleToolResult:
    """The outcome of a single tool call.

    The MCP adapter converts this to a ``CallToolResult``; a future REST
    adapter converts it to a JSON response body.  No MCP types appear here.
    """

    text: str
    """Human-readable summary suitable for LLM consumption."""

    structured: dict[str, object]
    """Machine-readable payload.  Exact keys are tool-specific and stable
    across MCP protocol versions."""

    is_error: bool = False
    """True when the tool failed in a recoverable, expected way (e.g.
    validation failure, auth error, conflict resolution error).  Distinct
    from an uncaught exception, which the adapter surfaces as a JSON-RPC
    error or sanitised internal error."""

    error_code: str | None = None
    """Waggle error code when ``is_error`` is True, e.g. ``'validation_failed'``."""

    status_code: int = 200
    """HTTP-equivalent status (200 for success, 4xx for client errors).
    Used by adapters that care about HTTP semantics."""

    extra: dict[str, object] = field(default_factory=dict)
    """Adapter-specific pass-through metadata (e.g. metrics tags)."""
