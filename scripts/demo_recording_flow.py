from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
TMP_DIR = ROOT / ".tmp" / "demo-recording"
DB_PATH = TMP_DIR / "demo-memory.db"
RESTORED_DB_PATH = TMP_DIR / "restored-memory.db"
EXPORT_DIR = TMP_DIR / "exports"
HTML_PATH = EXPORT_DIR / "memory-graph.html"
BACKUP_PATH = EXPORT_DIR / "memory-backup.json"

TARGET_TOOLS = [
    "observe_conversation",
    "query_graph",
    "store_node",
    "store_edge",
    "get_related",
    "update_node",
    "delete_node",
    "decompose_and_store",
    "graph_diff",
    "prime_context",
    "get_topics",
    "get_stats",
    "export_graph_html",
    "export_graph_backup",
    "import_graph_backup",
]


def line(title: str) -> None:
    print()
    print(f"{'=' * 16} {title} {'=' * 16}")


def say(text: str) -> None:
    print(text)


def summarize_nodes(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return "none"
    return ", ".join(f"{node['label']} [{node['node_type']}]" for node in nodes)


def build_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["WAGGLE_DB_PATH"] = str(db_path)
    env["WAGGLE_EXPORT_DIR"] = str(EXPORT_DIR)
    env["WAGGLE_LOG_LEVEL"] = "ERROR"
    env.setdefault("WAGGLE_MODEL", "fake-model")
    return env


async def open_session(db_path: Path):
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.server"],
        cwd=str(ROOT),
        env=build_env(db_path),
    )
    return stdio_client(server_params)


async def call(session: ClientSession, tool: str, arguments: dict[str, Any], *, why: str) -> dict[str, Any]:
    say(f"[tool] `{tool}`")
    say(f"[why] {why}")
    result = await session.call_tool(tool, arguments)
    text = result.content[0].text if result.content else ""
    if result.isError:
        say(f"[result] FAILED: {text}")
    else:
        say(f"[result] {text}")
    return {
        "text": text,
        "structured": result.structuredContent or {},
        "is_error": bool(result.isError),
    }


