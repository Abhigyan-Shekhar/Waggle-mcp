from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    import mcp  # type: ignore
except ModuleNotFoundError:
    # Provide a minimal stub for `mcp` to allow imports during tests when the
    # external `mcp` package is not available in the test environment.
    import types

    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    stdio = types.ModuleType("mcp.server.stdio")
    client = types.ModuleType("mcp.client")
    session = types.ModuleType("mcp.client.session")
    mcp_types = types.ModuleType("mcp.types")
    lowlevel = types.ModuleType("mcp.server.lowlevel")

    # Provide minimal classes used by imports
    exec("""
class NotificationOptions:
    pass

class Server:
    def __init__(self, *args, **kwargs):
        pass
""", lowlevel.__dict__)

    # Attach submodules
    mcp.server = server
    server.stdio = stdio
    mcp.client = client
    client.session = session
    mcp.types = mcp_types
    server.lowlevel = lowlevel

    # Create submodule mcp.server.lowlevel.server and provide `request_ctx`
    lowlevel_server = types.ModuleType("mcp.server.lowlevel.server")
    lowlevel_server.request_ctx = None
    server.lowlevel.server = lowlevel_server

    # mcp.server.models stub
    models = types.ModuleType("mcp.server.models")
    exec("""
class InitializationOptions:
    def __init__(self, *args, **kwargs):
        pass
""", models.__dict__)
    server.models = models

    # Insert into sys.modules so normal imports succeed
    sys.modules["mcp"] = mcp
    sys.modules["mcp.server"] = server
    sys.modules["mcp.server.stdio"] = stdio
    sys.modules["mcp.client"] = client
    sys.modules["mcp.client.session"] = session
    sys.modules["mcp.types"] = mcp_types
    sys.modules["mcp.server.lowlevel"] = lowlevel
    sys.modules["mcp.server.lowlevel.server"] = lowlevel_server
    sys.modules["mcp.server.models"] = models
