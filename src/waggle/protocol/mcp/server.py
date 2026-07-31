"""Factory for a properly configured MCP SDK v2 ``Server``.

Produces a ``mcp.server.lowlevel.Server`` wired with all Waggle handlers
via the ``on_*`` constructor pattern (MCP 2026-07-28 / SDK v2).

Usage — stdio::

    server, _ = build_waggle_server(config=config)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

Usage — HTTP (ASGI)::

    server, _ = build_waggle_server(config=config)
    app = server.streamable_http_app()

The factory returns ``(server, adapter)`` so callers can inspect the adapter
(e.g. for re-pointing the graph in multi-tenant deployments) and for testing.
"""
from __future__ import annotations

import logging
from typing import Any

from mcp.server.lowlevel import Server

from waggle.config import AppConfig
from waggle.metrics import MetricsRegistry
from waggle.tools.dispatcher import WaggleToolDispatcher

from .adapter import WagglemcpAdapter

LOGGER = logging.getLogger(__name__)


def build_waggle_server(
    graph: Any | None = None,
    *,
    config: AppConfig | None = None,
    metrics: MetricsRegistry | None = None,
) -> tuple[Server[Any], WagglemcpAdapter]:
    """Build an MCP SDK v2 ``Server`` wired with all Waggle tool, resource, and prompt handlers.

    Parameters
    ----------
    graph:
        A pre-resolved memory graph.  When ``None`` the factory resolves the
        default backend from ``config`` (same behaviour as ``WaggleServer``).
    config:
        Waggle application config.  Defaults to ``AppConfig.from_env()``.
    metrics:
        Metrics registry.  Defaults to a fresh ``MetricsRegistry()``.

    Returns
    -------
    (server, adapter)
        ``server`` is the ready-to-run MCP v2 ``Server`` instance.
        ``adapter`` is the ``WagglemcpAdapter`` for inspection and multi-tenant
        graph re-pointing.
    """
    from waggle.server.utils import _build_backend  # avoid circular import at module level

    cfg = config or AppConfig.from_env()
    mtr = metrics or MetricsRegistry()
    root_graph = graph or _build_backend(cfg)

    dispatcher = WaggleToolDispatcher(graph=root_graph, config=cfg, metrics=mtr)
    adapter = WagglemcpAdapter(dispatcher=dispatcher)

    server: Server[Any] = Server(
        "waggle",
        version=cfg.version if hasattr(cfg, "version") else "",
        # ── Tool handlers ───────────────────────────────────────────────
        on_list_tools=adapter.on_list_tools,
        on_call_tool=adapter.on_call_tool,
        # ── Resource handlers ───────────────────────────────────────────
        on_list_resources=adapter.on_list_resources,
        on_read_resource=adapter.on_read_resource,
        # ── Prompt handlers ─────────────────────────────────────────────
        on_list_prompts=adapter.on_list_prompts,
        on_get_prompt=adapter.on_get_prompt,
    )

    LOGGER.info(
        "waggle_mcp_v2_server_built",
        extra={
            "tool_count": len(dispatcher.list_tools()),
            "graph_backend": cfg.backend,
        },
    )

    return server, adapter