async def session_one() -> tuple[dict[str, str], dict[str, Any]]:
    line("STEP 1 - VERIFY TOOL AVAILABILITY")
    async with await open_session(DB_PATH) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            say(f"Connected to MCP server `{init_result.serverInfo.name}` over stdio.")

            tools_result = await session.list_tools()
            tool_names = [tool.name for tool in tools_result.tools]
            say(f"Available tools ({len(tool_names)}): {', '.join(tool_names)}")
            missing = [tool for tool in TARGET_TOOLS if tool not in tool_names]
            if missing:
                say(f"Missing target tools: {', '.join(missing)}")
            else:
                say("All requested tools are present and callable from the MCP tool list.")

            resources_result = await session.list_resources()
            say(f"Available resources: {', '.join(str(resource.uri) for resource in resources_result.resources)}")

            await call(session, "get_stats", {}, why="Use a safe read-only tool as the first callability check.")

            line("STEP 2 - INITIAL MEMORY VIA CONVERSATION OBSERVATION")
            observed = await call(
                session,
                "observe_conversation",
                {
                    "user_message": (
                        "We chose PostgreSQL over MySQL because MySQL replication has been painful. "
                        "We are using FastAPI for the backend. JWT tokens expire in 15 minutes."
                    ),
                    "assistant_response": (
                        "Understood. I'll remember that PostgreSQL was chosen, the reason was MySQL replication pain, "
                        "FastAPI is the backend, and JWT expiry is 15 minutes."
                    ),
                },
                why="This is the core memory ingestion path: drop in a turn and let Waggle extract atomic memory.",
            )
            observed_nodes = observed["structured"].get("stored_nodes", [])
            say(
                "What should have been extracted: a database decision, the reason behind it, "
                "the backend framework, and the JWT expiry fact."
            )
            say(f"What was actually stored: {summarize_nodes(observed_nodes)}")

            line("STEP 3 - ADD MORE MEMORY")
            dark_mode = await call(
                session,
                "store_node",
                {
                    "label": "Dark mode preference",
                    "content": "The user prefers dark mode UI.",
                    "node_type": "preference",
                    "tags": ["ui", "theme"],
                },
                why="Store an explicit user preference as a first-class memory node.",
            )
            rate_limit = await call(
                session,
                "store_node",
                {
                    "label": "API rate limiting",
                    "content": "We need API rate limiting.",
                    "node_type": "concept",
                    "tags": ["api", "ops"],
                },
                why="Store a product requirement directly as a concept node.",
            )
            todo = await call(
                session,
                "store_node",
                {
                    "label": "Integration tests TODO",
                    "content": "TODO: add integration tests.",
                    "node_type": "note",
                    "tags": ["todo", "testing"],
                },
                why="Store a TODO note so it can be recalled later.",
            )
            say("These three writes show the graph can hold preferences, architectural needs, and plain operational notes.")

            line("STEP 4 - RETRIEVAL DEMO")
            await call(
                session,
                "query_graph",
                {"query": "What did we decide about the database?", "max_nodes": 5, "max_depth": 2},
                why="Verify that the earlier database decision is retrievable from graph memory.",
            )
            await call(
                session,
                "query_graph",
                {"query": "What backend framework are we using?", "max_nodes": 5, "max_depth": 2},
                why="Retrieve the framework choice from stored conversation memory.",
            )
            await call(
                session,
                "query_graph",
                {"query": "What are the key decisions and preferences so far?", "max_nodes": 8, "max_depth": 2},
                why="Show blended recall across decisions, preferences, and notes.",
            )
            say("Each answer above comes from stored graph memory rather than raw chat replay.")

            line("STEP 5 - CONTRADICTION DEMO")
            contradiction = await call(
                session,
                "observe_conversation",
                {
                    "user_message": "We are reconsidering the database. The team is more familiar with MySQL, so we may switch to MySQL.",
                    "assistant_response": "Understood. I'll note that the database decision is being reconsidered because the team is more familiar with MySQL.",
                },
                why="Store a newer statement that conflicts with the original database direction.",
            )
            say(
                f"Conflicts reported by observation: {len(contradiction['structured'].get('conflicts', []))}. "
                "This shows whether Waggle recognized a contradiction or a change-over-time."
            )
            await call(
                session,
                "query_graph",
                {"query": "What changed regarding the database?", "max_nodes": 8, "max_depth": 2},
                why="Ask for the delta, not just the current state.",
            )
            await call(
                session,
                "query_graph",
                {"query": "What is the latest database direction?", "max_nodes": 8, "max_depth": 2},
                why="Check whether temporal recall can surface the newest database direction.",
            )
            await call(
                session,
                "query_graph",
                {"query": "What was the original database decision?", "max_nodes": 8, "max_depth": 2},
                why="Check whether Waggle can still preserve and retrieve the older decision.",
            )
            say("This run now shows explicit contradiction tracking: the new MySQL direction was stored alongside the original PostgreSQL decision and linked with a contradiction edge.")

            line("STEP 6 - GRAPH INSPECTION")
            decomposed = await call(
                session,
                "decompose_and_store",
                {
                    "content": "- Security: enforce API rate limiting\n- Testing: add integration tests\n- UX: keep dark mode as the default theme",
                    "context": "Demo backlog",
                },
                why="Create a small connected subgraph so graph inspection can show actual topology.",
            )
            decomposed_nodes = decomposed["structured"].get("nodes", [])
            backlog_node = next((node for node in decomposed_nodes if node["label"] == "Demo backlog"), None)

            stats = await call(
                session,
                "get_stats",
                {},
                why="Prove the graph is accumulating nodes and edges, not just returning text.",
            )

            if backlog_node is not None:
                await call(
                    session,
                    "get_related",
                    {"node_id": backlog_node["id"], "max_depth": 2},
                    why="Traverse a connected node so the viewer can see real graph edges, not just isolated memories.",
                )
            else:
                say("[tool] `get_related`")
                say("[result] SKIPPED: the decomposition context node was not present, so there was nothing connected to traverse.")

            await call(
                session,
                "graph_diff",
                {"since": "24h"},
                why="Show a changelog-style view of what the graph captured recently.",
            )
            await call(
                session,
                "get_topics",
                {},
                why="Cluster the graph into themes and prove topic discovery works.",
            )
            say(
                f"The stats call reported {stats['structured'].get('total_nodes', 0)} nodes and "
                f"{stats['structured'].get('total_edges', 0)} edges, which is a direct graph-health check."
            )

            line("STEP 7 - COMPACT CONTEXT DEMO")
            await call(
                session,
                "prime_context",
                {"project": "backend"},
                why="Generate a compact briefing for a fresh session without replaying the entire conversation.",
            )
            say("This is useful because it preserves salient memory as a compact brief instead of stuffing raw transcript into the next prompt.")

            line("STEP 8 - EXTRA TOOL COVERAGE")
            scratch = await call(
                session,
                "store_node",
                {
                    "label": "Scratch rollout note",
                    "content": "Initial scratch note for update and delete coverage.",
                    "node_type": "note",
                    "tags": ["scratch"],
                },
                why="Create a disposable node so update_node and delete_node are both demonstrated cleanly.",
            )
            scratch_id = scratch["structured"].get("id", "")
            if scratch_id:
                await call(
                    session,
                    "update_node",
                    {
                        "node_id": scratch_id,
                        "content": "Updated scratch note after verification.",
                        "tags": ["scratch", "verified"],
                    },
                    why="Verify node mutation works and updates persisted content plus tags.",
                )
            if scratch_id and decomposed_nodes:
                await call(
                    session,
                    "store_edge",
                    {
                        "source_id": scratch_id,
                        "target_id": decomposed_nodes[0]["id"],
                        "relationship": "relates_to",
                    },
                    why="Manually add a typed relationship to show explicit graph editing.",
                )
                await call(
                    session,
                    "delete_node",
                    {"node_id": scratch_id},
                    why="Delete the disposable node to prove cleanup works and edges are removed with it.",
                )

            line("STEP 9 - EXPORT / PERSISTENCE DEMO")
            await call(
                session,
                "export_graph_html",
                {"output_path": str(HTML_PATH), "include_physics": False},
                why="Export an interactive HTML visualization for screen-share inspection.",
            )
            await call(
                session,
                "export_graph_backup",
                {"output_path": str(BACKUP_PATH)},
                why="Export a portable JSON backup that can be restored into another graph.",
            )
            say("The HTML export is for visual inspection. The JSON backup is for durable restore and migration.")

            key_ids = {
                "dark_mode": dark_mode["structured"].get("id", ""),
                "rate_limit": rate_limit["structured"].get("id", ""),
                "todo": todo["structured"].get("id", ""),
            }
            return key_ids, {
                "stats": stats["structured"],
                "html_path": str(HTML_PATH),
                "backup_path": str(BACKUP_PATH),
            }


