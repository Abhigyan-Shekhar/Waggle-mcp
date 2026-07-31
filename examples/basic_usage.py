#!/usr/bin/env python3
"""Minimal end-to-end Waggle memory workflow without an MCP client.

Run from the repository root (after ``pip install -e ".[dev]"``):

    WAGGLE_MODEL=deterministic python examples/basic_usage.py

This mirrors the README's PostgreSQL vs MySQL decision story: store a turn,
query it in a "fresh" session, then record a contradicting correction.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


def _summarize_nodes(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return "(none)"
    return ", ".join(f"{n.get('label', '?')} [{n.get('node_type', '?')}]" for n in nodes)


def _build_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["WAGGLE_DB_PATH"] = str(db_path)
    env["WAGGLE_LOG_LEVEL"] = os.environ.get("WAGGLE_LOG_LEVEL", "ERROR")
    env["WAGGLE_MODEL"] = os.environ.get("WAGGLE_MODEL", "deterministic")
    return env


def _open_session(db_path: Path):
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.server"],
        cwd=str(ROOT),
        env=_build_env(db_path),
    )
    return stdio_client(server_params)


async def _call(
    session: ClientSession,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    text = result.content[0].text if result.content else ""
    if result.isError:
        raise RuntimeError(f"{tool} failed: {text}")
    structured = result.structuredContent or {}
    print(f"\n--- {tool} ---")
    print(text.strip() or structured)
    return {"text": text, "structured": structured}


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="waggle-basic-usage-") as tmp:
        db_path = Path(tmp) / "memory.db"
        print(f"Using isolated DB: {db_path}")
        print(f"WAGGLE_MODEL={os.environ.get('WAGGLE_MODEL', 'deterministic')}")

        async with (
            _open_session(db_path) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()

            # Session 1: store decision + reason (README PostgreSQL / MySQL example)
            observed = await _call(
                session,
                "observe_conversation",
                {
                    "user_message": (
                        "We chose PostgreSQL over MySQL because MySQL replication has been painful for our team."
                    ),
                    "assistant_response": ("Noted: PostgreSQL was selected; the reason is MySQL replication pain."),
                },
            )
            stored = observed["structured"].get("stored_nodes", [])
            print(f"Stored nodes: {_summarize_nodes(stored)}")

        # Session 2: simulate a new chat — same DB, new MCP process
        async with (
            _open_session(db_path) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()

            await _call(
                session,
                "query_graph",
                {
                    "query": "What database did we choose and why?",
                    "max_nodes": 8,
                    "max_depth": 2,
                },
            )

            correction = await _call(
                session,
                "observe_conversation",
                {
                    "user_message": ("Update: we're switching to MySQL — the team knows it better than PostgreSQL."),
                    "assistant_response": (
                        "I'll record that the database direction changed to MySQL for team familiarity."
                    ),
                },
            )
            conflicts = correction["structured"].get("conflicts", [])
            print(f"Conflicts reported: {len(conflicts)}")

            await _call(
                session,
                "query_graph",
                {
                    "query": "What is the latest database direction?",
                    "max_nodes": 8,
                    "max_depth": 2,
                },
            )

    print("\nDone. Graph lived under a temp DB; re-run anytime offline with WAGGLE_MODEL=deterministic.")


if __name__ == "__main__":
    asyncio.run(main())
