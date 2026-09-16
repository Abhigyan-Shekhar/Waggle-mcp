"""Stdio transport runner for Waggle's MCP SDK v2 server."""

from __future__ import annotations

import logging
import os

import mcp.server.stdio
from mcp.server.lowlevel import NotificationOptions

from waggle.config import AppConfig
from waggle.runtime_info import WAGGLE_SERVER_INFO

from .server import build_waggle_server

LOGGER = logging.getLogger(__name__)


async def run_waggle_stdio(config: AppConfig) -> None:
    """Run Waggle over stdio using the SDK v2 low-level server."""
    server, adapter = build_waggle_server(config=config)
    graph = adapter.graph
    em = graph.embedding_model
    is_bundled_runtime = os.environ.get("WAGGLE_BUNDLED_RUNTIME", "").strip() in {"1", "true", "yes"}

    if (
        not is_bundled_runtime
        and not config.is_fast_mode
        and hasattr(em, "start_background_warmup")
        and not getattr(em, "_warmup_started", False)
    ):
        em.start_background_warmup()

    if config.is_strict_mode:
        LOGGER.info("stdio_strict_mode_waiting_for_embedding", extra={"model": em.model_name})
        if hasattr(em, "_ready_event"):
            em._ready_event.wait(timeout=120.0)
        LOGGER.info(
            "stdio_strict_mode_embedding_status",
            extra={"status": getattr(em, "warmup_status", "unknown"), "error": getattr(em, "warmup_error", "")},
        )

    initialization_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={"waggle_server_info": WAGGLE_SERVER_INFO},
    )
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, initialization_options)
