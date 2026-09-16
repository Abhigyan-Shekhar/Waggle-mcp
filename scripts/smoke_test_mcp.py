from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    smoke_root = Path(tempfile.mkdtemp(prefix="waggle-mcp-smoke-"))
    db_path = smoke_root / "smoke-test-memory.db"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["WAGGLE_DB_PATH"] = str(db_path)
    env["WAGGLE_STARTUP_MODE"] = env.get("WAGGLE_STARTUP_MODE", "normal")
    env["WAGGLE_BUNDLED_RUNTIME"] = env.get("WAGGLE_BUNDLED_RUNTIME", "1")
    env["WAGGLE_MODEL"] = env.get("WAGGLE_MODEL", "deterministic")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.entrypoints.server_only", "serve", "--transport", "stdio"],
        cwd=str(ROOT),
        env=env,
    )

    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        init_result = await session.initialize()
        print(f"initialized: {init_result.server_info.name}")

        store_result = await session.call_tool(
            "store_node",
            {
                "label": "Smoke Test Preference",
                "content": "The user prefers graph memory over flat summaries.",
                "node_type": "preference",
                "tags": ["smoke-test", "memory"],
            },
        )
        if store_result.is_error:
            raise RuntimeError(store_result.content[0].text)
        print(store_result.content[0].text)

        query_result = await session.call_tool(
            "query_graph",
            {
                "query": "What does the user prefer about memory?",
                "max_nodes": 5,
                "max_depth": 1,
                "retrieval_mode": "graph",
            },
        )
        if query_result.is_error:
            raise RuntimeError(query_result.content[0].text)
        print()
        print(query_result.content[0].text)

        resource_result = await session.read_resource("graph://stats")
        print()
        print(resource_result.contents[0].text)


if __name__ == "__main__":
    asyncio.run(main())
