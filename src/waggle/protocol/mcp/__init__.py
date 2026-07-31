"""MCP 2026-07-28 (SDK v2) protocol adapter for Waggle.

Public surface::

    from waggle.protocol.mcp import build_waggle_server, WagglemcpAdapter

    server, adapter = build_waggle_server(config=cfg)
"""

from .adapter import WagglemcpAdapter
from .server import build_waggle_server

_PACKAGE_DESCRIPTION = "Waggle MCP 2026-07-28 adapter"

__all__ = ["WagglemcpAdapter", "build_waggle_server"]
