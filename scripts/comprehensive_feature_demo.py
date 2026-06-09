from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
TMP_DIR = ROOT / ".tmp" / "comprehensive-feature-demo"
DB_PATH = TMP_DIR / "demo-memory.db"
RESTORE_DB_PATH = TMP_DIR / "restored-memory.db"
EXPORT_DIR = TMP_DIR / "exports"
VAULT_DIR = EXPORT_DIR / "vault"
CONTEXT_DIR = EXPORT_DIR / "context-bundle"
GRAPH_HTML = EXPORT_DIR / "graph.html"
GRAPH_BACKUP = EXPORT_DIR / "graph-backup.json"
ARTIFACT_DIR = ROOT / "tests" / "artifacts" / "verification" / "2026-04-21-comprehensive-feature-demo"
REPORT_PATH = ARTIFACT_DIR / "comprehensive_feature_demo.md"
RAW_JSON_PATH = ARTIFACT_DIR / "tool_calls.json"


def setup_dirs() -> None:
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def build_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["WAGGLE_DB_PATH"] = str(db_path)
    env["WAGGLE_EXPORT_DIR"] = str(EXPORT_DIR)
    env["WAGGLE_LOG_LEVEL"] = "ERROR"
    env["WAGGLE_MODEL"] = env.get("WAGGLE_MODEL", "all-MiniLM-L6-v2")
    env["WAGGLE_BACKEND"] = "sqlite"
    return env


