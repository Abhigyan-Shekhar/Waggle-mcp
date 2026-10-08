"""Protocol-independent tool definition used to build the tool catalogue.

``WaggleToolDefinition`` stores the tool metadata in Waggle-native form.
MCP adapters leave ``input_schema`` in SDK v2 snake-case. The legacy
compatibility shell translates it to ``inputSchema`` for older callers.
No MCP types appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    annotations: dict[str, Any] | None = None
    """Optional MCP tool annotations such as readOnlyHint and destructiveHint."""
