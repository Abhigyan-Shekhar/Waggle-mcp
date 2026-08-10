"""Waggle protocol-independent tool layer.

This package decouples the Waggle memory engine from any specific MCP
protocol version.  The MCP adapter (and future REST / SDK adapters) import
from here; they do not import from waggle.server.mcp directly.
"""
