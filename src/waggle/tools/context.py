"""Protocol-independent request context passed to every tool call."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class WaggleRequestContext:
    """Carries caller identity without any MCP-specific types.

    The MCP adapter populates this from the incoming ``ServerRequestContext``
    (or from Starlette's ``request.state`` for HTTP).  Any future transport
    (REST, Python SDK, CLI) that calls the dispatcher constructs its own
    context without touching MCP internals.
    """

    request_id: str
    tenant_id: str
    transport: str  # "stdio" | "http" | "test"
    api_key_id: str | None = None
    protocol_version: str | None = None
    client_name: str | None = None
    # Arbitrary extra metadata for audit / tracing; never used for auth.
    extra: dict[str, str] = field(default_factory=dict)