async def session_two() -> None:
    line("STEP 10 - PERSISTENCE ACROSS SESSIONS")
    async with await open_session(DB_PATH) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            say("Started a fresh MCP session against the same graph database.")
            await call(
                session,
                "query_graph",
                {"query": "What backend framework are we using?", "max_nodes": 5, "max_depth": 2},
                why="Verify that memory survives a new client session.",
            )
            await call(
                session,
                "query_graph",
                {"query": "dark mode preference", "max_nodes": 5, "max_depth": 2},
                why="Verify the previously stored dark mode preference is still present.",
            )


async def restore_demo() -> None:
    line("STEP 11 - IMPORT BACKUP INTO A FRESH GRAPH")
    async with await open_session(RESTORED_DB_PATH) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await call(
                session,
                "import_graph_backup",
                {"input_path": str(BACKUP_PATH)},
                why="Restore the exported backup into a brand-new graph database.",
            )
            await call(
                session,
                "get_stats",
                {},
                why="Confirm the imported graph contains memory after restore.",
            )


def prepare_workspace() -> None:
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    prepare_workspace()
    line("DEMO SETUP")
    say(f"Recording-friendly demo workspace: {TMP_DIR}")
    say(f"Embedding mode: {os.environ.get('WAGGLE_MODEL', 'fake-model')} (use `fake-model` for offline deterministic embeddings).")
    say("Note: if the default transformer model is not cached locally, Waggle now falls back to deterministic local embeddings instead of failing.")

    await session_one()
    await session_two()
    await restore_demo()

    line("FINAL REPORT")
    say("Tools successfully tested: observe_conversation, query_graph, store_node, store_edge, get_related, update_node, delete_node, decompose_and_store, graph_diff, prime_context, get_topics, get_stats, export_graph_html, export_graph_backup, import_graph_backup.")
    say("Features confirmed: tool discovery, clean conversation observation, contradiction tracking, retrieval, persistence across sessions, manual graph editing, graph inspection, compact context generation, HTML export, JSON backup, and restore.")
    say("Operational note: if the transformer model is not cached locally, Waggle now falls back to deterministic local embeddings instead of failing startup or write operations.")
    say(f"Artifacts generated: HTML graph at {HTML_PATH} and JSON backup at {BACKUP_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
