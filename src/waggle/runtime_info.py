from __future__ import annotations

import json

from waggle import __version__

SERVER_NAME = "waggle"
# Keep this metadata path import-light; the bundled runtime launcher uses it for
# fast health checks before loading the MCP server stack.
MIN_SUPPORTED_MCP_PROTOCOL_VERSION = "2025-06-18"
WAGGLE_SERVER_INFO = {
    "name": SERVER_NAME,
    "version": __version__,
    "minimum_supported_protocol_version": MIN_SUPPORTED_MCP_PROTOCOL_VERSION,
    "runtime_scope": "mcp-server-stdio",
}


def server_info_json() -> str:
    return json.dumps(WAGGLE_SERVER_INFO, sort_keys=True)