def clip(text: str, limit: int = 2500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def format_json(data: Any, limit: int = 2000) -> str:
    dumped = json.dumps(data, indent=2, ensure_ascii=True, default=str)
    return clip(dumped, limit)


async def call_tool(session: ClientSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    started = datetime.now(UTC).isoformat()
    try:
        result = await session.call_tool(name, args)
        text = result.content[0].text if result.content else ""
        payload = {
            "tool": name,
            "arguments": args,
            "started_at_utc": started,
            "is_error": bool(result.isError),
            "text": text,
            "structured": result.structuredContent or {},
        }
    except Exception as exc:
        payload = {
            "tool": name,
            "arguments": args,
            "started_at_utc": started,
            "is_error": True,
            "text": f"Exception: {exc}",
            "structured": {},
        }
    return payload


async def run_demo() -> tuple[list[dict[str, Any]], str]:
    logs: list[dict[str, Any]] = []

    # CLI features guide (outside MCP tool call)
    try:
        cli = subprocess.run(
            [sys.executable, "-m", "waggle.server", "features"],
            cwd=str(ROOT),
            env=build_env(DB_PATH),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        features_output = (
        (cli.stdout or "")
        + ("\n" + cli.stderr if cli.stderr else "")
        )

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
        "waggle.server features command timed out after 30 seconds.\n"
        f"Stdout:\n{exc.stdout or ''}\n"
        f"Stderr:\n{exc.stderr or ''}"
    ) from exc

    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
        "waggle.server features command failed.\n"
        f"Return code: {exc.returncode}\n"
        f"Stdout:\n{exc.stdout or ''}\n"
        f"Stderr:\n{exc.stderr or ''}"
    ) from exc

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.server"],
        cwd=str(ROOT),
        env=build_env(DB_PATH),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            logs.append({
                "tool": "initialize",
                "arguments": {},
                "is_error": False,
                "text": f"Connected to {init.serverInfo.name} {init.serverInfo.version}",
                "structured": {"server": init.serverInfo.name, "version": init.serverInfo.version},
            })

            tools_result = await session.list_tools()
            logs.append({
                "tool": "list_tools",
                "arguments": {},
                "is_error": False,
                "text": f"{len(tools_result.tools)} tools available",
                "structured": {"tool_names": [t.name for t in tools_result.tools]},
            })

            resources_result = await session.list_resources()
            logs.append({
                "tool": "list_resources",
                "arguments": {},
                "is_error": False,
                "text": f"{len(resources_result.resources)} resources available",
                "structured": {"resources": [str(r.uri) for r in resources_result.resources]},
            })

            # Core ingestion data across multiple contexts
            logs.append(await call_tool(session, "observe_conversation", {
                "user_message": "Should we use PostgreSQL or MySQL for production?",
                "assistant_response": "Decision: use PostgreSQL for production due to reliability and team readiness.",
                "agent_id": "demo-agent",
                "project": "waggle-demo",
                "session_id": "s1",
            }))
            logs.append(await call_tool(session, "observe_conversation", {
                "user_message": "Actually we might switch to MySQL because team familiarity is higher.",
                "assistant_response": "Noted: potential switch to MySQL due to team familiarity.",
                "agent_id": "demo-agent",
                "project": "waggle-demo",
                "session_id": "s2",
            }))
            logs.append(await call_tool(session, "observe_conversation", {
                "user_message": "Frontend default should be dark mode and auth token expiry is 15 minutes.",
                "assistant_response": "Stored preference for dark mode and JWT expiry = 15 minutes.",
                "agent_id": "demo-agent",
                "project": "waggle-demo",
                "session_id": "s3",
            }))

            custom1 = await call_tool(session, "store_node", {
                "label": "API rate limiting",
                "content": "Apply 100 req/min baseline and burst controls.",
                "node_type": "concept",
                "tags": ["api", "ops"],
            })
            logs.append(custom1)

            custom2 = await call_tool(session, "store_node", {
                "label": "Bench preference",
                "content": "Use Python for benchmark harness scripts.",
                "node_type": "preference",
                "tags": ["benchmark", "python"],
            })
            logs.append(custom2)

            source_id = custom1.get("structured", {}).get("id", "")
            target_id = custom2.get("structured", {}).get("id", "")
            if source_id and target_id:
                logs.append(await call_tool(session, "store_edge", {
                    "source_id": source_id,
                    "target_id": target_id,
                    "relationship": "relates_to",
                    "weight": 0.72,
                }))

            decomp = await call_tool(session, "decompose_and_store", {
                "content": "- Add integration tests\n- Keep dark mode default\n- Keep PostgreSQL migration scripts reviewed weekly",
                "context": "Milestone backlog",
            })
            logs.append(decomp)

            # Multi-input graph retrieval tests
            query_inputs = [
                "database decision",
                "latest database direction",
                "original database choice",
                "dark mode preference",
                "benchmark language preference",
            ]
            for mode in ["graph", "replay", "fusion"]:
                for q in query_inputs:
                    logs.append(await call_tool(session, "query_graph", {
                        "query": q,
                        "max_nodes": 10,
                        "max_depth": 2,
                        "retrieval_mode": mode,
                        "project": "waggle-demo",
                    }))

            # Scope/history/timeline/related
            if source_id:
                logs.append(await call_tool(session, "get_related", {"node_id": source_id, "max_depth": 2}))
                logs.append(await call_tool(session, "get_node_history", {"node_id": source_id, "max_depth": 2}))
                logs.append(await call_tool(session, "timeline", {"node_id": source_id, "limit": 20, "max_depth": 2, "include_evidence": True}))

            logs.append(await call_tool(session, "timeline", {
                "query": "database decision",
                "limit": 20,
                "max_depth": 2,
                "include_evidence": True,
            }))

            logs.append(await call_tool(session, "list_context_scopes", {}))
            logs.append(await call_tool(session, "list_conflicts", {"include_resolved": False, "limit": 20}))

            # Attempt conflict resolution for first unresolved edge
            unresolved = logs[-1].get("structured", {}).get("conflicts", [])
            if unresolved:
                edge_id = unresolved[0].get("edge_id") or unresolved[0].get("id") or ""
                if edge_id:
                    logs.append(await call_tool(session, "resolve_conflict", {
                        "edge_id": edge_id,
                        "resolution_note": "Resolved in demo: retain latest as active, keep history.",
                    }))
                    logs.append(await call_tool(session, "list_conflicts", {"include_resolved": True, "limit": 20}))

            # Updates and deletes
            if target_id:
                logs.append(await call_tool(session, "update_node", {
                    "node_id": target_id,
                    "content": "Use Python 3.11+ for benchmark harness scripts.",
                    "tags": ["benchmark", "python", "updated"],
                }))

                disposable = await call_tool(session, "store_node", {
                    "label": "Disposable note",
                    "content": "Temporary node for delete verification",
                    "node_type": "note",
                    "tags": ["tmp"],
                })
                logs.append(disposable)
                disposable_id = disposable.get("structured", {}).get("id", "")
                if disposable_id:
                    logs.append(await call_tool(session, "delete_node", {"node_id": disposable_id}))

            # Context and stats
            logs.append(await call_tool(session, "prime_context", {"project": "waggle-demo", "agent_id": "demo-agent"}))
            logs.append(await call_tool(session, "get_topics", {}))
            logs.append(await call_tool(session, "graph_diff", {"since": "24h"}))
            logs.append(await call_tool(session, "get_stats", {}))

            # Exports
            logs.append(await call_tool(session, "export_graph_html", {
                "output_path": str(GRAPH_HTML),
                "include_physics": False,
            }))
            logs.append(await call_tool(session, "export_graph_backup", {
                "output_path": str(GRAPH_BACKUP),
            }))
            logs.append(await call_tool(session, "export_context_bundle", {
                "mode": "query",
                "query": "database decision and preferences",
                "project": "waggle-demo",
                "retrieval_mode": "fusion",
                "format": "both",
                "output_path": str(CONTEXT_DIR),
                "max_nodes": 20,
                "max_depth": 2,
                "include_edges": True,
                "include_timestamps": True,
                "audience": "human",
            }))
            logs.append(await call_tool(session, "export_markdown_vault", {
                "root_path": str(VAULT_DIR),
                "project": "waggle-demo",
            }))

    # Import validation in fresh db
    restore_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "waggle.server"],
        cwd=str(ROOT),
        env=build_env(RESTORE_DB_PATH),
    )
    async with stdio_client(restore_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            logs.append(await call_tool(session, "import_graph_backup", {"input_path": str(GRAPH_BACKUP)}))
            logs.append(await call_tool(session, "get_stats", {}))
            logs.append(await call_tool(session, "import_markdown_vault", {"root_path": str(VAULT_DIR)}))
            logs.append(await call_tool(session, "query_graph", {
                "query": "database decision",
                "max_nodes": 10,
                "max_depth": 2,
                "retrieval_mode": "graph",
            }))

    return logs, features_output


def build_report(logs: list[dict[str, Any]], features_output: str, model_name: str) -> str:
    generated_at = datetime.now(UTC).isoformat()
    total = len(logs)
    failures = sum(1 for l in logs if l.get("is_error"))
    unique_tools = sorted({l.get("tool", "") for l in logs})

    lines: list[str] = []
    lines.append("# Waggle MCP Comprehensive Feature Demo")
    lines.append("")
    lines.append(f"Generated (UTC): `{generated_at}`")
    lines.append(f"Workspace: `{ROOT}`")
    lines.append(f"Database (primary): `{DB_PATH}`")
    lines.append(f"Database (restore validation): `{RESTORE_DB_PATH}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total calls recorded: **{total}**")
    lines.append(f"- Calls with errors: **{failures}**")
    lines.append(f"- Unique actions covered: **{len(unique_tools)}**")
    lines.append(f"- Covered tools/actions: `{', '.join(unique_tools)}`")
    lines.append(f"- Embedding model: **{model_name}**")
    lines.append("")
    lines.append("## CLI Feature Guide Output")
    lines.append("")
    lines.append("```text")
    lines.append(clip(features_output.strip(), 6000))
    lines.append("```")
    lines.append("")
    lines.append("## Tool Call Transcript")
    lines.append("")

    for i, entry in enumerate(logs, start=1):
        tool = entry.get("tool", "")
        status = "ERROR" if entry.get("is_error") else "OK"
        lines.append(f"### {i}. `{tool}` [{status}]")
        lines.append("")
        lines.append("Arguments:")
        lines.append("```json")
        lines.append(format_json(entry.get("arguments", {}), 1200))
        lines.append("```")
        lines.append("")
        lines.append("Structured content:")
        lines.append("```json")
        lines.append(format_json(entry.get("structured", {}), 1800))
        lines.append("```")
        lines.append("")
        lines.append("Text output:")
        lines.append("```text")
        lines.append(clip((entry.get("text") or "").strip(), 1800))
        lines.append("```")
        lines.append("")

    lines.append("## Exported Artifacts")
    lines.append("")
    lines.append(f"- Graph HTML: `{GRAPH_HTML}`")
    lines.append(f"- Graph backup JSON: `{GRAPH_BACKUP}`")
    lines.append(f"- Context bundle directory: `{CONTEXT_DIR}`")
    lines.append(f"- Markdown vault directory: `{VAULT_DIR}`")
    lines.append(f"- Raw call log JSON: `{RAW_JSON_PATH}`")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Retrieval was tested with multiple natural-language inputs and across `graph`, `replay`, and `fusion` retrieval modes.")
    lines.append("- Backup export/import and markdown vault export/import were validated in a fresh database instance.")
    lines.append(f"- Demo used `WAGGLE_MODEL={model_name}`.")
    return "\n".join(lines) + "\n"


async def main() -> None:
    setup_dirs()
    model_name = os.environ.get("WAGGLE_MODEL", "all-MiniLM-L6-v2")
    logs, features_output = await run_demo()
    RAW_JSON_PATH.write_text(json.dumps(logs, indent=2, ensure_ascii=True, default=str) + "\n", encoding="utf-8")
    report = build_report(logs, features_output, model_name)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(str(REPORT_PATH))


if __name__ == "__main__":
    asyncio.run(main())
