from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    db_path = ROOT / ".tmp" / "smoke-test-memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["WAGGLE_DB_PATH"] = str(db_path)
    env["WAGGLE_MODEL"] = env.get("WAGGLE_MODEL", "all-MiniLM-L6-v2")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.server"],
        cwd=str(ROOT),
        env=env,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            print(f"initialized: {init_result.serverInfo.name}")

            store_result = await session.call_tool(
                "store_node",
                {
                    "label": "Smoke Test Preference",
                    "content": "The user prefers graph memory over flat summaries.",
                    "node_type": "preference",
                    "tags": ["smoke-test", "memory"],
                },
            )
            print(store_result.content[0].text)

            query_result = await session.call_tool(
                "query_graph",
                {
                    "query": "What does the user prefer about memory?",
                    "max_nodes": 5,
                    "max_depth": 1,
                },
            )
            print()
            print(query_result.content[0].text)

            resource_result = await session.read_resource("graph://stats")
            print()
            print(resource_result.contents[0].text)


if __name__ == "__main__":
    asyncio.run(main())
