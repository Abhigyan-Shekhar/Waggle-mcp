from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_server_stdio_initialize_and_basic_calls(tmp_path: Path) -> None:
    await asyncio.wait_for(_run_stdio_initialize_and_basic_calls(tmp_path), timeout=30.0)


async def _run_stdio_initialize_and_basic_calls(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["WAGGLE_DB_PATH"] = str(tmp_path / "integration-memory.db")
    env["WAGGLE_MODEL"] = "deterministic"
    env["WAGGLE_STARTUP_MODE"] = "fast"
    env["WAGGLE_BUNDLED_RUNTIME"] = "1"

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.entrypoints.server_only", "serve", "--transport", "stdio"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
    )

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        assert init_result.serverInfo.name == "waggle"

        tools_result = await session.list_tools()
        assert len(tools_result.tools) == 41
        assert {tool.name for tool in tools_result.tools} >= {
            "store_node",
            "query_graph",
            "debug_retrieval",
            "clear_session",
            "clear_project",
            "clear_all",
            "list_context_scopes",
            "list_context_windows",
            "get_context_window",
            "close_context_window",
            "get_node_history",
            "timeline",
            "list_conflicts",
            "resolve_conflict",
            "observe_conversation",
            "graph_diff",
            "prime_context",
            "get_topics",
            "get_stats",
            "export_graph_html",
            "window_graph_viz",
            # git-vocabulary canonical names (legacy aliases resolve via _TOOL_ALIASES)
            "commit",
            "pull",
            "diff",
            "merge",
            "grep",
            "fsck",
            "show",
            "load_abhi_chunks",
            "export_markdown_vault",
            "import_markdown_vault",
            # recursive context assembly
            "build_context",
        }

        resources_result = await session.list_resources()
        assert len(resources_result.resources) == 4
        assert {str(resource.uri) for resource in resources_result.resources} == {
            "graph://stats",
            "graph://recent",
            "graph://windows",
            "graph://memory-policy",
        }

        prompts_result = await session.list_prompts()
        assert {prompt.name for prompt in prompts_result.prompts} == {"waggle_memory_policy"}

        stats_result = await session.call_tool("get_stats", {})
        assert "Memory Graph Stats" in stats_result.content[0].text

        resource_result = await session.read_resource("graph://stats")
        assert "Memory Graph Stats" in resource_result.contents[0].text

        policy_resource = await session.read_resource("graph://memory-policy")
        assert "The user should not manually manage memory" in policy_resource.contents[0].text
        assert "If memory looks empty" in policy_resource.contents[0].text
