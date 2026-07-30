from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import threading
import warnings
import webbrowser
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn

from waggle import __version__
from waggle.abhi import (
    ABHI_MERGE_STRATEGIES,
    DEFAULT_ABHI_MERGE_STRATEGY,
    build_abhi_document,
    load_abhi_document,
)
from waggle.config import DEFAULT_DB_PATH, AppConfig
from waggle.embeddings import EmbeddingModel
from waggle.errors import ValidationFailure, WaggleError
from waggle.graph import MemoryGraph
from waggle.models import TranscriptIngestionInput, utc_now

# Import utilities from server package
from .mcp import AUTOMATIC_MEMORY_RULE_TEXT, WaggleServer
from .routes import create_http_application
from .utils import (
    _assert_export_safe,
    _build_backend,
    _c,
    _default_graph,
    _default_stdio_command,
    _emit_cli_error,
    _fail,
    _ok,
    _parse_api_key_scopes,
    _python_exe,
    _resolve_drive_token_path,
    _resolve_passphrase,
    _serialize_api_key_record,
    _serialize_audit_event,
    _serialize_retention_policy,
    _serialize_retention_run,
)

# Optional drive_sync imports
try:
    from waggle.drive_sync import (
        download_drive_file,
        ensure_drive_credentials,
        merge_downloaded_abhi,
        push_file_to_drive,
        resolve_drive_file_id,
        share_drive_file,
    )
except Exception:
    download_drive_file = None
    ensure_drive_credentials = None
    merge_downloaded_abhi = None
    push_file_to_drive = None
    resolve_drive_file_id = None
    share_drive_file = None

from .drive import _require_drive_sync

LOGGER = logging.getLogger(__name__)

_KNOWN_CONFIG_PATHS: list[tuple[str, str]] = [
    ("Claude Desktop (macOS/Linux)", "~/.config/claude/claude_desktop_config.json"),
    ("Claude Desktop (macOS alt)", "~/Library/Application Support/Claude/claude_desktop_config.json"),
    ("Claude Desktop (Windows)", "%APPDATA%\\Claude\\claude_desktop_config.json"),
    ("Cursor (macOS/Linux)", "~/.cursor/mcp.json"),
    ("Cursor (Windows)", "%APPDATA%\\Cursor\\User\\mcp.json"),
    ("Antigravity AI agent (macOS/Linux)", "~/.gemini/antigravity/mcp_config.json"),
    ("Antigravity AI agent (Windows)", "%USERPROFILE%\\.gemini\\antigravity\\mcp_config.json"),
    ("VS Code extension (Windows)", "%APPDATA%\\Antigravity\\User\\mcp.json"),
    ("Codex", "~/.codex/config.toml"),
]

_DOCTOR_KNOWN_GOTCHAS = """\
Known API gotchas (from the Waggle field error log):
  • observe_conversation requires fields 'user_message' and 'assistant_response'.
    Do NOT use 'user_text' / 'assistant_text' — those will be rejected.
  • get_topics does not support scope filtering. Passing 'project' etc. used to
    cause "additional properties" errors. It now accepts (and ignores) scope fields.
  • Use the official 'mcp' Python package for stdio clients, not hand-rolled JSON-RPC.
    Install: pip install mcp
    Use:     from mcp import ClientSession, StdioServerParameters
             from mcp.client.stdio import stdio_client
  • On Windows, stdio framing requires the official mcp client; raw subprocess I/O
    with manual \\n line endings will produce garbled or dropped messages.
  • Set WAGGLE_MODEL=deterministic for offline/offline-first environments.
    The default (all-MiniLM-L6-v2) downloads ~420 MB on first run and will
    block store_node indefinitely if no network is available.
"""

_ABHI_IMPORT_STRATEGIES = (
    "skip-existing",
    "overwrite",
    "branch",
)

_FEATURES_GUIDE = """\
waggle-mcp feature guide
========================

Core graph workflow
-------------------
1. Ingest memory
   - observe_conversation : extract structured facts/decisions from a finished turn
   - decompose_and_store  : split long content into linked atomic nodes
   - store_node           : save one standalone node directly
   - store_edge           : connect nodes explicitly when relationship is known

2. Retrieve context
   - query_graph          : semantic retrieval over nodes plus graph/replay/fusion context
   - get_related          : walk outward from one node to inspect connected context
   - get_node_history     : inspect one node's evidence, validity, and linked context
   - timeline             : see recent graph events chronologically
   - prime_context        : build a compact briefing for a new model/session

3. Resolve conflicts
   - list_conflicts       : list contradiction/update edges
   - resolve_conflict     : mark a conflict as resolved without deleting history

4. Export / handoff  (git-vocabulary)
   - commit                : snapshot memory to a portable .abhi file (waggle commit)
   - checkpoint-context    : scoped handoff checkpoint for session/app switches
   - pull                  : load a .abhi file into the graph (waggle pull)
   - push                  : upload to Google Drive (waggle push)
   - diff                  : compare two .abhi files (waggle diff)
   - merge                 : three-way merge .abhi files (waggle merge)
   - fsck                  : validate an .abhi file (waggle fsck)
   - show                  : inspect an .abhi file without importing (waggle show)
   - grep                  : query an .abhi file (waggle grep)
   - export_markdown_vault : export one-file-per-node markdown for manual editing
   - import_markdown_vault : re-import edited markdown vault files
   - export_graph_html     : interactive graph visualization

5. Inspect the graph
   - get_stats            : node/edge counts and high-level graph stats
   - list_context_scopes  : available project / agent / session scopes
   - get_topics           : topic clusters via community detection
   - graph_diff           : recently changed nodes and edges

Important behavior
------------------
- store_node alone does not create edges.
- Edges come from:
  - explicit store_edge calls
  - observe_conversation extraction
  - decompose_and_store inferred structure
  - automatic contradiction/update detection in some cases
- The graph-aware tools are what bring connected context back to the model:
  - query_graph
  - get_related
  - get_node_history
  - prime_context
  - commit (waggle commit — snapshot memory to .abhi)

Common workflows
----------------
- Quick setup:
  waggle-mcp setup --yes

- Start the MCP server:
  waggle-mcp serve

- Export a handoff bundle:
  waggle-mcp export-context-bundle --mode query --query "why did we choose PostgreSQL?"

- Checkpoint the current context before switching sessions/apps:
  waggle-mcp checkpoint-context --project MCP --session-id thread-123 --output ./handoff.abhi

- Resume from a portable checkpoint when scoped DB recall is cold:
  waggle-mcp pull ./handoff.abhi

- Edit memory as markdown:
  waggle-mcp export-markdown-vault --root-path ./vault
  waggle-mcp import-markdown-vault --root-path ./vault
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waggle-mcp",
        description=(
            "Persistent graph memory for MCP-compatible AI clients. "
            "Use 'waggle-mcp features' for a detailed guide to ingestion, graph retrieval, "
            "conflict handling, and export workflows."
        ),
        epilog="Examples: 'waggle-mcp setup --yes', 'waggle-mcp serve', 'waggle-mcp features'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--quiet", dest="global_quiet", action="store_true", help="Suppress startup banners and messages."
    )
    quiet_parent = argparse.ArgumentParser(add_help=False)
    quiet_parent.add_argument("--quiet", action="store_true", help="Suppress startup banners and messages.")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser(
        "serve",
        parents=[quiet_parent],
        help="Run the MCP server using the configured stdio or HTTP transport.",
    )
    serve.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="Override WAGGLE_TRANSPORT for this run.",
    )
    graph_editor = subparsers.add_parser(
        "edit-graph",
        parents=[quiet_parent],
        help="Launch the visual graph editor in a browser window.",
        description=(
            "Start the Waggle HTTP app for local graph editing and open /graph in the browser by default. "
            "Use this for mouse-and-keyboard graph editing: add, remove, connect, reposition, import, and export."
        ),
    )
    graph_editor.add_argument("--host", default="127.0.0.1")
    graph_editor.add_argument("--port", type=int, default=8686)
    graph_editor.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    graph_viewer = subparsers.add_parser(
        "view-graph",
        parents=[quiet_parent],
        help="Launch the visual graph viewer/editor in a browser window.",
        description="Alias for edit-graph. Starts the local graph UI and opens it in the browser by default.",
    )
    graph_viewer.add_argument("--host", default="127.0.0.1")
    graph_viewer.add_argument("--port", type=int, default=8686)
    graph_viewer.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    graph_ui = subparsers.add_parser(
        "ui",
        parents=[quiet_parent],
        help="Launch the local graph UI in the browser.",
        description="Alias for edit-graph. Starts the localhost Graph Studio and opens it by default.",
    )
    graph_ui.add_argument("--host", default="127.0.0.1")
    graph_ui.add_argument("--port", type=int, default=8686)
    graph_ui.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    graph_studio = subparsers.add_parser(
        "graph-studio",
        parents=[quiet_parent],
        help="Alias for the local Graph Studio browser UI.",
        description="Alias for ui/edit-graph. Starts the localhost Graph Studio and opens it by default.",
    )
    graph_studio.add_argument("--host", default="127.0.0.1")
    graph_studio.add_argument("--port", type=int, default=8686)
    graph_studio.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    open_studio = subparsers.add_parser(
        "open-studio",
        parents=[quiet_parent],
        help="Alias for graph-studio.",
        description="Alias for graph-studio/ui/edit-graph. Starts the localhost Graph Studio and opens it by default.",
    )
    open_studio.add_argument("--host", default="127.0.0.1")
    open_studio.add_argument("--port", type=int, default=8686)
    open_studio.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)

    create_tenant = subparsers.add_parser(
        "create-tenant", help="Create or update a tenant record in the active backend."
    )
    create_tenant.add_argument("--tenant-id", required=True)
    create_tenant.add_argument("--name", default="")

    create_api_key = subparsers.add_parser("create-api-key", help="Issue an API key for a tenant.")
    create_api_key.add_argument("--tenant-id", required=True)
    create_api_key.add_argument("--name", default="")
    create_api_key.add_argument("--expires-in-days", type=int, default=0)
    create_api_key.add_argument("--created-by", default="")
    create_api_key.add_argument("--scopes", default="graph:read,graph:write,admin:read,admin:write")

    list_api_keys = subparsers.add_parser("list-api-keys", help="List API keys for a tenant.")
    list_api_keys.add_argument("--tenant-id", required=True)

    revoke_api_key = subparsers.add_parser("revoke-api-key", help="Revoke an API key.")
    revoke_api_key.add_argument("--api-key-id", required=True)
    revoke_api_key.add_argument("--tenant-id", default="")

    retention_status = subparsers.add_parser("retention-status", help="Show the active retention policy for a tenant.")
    retention_status.add_argument("--tenant-id", default="")

    set_retention = subparsers.add_parser("set-retention", help="Create or update the retention policy for a tenant.")
    set_retention.add_argument("--tenant-id", default="")
    set_retention.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None)
    set_retention.add_argument("--days", type=int, default=None)
    set_retention.add_argument("--interval-hours", type=int, default=None)

    prune_retention = subparsers.add_parser("prune-retention", help="Run retention pruning immediately for a tenant.")
    prune_retention.add_argument("--tenant-id", default="")
    prune_retention.add_argument("--batch-size", type=int, default=1000)

    list_retention_runs = subparsers.add_parser(
        "list-retention-runs", help="List recent retention prune runs for a tenant."
    )
    list_retention_runs.add_argument("--tenant-id", default="")
    list_retention_runs.add_argument("--limit", type=int, default=20)

    list_audit_events = subparsers.add_parser("list-audit-events", help="List recent audit events for a tenant.")
    list_audit_events.add_argument("--tenant-id", default="")
    list_audit_events.add_argument("--limit", type=int, default=100)
    list_audit_events.add_argument("--type", dest="event_type", default="")
    list_audit_events.add_argument("--actor-id", default="")
    list_audit_events.add_argument("--resource-id", default="")
    list_audit_events.add_argument("--resource-type", default="")
    list_audit_events.add_argument("--status", default="")

    migrate_sqlite = subparsers.add_parser(
        "migrate-sqlite", help="Export a SQLite graph and import it into the configured Neo4j backend."
    )
    migrate_sqlite.add_argument("--db-path", required=True)
    migrate_sqlite.add_argument("--tenant-id", required=True)

    export_abhi = subparsers.add_parser(
        "export",
        help="Export the current memory graph in a portable format. (alias: commit)",
    )
    export_abhi.add_argument("--output", dest="output_path", default=None)
    export_abhi.add_argument("--project", default="")
    export_abhi.add_argument("--agent-id", default="")
    export_abhi.add_argument("--session-id", default="")
    export_abhi.add_argument("--scope", choices=["all", "project", "session", "since-date"], default="all")
    export_abhi.add_argument("--since-date", default="")
    export_abhi.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    export_abhi.add_argument("--encrypt", action="store_true")
    export_abhi.add_argument("--sign", action="store_true")
    export_abhi.add_argument("--signing-key-dir", default="~/.waggle/keys")
    export_abhi.add_argument("--redact", dest="redact_patterns", action="append", default=[])
    export_abhi.add_argument("--passphrase-env", default="")
    export_abhi.add_argument("--strict-export", action="store_true")
    export_abhi.add_argument("--include-deps", action="store_true")
    export_abhi.add_argument(
        "--force", action="store_true", help="Export even if transcript secret scan finds likely credentials or tokens."
    )

    commit_abhi = subparsers.add_parser(
        "commit",
        help="Snapshot the current memory graph to a portable .abhi file. (waggle commit)",
        description=(
            "Save the current memory graph as a portable .abhi file — like git commit for your AI context. "
            "The output file can be shared, diffed, merged, and pulled into any Waggle-enabled client."
        ),
    )
    commit_abhi.add_argument("--output", dest="output_path", default=None)
    commit_abhi.add_argument("--project", default="")
    commit_abhi.add_argument("--agent-id", default="")
    commit_abhi.add_argument("--session-id", default="")
    commit_abhi.add_argument("--scope", choices=["all", "project", "session", "since-date"], default="all")
    commit_abhi.add_argument("--since-date", default="")
    commit_abhi.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    commit_abhi.add_argument("--encrypt", action="store_true")
    commit_abhi.add_argument("--sign", action="store_true")
    commit_abhi.add_argument("--signing-key-dir", default="~/.waggle/keys")
    commit_abhi.add_argument("--redact", dest="redact_patterns", action="append", default=[])
    commit_abhi.add_argument("--passphrase-env", default="")
    commit_abhi.add_argument("--strict-export", action="store_true")
    commit_abhi.add_argument("--include-deps", action="store_true")
    commit_abhi.add_argument(
        "--force", action="store_true", help="Commit even if transcript secret scan finds likely credentials or tokens."
    )

    checkpoint_context = subparsers.add_parser(
        "checkpoint-context",
        help="Create a scoped .abhi checkpoint for context-switch handoff.",
        description=(
            "Checkpoint the current project/session context to a portable .abhi file before switching "
            "sessions or apps. This is a thin wrapper around the existing scoped export flow: keep SQLite "
            "as the live store, use .abhi as the portable handoff artifact."
        ),
    )
    checkpoint_context.add_argument("--output", dest="output_path", default=None)
    checkpoint_context.add_argument("--project", default="")
    checkpoint_context.add_argument("--agent-id", default="")
    checkpoint_context.add_argument("--session-id", default="")
    checkpoint_context.add_argument("--scope", choices=["project", "session", "all", "since-date"], default="")
    checkpoint_context.add_argument("--since-date", default="")
    checkpoint_context.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    checkpoint_context.add_argument("--encrypt", action="store_true")
    checkpoint_context.add_argument("--sign", action="store_true")
    checkpoint_context.add_argument("--signing-key-dir", default="~/.waggle/keys")
    checkpoint_context.add_argument("--redact", dest="redact_patterns", action="append", default=[])
    checkpoint_context.add_argument("--passphrase-env", default="")
    checkpoint_context.add_argument("--strict-export", action="store_true")
    checkpoint_context.add_argument("--include-deps", action="store_true")
    checkpoint_context.add_argument(
        "--force",
        action="store_true",
        help="Checkpoint even if transcript secret scan finds likely credentials or tokens.",
    )

    clear_session = subparsers.add_parser(
        "clear-session",
        help="Delete all graph memory data for one session.",
    )
    clear_session.add_argument("--session-id", required=True)
    clear_session.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_session.add_argument(
        "--dry-run", action="store_true", help="Preview the clear operation without deleting data."
    )

    clear_project = subparsers.add_parser(
        "clear-project",
        help="Delete all graph memory data for one project/repository scope.",
    )
    clear_project.add_argument("--project", required=True)
    clear_project.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_project.add_argument(
        "--dry-run", action="store_true", help="Preview the clear operation without deleting data."
    )

    clear_all = subparsers.add_parser(
        "clear-all",
        help="Delete all graph memory data for the current tenant.",
    )
    clear_all.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_all.add_argument("--dry-run", action="store_true", help="Preview the clear operation without deleting data.")

    import_abhi = subparsers.add_parser(
        "import",
        help="Import a portable memory file into the active backend. (alias: pull <local-file>)",
    )
    import_abhi.add_argument("input_path", nargs="?")
    import_abhi.add_argument("--input", dest="input_path_flag", default="")
    import_abhi.add_argument("--namespace", default="")
    import_abhi.add_argument(
        "--merge-strategy", choices=["skip-existing", "overwrite", "branch"], default="skip-existing"
    )
    import_abhi.add_argument("--verify-signature", action="store_true")
    import_abhi.add_argument("--read-only", action="store_true")
    import_abhi.add_argument("--reembed-on-mismatch", action="store_true")
    import_abhi.add_argument("--passphrase-env", default="")

    validate_abhi = subparsers.add_parser(
        "validate",
        help="Validate a portable .abhi memory file. (alias: fsck)",
    )
    validate_abhi.add_argument("--input", dest="input_path", required=True)
    validate_abhi.add_argument("--passphrase-env", default="")

    fsck_abhi = subparsers.add_parser(
        "fsck",
        help="Verify integrity of an .abhi memory file. (waggle fsck)",
        description=(
            "Verify the integrity hash, schema compliance, and constraint satisfaction of an .abhi file "
            "without importing it — like git fsck for your memory graph."
        ),
    )
    fsck_abhi.add_argument("--input", dest="input_path", required=True)
    fsck_abhi.add_argument("--passphrase-env", default="")

    inspect_abhi = subparsers.add_parser(
        "inspect",
        help="Inspect an .abhi memory file without importing it. (alias: show)",
    )
    inspect_abhi.add_argument("--input", dest="input_path", required=True)
    inspect_abhi.add_argument("--passphrase-env", default="")

    show_abhi = subparsers.add_parser(
        "show",
        help="Inspect an .abhi memory file without importing it. (waggle show)",
        description=(
            "Show summary stats, node/edge type breakdowns, and metadata counts for an .abhi file "
            "without loading it into the graph — like git show for a commit object."
        ),
    )
    show_abhi.add_argument("--input", dest="input_path", required=True)
    show_abhi.add_argument("--passphrase-env", default="")

    diff_abhi = subparsers.add_parser(
        "diff",
        help="Compare two .abhi memory files.",
    )
    diff_abhi.add_argument("input_path_a", nargs="?")
    diff_abhi.add_argument("input_path_b", nargs="?")
    diff_abhi.add_argument("--file-a", dest="input_path_a_flag", default="")
    diff_abhi.add_argument("--file-b", dest="input_path_b_flag", default="")

    merge_abhi = subparsers.add_parser(
        "merge",
        help="Three-way merge .abhi memory files.",
    )
    merge_abhi.add_argument("left_input_path", nargs="?")
    merge_abhi.add_argument("right_input_path", nargs="?")
    merge_abhi.add_argument("--base", dest="base_input_path", default="")
    merge_abhi.add_argument("--left", dest="left_input_path_flag", default="")
    merge_abhi.add_argument("--right", dest="right_input_path_flag", default="")
    merge_abhi.add_argument("--output", dest="output_path", required=True)
    merge_abhi.add_argument(
        "--merge-strategy", choices=["prefer_right", "prefer_left", "last_write_wins"], default="prefer_right"
    )

    query_abhi = subparsers.add_parser(
        "query",
        help="Execute a query against an .abhi memory file. (alias: grep)",
    )
    query_abhi.add_argument("--input", dest="input_path", required=True)
    query_abhi.add_argument("--query-id", default="")
    query_abhi.add_argument("--query-text", default="")
    query_abhi.add_argument("--passphrase-env", default="")

    grep_abhi = subparsers.add_parser(
        "grep",
        help="Search an .abhi memory file with a query. (waggle grep)",
        description=(
            "Execute a saved or ad hoc query against an .abhi file and return matching nodes — "
            "like git grep but for your memory graph."
        ),
    )
    grep_abhi.add_argument("--input", dest="input_path", required=True)
    grep_abhi.add_argument("--query-id", default="")
    grep_abhi.add_argument("--query-text", default="")
    grep_abhi.add_argument("--passphrase-env", default="")

    load_abhi_chunks = subparsers.add_parser(
        "load-chunks",
        help="Load selected or query-relevant chunks from an .abhi memory file.",
    )
    load_abhi_chunks.add_argument("--input", dest="input_path", required=True)
    load_abhi_chunks.add_argument("--chunk-id", dest="chunk_ids", action="append", default=[])
    load_abhi_chunks.add_argument("--query-id", default="")
    load_abhi_chunks.add_argument("--query-text", default="")
    load_abhi_chunks.add_argument("--passphrase-env", default="")

    push_abhi = subparsers.add_parser(
        "push",
        help="Export the current graph to .abhi and upload it to Google Drive.",
    )
    push_abhi.add_argument("--drive", action="store_true")
    push_abhi.add_argument("--output", dest="output_path", default=None)
    push_abhi.add_argument("--project", default="")
    push_abhi.add_argument("--agent-id", default="")
    push_abhi.add_argument("--session-id", default="")
    push_abhi.add_argument("--scope", choices=["all", "project", "session", "since-date"], default="all")
    push_abhi.add_argument("--since-date", default="")
    push_abhi.add_argument("--include-embeddings", action=argparse.BooleanOptionalAction, default=True)
    push_abhi.add_argument("--encrypt", action=argparse.BooleanOptionalAction, default=True)
    push_abhi.add_argument("--passphrase-env", default="")
    push_abhi.add_argument(
        "--force",
        action="store_true",
        help="Export/upload even if transcript secret scan finds likely credentials or tokens.",
    )
    push_abhi.add_argument("--folder-id", default="")
    push_abhi.add_argument("--remote-name", default="")
    push_abhi.add_argument("--client-secret-path", default="~/.waggle/google-client-secret.json")
    push_abhi.add_argument("--token-path", default="")
    push_abhi.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)

    pull_abhi = subparsers.add_parser(
        "pull",
        help="Download a Google Drive .abhi file and merge it into the current graph.",
    )
    pull_abhi.add_argument("file_ref")
    pull_abhi.add_argument("--folder-id", default="")
    pull_abhi.add_argument("--download-path", default="")
    pull_abhi.add_argument("--merged-output", default="")
    pull_abhi.add_argument("--passphrase-env", default="")
    pull_abhi.add_argument("--client-secret-path", default="~/.waggle/google-client-secret.json")
    pull_abhi.add_argument("--token-path", default="")
    pull_abhi.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)
    pull_abhi.add_argument("--namespace", default="")
    pull_abhi.add_argument(
        "--merge-strategy",
        choices=[
            *ABHI_MERGE_STRATEGIES,
            "skip-existing",
            "overwrite",
            "branch",
        ],
        default=None,
        help=(
            "Conflict strategy used when merging local memory with the "
            "downloaded Drive document. Legacy import values are deprecated; "
            "use --import-strategy for those."
        ),
    )
    pull_abhi.add_argument(
        "--import-strategy",
        choices=["skip-existing", "overwrite", "branch"],
        default=None,
        help="Strategy used to import the merged document into the active graph.",
    )
    pull_abhi.add_argument("--verify-signature", action="store_true")
    pull_abhi.add_argument("--read-only", action="store_true")
    pull_abhi.add_argument("--reembed-on-mismatch", action="store_true")

    share_abhi = subparsers.add_parser(
        "share",
        help="Create an anyone-with-link share URL for a Google Drive file.",
    )
    share_abhi.add_argument("file_ref")
    share_abhi.add_argument("--folder-id", default="")
    share_abhi.add_argument("--client-secret-path", default="~/.waggle/google-client-secret.json")
    share_abhi.add_argument("--token-path", default="")
    share_abhi.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)

    export_context_bundle = subparsers.add_parser(
        "export-context-bundle",
        help="Export a markdown/json context package for another model or conversation.",
        description=(
            "Build a portable context bundle from the graph. "
            "Use mode=query for question-focused handoff, mode=prime for a fresh-session brief, "
            "and mode=graph for a broader graph export."
        ),
    )
    export_context_bundle.add_argument("--mode", choices=["prime", "query", "graph"], default="prime")
    export_context_bundle.add_argument("--query", default="")
    export_context_bundle.add_argument("--project", default="")
    export_context_bundle.add_argument("--agent-id", default="")
    export_context_bundle.add_argument("--session-id", default="")
    export_context_bundle.add_argument("--max-nodes", type=int, default=25)
    export_context_bundle.add_argument("--max-depth", type=int, default=2)
    export_context_bundle.add_argument("--retrieval-mode", choices=["graph", "verbatim", "hybrid"], default="graph")
    export_context_bundle.add_argument("--format", choices=["markdown", "json", "both"], default="both")
    export_context_bundle.add_argument("--output-path", default=None)
    export_context_bundle.add_argument("--include-edges", action=argparse.BooleanOptionalAction, default=True)
    export_context_bundle.add_argument("--include-timestamps", action=argparse.BooleanOptionalAction, default=True)
    export_context_bundle.add_argument("--include-source-prompt", action=argparse.BooleanOptionalAction, default=False)
    export_context_bundle.add_argument("--audience", choices=["llm", "human"], default="llm")

    export_markdown_vault = subparsers.add_parser(
        "export-markdown-vault",
        help="Export graph memory as an Obsidian-style markdown vault.",
    )
    export_markdown_vault.add_argument("--root-path", required=True)
    export_markdown_vault.add_argument("--project", default="")
    export_markdown_vault.add_argument("--agent-id", default="")
    export_markdown_vault.add_argument("--session-id", default="")

    import_markdown_vault = subparsers.add_parser(
        "import-markdown-vault",
        help="Import an edited markdown vault back into the graph.",
    )
    import_markdown_vault.add_argument("--root-path", required=True)

    backfill_windows = subparsers.add_parser(
        "backfill-windows",
        help="Retroactively assign legacy nodes to context windows grouped by project/session.",
    )
    backfill_windows.add_argument(
        "--dry-run",
        action="store_true",
        help="Show backfill stats without assigning nodes or creating windows.",
    )

    ingest_transcript_handoff = subparsers.add_parser(
        "ingest-transcript-handoff",
        help="Ingest a full session transcript as a rollover handoff, extract memory, export a context bundle, and emit a session checkpoint.",
        description=(
            "Client-triggered rollover handoff: pass the full ordered transcript as JSON, "
            "Waggle stores all messages as transcript provenance, extracts durable memory from "
            "logical user->assistant turns, exports a session-scoped context bundle, and emits a session-scoped .abhi checkpoint. "
            "Supported backend: SQLite only in v1. Neo4j support is deferred."
        ),
    )
    ingest_transcript_handoff.add_argument(
        "--input",
        default="-",
        metavar="PATH_OR_DASH",
        help="Path to the JSON transcript file, or '-' to read from stdin (default: stdin).",
    )
    ingest_transcript_handoff.add_argument("--project", default="", help="Scope override: project name.")
    ingest_transcript_handoff.add_argument("--agent-id", default="", help="Scope override: agent identifier.")
    ingest_transcript_handoff.add_argument("--session-id", default="", help="Scope override: session identifier.")
    ingest_transcript_handoff.add_argument("--output-path", default=None, help="Optional export output path prefix.")
    ingest_transcript_handoff.add_argument(
        "--export-format",
        choices=["markdown", "json", "both"],
        default="both",
        help="Export format for the post-ingestion context bundle (default: both).",
    )
    ingest_transcript_handoff.add_argument(
        "--max-nodes",
        type=int,
        default=25,
        help="Maximum nodes included in the exported context bundle (default: 25).",
    )
    ingest_transcript_handoff.add_argument(
        "--max-input-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="Hard input-size cap in bytes (default: 16 MiB). Oversized payloads fail with exit code 1.",
    )

    setup = subparsers.add_parser(
        "setup",
        help="Non-interactive one-line setup — auto-patch supported MCP clients.",
        description=(
            "Patch supported MCP client config files without prompts. "
            "By default, --clients auto updates detected clients; use --yes to run from install scripts."
        ),
    )
    setup.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactive setup. Required unless --dry-run is used.",
    )
    setup.add_argument(
        "--clients",
        default="auto",
        help=(
            "Comma-separated clients to configure, or 'auto'. "
            "Supported: codex, claude-desktop, cursor, gemini, antigravity, other."
        ),
    )
    setup.add_argument("--db", default="", help="Database path. Default: ~/.waggle/waggle.db")
    setup.add_argument(
        "--model",
        default="all-MiniLM-L6-v2",
        help="Embedding model for client env. Use 'deterministic' for offline-safe startup.",
    )
    setup.add_argument(
        "--project-instructions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write supported project instruction files, currently Codex AGENTS.md.",
    )
    setup.add_argument("--dry-run", action="store_true", help="Show what would change without writing files.")
    setup.add_argument("--run-doctor", action=argparse.BooleanOptionalAction, default=True)
    setup.add_argument(
        "--hooks",
        default="auto",
        help=(
            "Which hook-capable tools to install automatic memory hooks for: "
            "'auto' (all detected, currently Claude Code), 'none', or a comma-separated "
            "list (currently supported: claude-code)."
        ),
    )
    setup.add_argument(
        "--no-hooks",
        action="store_true",
        help="Deprecated alias for --hooks none. Skip all hook installation.",
    )

    subparsers.add_parser("init", help="Interactive setup wizard — configure an MCP client to use waggle-mcp.")
    subparsers.add_parser(
        "features",
        help="Explain the main tools, graph workflows, and how connected context reaches the model.",
        description="Print a detailed guide to the waggle-mcp feature surface.",
    )
    doctor = subparsers.add_parser(
        "doctor",
        help="Check your Waggle installation: config files, embedding model status, DB path, and common mistakes.",
        description=(
            "Inspect the waggle-mcp environment and surface any configuration issues before they become runtime errors. "
            "Checks: config file locations per MCP client, embedding model cache, DB path writability, "
            "WAGGLE_STARTUP_MODE, stdout encoding (Windows), and known API gotchas."
        ),
    )
    doctor.add_argument(
        "--fix", action="store_true", help="Re-embed stale transcript/node rows to the current model ID."
    )
    doctor.add_argument(
        "--json",
        "--as-json",
        dest="json_output",
        action="store_true",
        help="Print a machine-readable JSON report instead of the human-readable doctor output.",
    )

    demo_cmd = subparsers.add_parser(
        "demo",
        help="Run a 60-second local demo with a pre-loaded example graph. No MCP client required.",
        description=(
            "Import the bundled example graph (examples/demo.abhi) into a temporary SQLite DB, "
            "run 4 scripted queries, and print results. Uses WAGGLE_MODEL=deterministic by default "
            "for instant startup. Pass --with-embeddings to use the real sentence-transformers model."
        ),
    )
    demo_cmd.add_argument(
        "--with-embeddings",
        action="store_true",
        help="Use the real sentence-transformers model instead of deterministic mode.",
    )

    subparsers.add_parser(
        "uninstall-hooks",
        help="Remove the Waggle managed hooks block from Claude Code settings.",
        description="Idempotent: removes the waggle-managed block from ~/.claude/settings.json if present.",
    )

    return parser


def _run_graph_editor_command(config: AppConfig, args: argparse.Namespace) -> int:
    host = str(getattr(args, "host", "") or config.http_host or "127.0.0.1")
    port = int(getattr(args, "port", 8686) or 8686)
    should_open = bool(getattr(args, "open", True))
    mode = str(getattr(args, "command", "edit-graph")).strip().lower()
    page_mode = "view" if mode == "view-graph" else "edit"

    config.transport = "http"
    config.http_host = host
    config.http_port = port
    app_server = WaggleServer(config=config)
    http_app = create_http_application(app_server, config)
    url = f"http://{host}:{port}/graph?mode={page_mode}"

    print(f"Launching Waggle Graph Studio at {url}")
    print("Use Ctrl+C in this terminal to stop the editor server.")

    if should_open:

        def _open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("graph_editor_open_failed", extra={"error": str(exc), "url": url})

        timer = threading.Timer(0.4, _open_browser)
        timer.daemon = True
        timer.start()

    uvicorn.run(http_app, host=host, port=port, log_level=config.log_level.lower())
    return 0


def _normalize_pull_strategy_args(
    args: argparse.Namespace,
) -> argparse.Namespace:
    """Normalize current and legacy strategy arguments for Drive pull."""
    if getattr(args, "command", "") != "pull":
        return args

    raw_merge_strategy = getattr(args, "merge_strategy", None)
    explicit_import_strategy = getattr(args, "import_strategy", None)

    if raw_merge_strategy in _ABHI_IMPORT_STRATEGIES:
        if explicit_import_strategy is None:
            args.import_strategy = raw_merge_strategy
            warning_message = (
                f"`--merge-strategy {raw_merge_strategy}` is deprecated for "
                "selecting the graph import strategy. Use "
                f"`--import-strategy {raw_merge_strategy}` instead."
            )
        else:
            warning_message = (
                f"Legacy `--merge-strategy {raw_merge_strategy}` is deprecated "
                "and has been ignored because `--import-strategy` was also "
                "supplied."
            )

        args.merge_strategy = DEFAULT_ABHI_MERGE_STRATEGY
        warnings.warn(
            warning_message,
            FutureWarning,
            stacklevel=2,
        )
    else:
        args.merge_strategy = raw_merge_strategy or DEFAULT_ABHI_MERGE_STRATEGY
        if explicit_import_strategy is None:
            args.import_strategy = "skip-existing"

    return args


def _run_admin_command(config: AppConfig, args: argparse.Namespace) -> int:
    _CLI_COMMAND_ALIASES: dict[str, str] = {
        "commit": "export",
        "fsck": "validate",
        "show": "inspect",
        "grep": "query",
    }
    if args.command in _CLI_COMMAND_ALIASES:
        args.command = _CLI_COMMAND_ALIASES[args.command]
    if args.command == "doctor":
        return _run_doctor_command(config, args)
    backend = _build_backend(config)
    if args.command == "create-tenant":
        tenant = backend.ensure_tenant(args.tenant_id, args.name)
        print(json.dumps(tenant.model_dump(), indent=2))
        return 0
    if args.command == "create-api-key":
        expires_in_days = int(getattr(args, "expires_in_days", 0) or 0)
        expires_at = utc_now() + timedelta(days=expires_in_days) if expires_in_days > 0 else None
        tenant_backend = backend.for_tenant(args.tenant_id)
        created = tenant_backend.create_api_key(
            args.tenant_id,
            args.name,
            expires_at=expires_at,
            created_by=str(getattr(args, "created_by", "") or "").strip(),
            scopes=_parse_api_key_scopes(getattr(args, "scopes", "")) or None,
        )
        tenant_backend.emit_audit_event(
            event_type="api_key.created",
            actor_type="admin",
            actor_id=str(getattr(args, "created_by", "") or "").strip() or "local-cli",
            resource_type="api_key",
            resource_id=created.record.api_key_id,
            action="create",
            metadata={
                "name": created.record.name,
                "prefix": created.record.prefix,
                "expires_at": created.record.expires_at.isoformat() if created.record.expires_at else None,
                "scopes": created.record.scopes,
            },
        )
        print(
            json.dumps(
                {
                    "api_key_id": created.record.api_key_id,
                    "tenant_id": created.record.tenant_id,
                    "prefix": created.record.prefix,
                    "name": created.record.name,
                    "status": created.record.status,
                    "expires_at": created.record.expires_at.isoformat() if created.record.expires_at else None,
                    "created_by": created.record.created_by,
                    "scopes": created.record.scopes,
                    "raw_api_key": created.raw_api_key,
                },
                indent=2,
            )
        )
        return 0
    if args.command == "list-api-keys":
        print(
            json.dumps(
                [_serialize_api_key_record(record) for record in backend.list_api_keys(args.tenant_id)], indent=2
            )
        )
        return 0
    if args.command == "revoke-api-key":
        tenant_backend = backend.for_tenant(getattr(args, "tenant_id", "") or config.default_tenant_id)
        tenant_backend.revoke_api_key(args.api_key_id)
        tenant_backend.emit_audit_event(
            event_type="api_key.revoked",
            actor_type="admin",
            actor_id="local-cli",
            resource_type="api_key",
            resource_id=args.api_key_id,
            action="revoke",
        )
        print(json.dumps({"revoked": args.api_key_id}))
        return 0
    if args.command in {
        "retention-status",
        "set-retention",
        "prune-retention",
        "list-retention-runs",
        "list-audit-events",
    }:
        retention_backend = backend.for_tenant(getattr(args, "tenant_id", "") or config.default_tenant_id)
        policy_kwargs = {
            "default_enabled": config.retention_enabled,
            "default_retention_days": config.retention_days,
            "default_prune_interval_hours": config.retention_prune_interval_hours,
        }
        if args.command == "retention-status":
            policy = retention_backend.get_retention_policy(**policy_kwargs)
            payload = _serialize_retention_policy(policy)
            payload["recent_runs"] = [
                _serialize_retention_run(run) for run in retention_backend.list_retention_runs(limit=5)
            ]
            print(json.dumps(payload, indent=2))
            return 0
        if args.command == "set-retention":
            policy = retention_backend.update_retention_policy(
                enabled=getattr(args, "enabled", None),
                retention_days=getattr(args, "days", None),
                prune_interval_hours=getattr(args, "interval_hours", None),
                **policy_kwargs,
            )
            retention_backend.emit_audit_event(
                event_type="retention.policy.updated",
                actor_type="admin",
                actor_id="local-cli",
                resource_type="retention_policy",
                resource_id=policy.tenant_id,
                action="update",
                metadata={
                    "enabled": policy.enabled,
                    "retention_days": policy.retention_days,
                    "prune_interval_hours": policy.prune_interval_hours,
                },
            )
            print(json.dumps(_serialize_retention_policy(policy), indent=2))
            return 0
        if args.command == "prune-retention":
            run = retention_backend.prune_retention(
                batch_size=getattr(args, "batch_size", 1000),
                **policy_kwargs,
            )
            payload = _serialize_retention_run(run)
            payload["policy"] = _serialize_retention_policy(retention_backend.get_retention_policy(**policy_kwargs))
            print(json.dumps(payload, indent=2))
            return 0
        if args.command == "list-audit-events":
            events = retention_backend.list_audit_events(
                limit=getattr(args, "limit", 100),
                event_type=getattr(args, "event_type", ""),
                actor_id=getattr(args, "actor_id", ""),
                resource_id=getattr(args, "resource_id", ""),
                resource_type=getattr(args, "resource_type", ""),
                status=getattr(args, "status", ""),
            )
            print(json.dumps([_serialize_audit_event(event) for event in events], indent=2))
            return 0
        runs = retention_backend.list_retention_runs(limit=getattr(args, "limit", 20))
        print(json.dumps([_serialize_retention_run(run) for run in runs], indent=2))
        return 0
    if args.command == "migrate-sqlite":
        if config.backend != "neo4j":
            raise ValidationFailure("migrate-sqlite requires WAGGLE_BACKEND=neo4j for the target environment.")
        source = MemoryGraph(
            args.db_path,
            EmbeddingModel(
                config.model_name,
                embedding_backend=config.embedding_backend,
            ),
            tenant_id=args.tenant_id,
            export_dir=config.export_dir,
        )
        target = backend.for_tenant(args.tenant_id)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            temp_path = Path(handle.name)
        backup = source.export_graph_backup(output_path=temp_path)
        imported = target.import_graph_backup(input_path=temp_path)
        print(
            json.dumps(
                {
                    "backup": backup.model_dump(),
                    "import": imported.model_dump(),
                },
                indent=2,
            )
        )
        temp_path.unlink(missing_ok=True)
        return 0
    if args.command == "export":
        _assert_export_safe(
            backend,
            force=bool(getattr(args, "force", False)),
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
        )
        exported = backend.export_abhi(
            output_path=args.output_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
            include_embeddings=bool(getattr(args, "include_embeddings", True)),
            passphrase=_resolve_passphrase(args),
            redact_patterns=list(getattr(args, "redact_patterns", []) or []),
            sign=bool(getattr(args, "sign", False)),
            signing_key_dir=getattr(args, "signing_key_dir", "~/.waggle/keys"),
            strict_export=bool(getattr(args, "strict_export", False)),
            include_deps=bool(getattr(args, "include_deps", False)),
        )
        print(json.dumps(exported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "checkpoint-context":
        scope = getattr(args, "scope", "") or ""
        if not scope:
            if getattr(args, "session_id", ""):
                scope = "session"
            elif getattr(args, "project", ""):
                scope = "project"
            elif getattr(args, "since_date", ""):
                scope = "since-date"
            else:
                scope = "all"
        _assert_export_safe(
            backend,
            force=bool(getattr(args, "force", False)),
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=scope,
            since_date=getattr(args, "since_date", ""),
        )
        exported = backend.export_abhi(
            output_path=args.output_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=scope,
            since_date=getattr(args, "since_date", ""),
            include_embeddings=bool(getattr(args, "include_embeddings", True)),
            passphrase=_resolve_passphrase(args),
            redact_patterns=list(getattr(args, "redact_patterns", []) or []),
            sign=bool(getattr(args, "sign", False)),
            signing_key_dir=getattr(args, "signing_key_dir", "~/.waggle/keys"),
            strict_export=bool(getattr(args, "strict_export", False)),
            include_deps=bool(getattr(args, "include_deps", False)),
        )
        payload = exported.model_dump(mode="json")
        payload["checkpoint_scope"] = scope
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "clear-session":
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not bool(getattr(args, "yes", False)):
            raise ValidationFailure("clear-session is destructive and requires --yes.")
        cleared = backend.clear_session(session_id=args.session_id, dry_run=dry_run)
        print(json.dumps(cleared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "clear-project":
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not bool(getattr(args, "yes", False)):
            raise ValidationFailure("clear-project is destructive and requires --yes.")
        cleared = backend.clear_project(project=args.project, dry_run=dry_run)
        print(json.dumps(cleared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "clear-all":
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not bool(getattr(args, "yes", False)):
            raise ValidationFailure("clear-all is destructive and requires --yes.")
        cleared = backend.clear_all(dry_run=dry_run)
        print(json.dumps(cleared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "import":
        input_path = getattr(args, "input_path", None) or getattr(args, "input_path_flag", "")
        imported = backend.import_abhi(
            input_path=input_path,
            passphrase=_resolve_passphrase(args),
            namespace=getattr(args, "namespace", ""),
            merge_strategy=getattr(args, "merge_strategy", "skip-existing"),
            verify_signature=bool(getattr(args, "verify_signature", False)),
            read_only=bool(getattr(args, "read_only", False)),
            reembed_on_mismatch=bool(getattr(args, "reembed_on_mismatch", False)),
        )
        print(json.dumps(imported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "validate":
        validation = backend.validate_abhi(input_path=args.input_path, passphrase=_resolve_passphrase(args))
        print(json.dumps(validation.model_dump(mode="json"), indent=2))
        return 0 if validation.valid else 1
    if args.command == "inspect":
        inspected = backend.inspect_abhi(input_path=args.input_path, passphrase=_resolve_passphrase(args))
        print(json.dumps(inspected.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "diff":
        input_path_a = getattr(args, "input_path_a", None) or getattr(args, "input_path_a_flag", "")
        input_path_b = getattr(args, "input_path_b", None) or getattr(args, "input_path_b_flag", "")
        diff = backend.diff_abhi(input_path_a=input_path_a, input_path_b=input_path_b)
        print(json.dumps(diff.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "merge":
        left_input_path = getattr(args, "left_input_path_flag", "") or args.left_input_path
        right_input_path = getattr(args, "right_input_path_flag", "") or args.right_input_path
        merged = backend.merge_abhi(
            base_input_path=args.base_input_path or left_input_path,
            left_input_path=left_input_path,
            right_input_path=right_input_path,
            output_path=args.output_path,
            merge_strategy=args.merge_strategy,
        )
        print(json.dumps(merged.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "query":
        queried = backend.query_abhi(
            input_path=args.input_path,
            query_id=getattr(args, "query_id", ""),
            query_text=getattr(args, "query_text", ""),
            passphrase=_resolve_passphrase(args),
        )
        print(json.dumps(queried.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "load-chunks":
        loaded = backend.load_abhi_chunks(
            input_path=args.input_path,
            chunk_ids=getattr(args, "chunk_ids", []),
            query_id=getattr(args, "query_id", ""),
            query_text=getattr(args, "query_text", ""),
            passphrase=_resolve_passphrase(args),
        )
        print(json.dumps(loaded.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "push":
        _require_drive_sync()
        passphrase = _resolve_passphrase(args)
        _assert_export_safe(
            backend,
            force=bool(getattr(args, "force", False)),
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
        )
        exported = backend.export_abhi(
            output_path=args.output_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            scope=getattr(args, "scope", "all"),
            since_date=getattr(args, "since_date", ""),
            include_embeddings=bool(getattr(args, "include_embeddings", True)),
            passphrase=passphrase,
        )
        credentials = ensure_drive_credentials(
            client_secret_path=args.client_secret_path,
            token_path=_resolve_drive_token_path(args, config),
            open_browser=bool(getattr(args, "open_browser", True)),
        )
        pushed = push_file_to_drive(
            local_path=exported.output_path,
            folder_id=str(getattr(args, "folder_id", "") or ""),
            credentials=credentials,
            remote_name=str(getattr(args, "remote_name", "") or ""),
            encrypted=bool(passphrase),
        )
        print(json.dumps(pushed.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "pull":
        _require_drive_sync()
        passphrase = _resolve_passphrase(args)
        credentials = ensure_drive_credentials(
            client_secret_path=args.client_secret_path,
            token_path=_resolve_drive_token_path(args, config),
            open_browser=bool(getattr(args, "open_browser", True)),
        )
        remote_file_id, remote_name = resolve_drive_file_id(
            file_ref=args.file_ref,
            credentials=credentials,
            folder_id=str(getattr(args, "folder_id", "") or ""),
        )
        download_path = Path(getattr(args, "download_path", "") or backend.export_dir / f"{remote_file_id}.abhi")
        _, resolved_name = download_drive_file(
            file_id=remote_file_id,
            destination_path=download_path,
            credentials=credentials,
        )
        local_document = build_abhi_document(backend.get_graph_snapshot())
        remote_document = load_abhi_document(download_path, passphrase=passphrase)
        merged_output = Path(getattr(args, "merged_output", "") or backend.export_dir / f"merged-{remote_file_id}.abhi")
        selected_merge_strategy = getattr(args, "merge_strategy", None) or DEFAULT_ABHI_MERGE_STRATEGY
        selected_import_strategy = getattr(args, "import_strategy", None) or "skip-existing"

        merged_path = merge_downloaded_abhi(
            local_document=local_document,
            remote_document=remote_document,
            output_path=merged_output,
            merge_strategy=selected_merge_strategy,
        )
        imported = backend.import_abhi(
            input_path=merged_path,
            passphrase=passphrase,
            namespace=getattr(args, "namespace", ""),
            merge_strategy=selected_import_strategy,
            verify_signature=bool(getattr(args, "verify_signature", False)),
            read_only=bool(getattr(args, "read_only", False)),
            reembed_on_mismatch=bool(getattr(args, "reembed_on_mismatch", False)),
        )
        from waggle.models import DrivePullResult

        result = DrivePullResult(
            remote_file_id=remote_file_id,
            remote_name=resolved_name or remote_name,
            downloaded_path=str(download_path),
            merged_output_path=merged_path,
            merge_strategy=selected_merge_strategy,
            nodes_created=imported.nodes_created,
            nodes_updated=imported.nodes_updated,
            edges_created=imported.edges_created,
            edges_updated=imported.edges_updated,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "share":
        _require_drive_sync()
        credentials = ensure_drive_credentials(
            client_secret_path=args.client_secret_path,
            token_path=_resolve_drive_token_path(args, config),
            open_browser=bool(getattr(args, "open_browser", True)),
        )
        remote_file_id, _ = resolve_drive_file_id(
            file_ref=args.file_ref,
            credentials=credentials,
            folder_id=str(getattr(args, "folder_id", "") or ""),
        )
        shared = share_drive_file(file_id=remote_file_id, credentials=credentials)
        print(json.dumps(shared.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "export-context-bundle":
        exported = backend.export_context_bundle(
            mode=args.mode,
            query=args.query,
            project=args.project,
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
            max_nodes=args.max_nodes,
            max_depth=args.max_depth,
            retrieval_mode=getattr(args, "retrieval_mode", "graph"),
            format=args.format,
            output_path=args.output_path,
            include_edges=args.include_edges,
            include_timestamps=args.include_timestamps,
            include_source_prompt=args.include_source_prompt,
            audience=args.audience,
        )
        print(json.dumps(exported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "export-markdown-vault":
        exported = backend.export_markdown_vault(
            root_path=args.root_path,
            project=getattr(args, "project", ""),
            agent_id=getattr(args, "agent_id", ""),
            session_id=getattr(args, "session_id", ""),
        )
        print(json.dumps(exported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "import-markdown-vault":
        imported = backend.import_markdown_vault(root_path=args.root_path)
        print(json.dumps(imported.model_dump(mode="json"), indent=2))
        return 0
    if args.command == "backfill-windows":
        from waggle.backfill import backfill_context_windows

        if not isinstance(backend, MemoryGraph):
            raise ValidationFailure("backfill-windows is currently supported only for the SQLite backend.")
        stats = backfill_context_windows(backend, dry_run=bool(args.dry_run))
        print(json.dumps(stats.model_dump(mode="json"), indent=2))
        return 1 if stats.errors else 0
    if args.command == "ingest-transcript-handoff":
        return _run_ingest_transcript_handoff(config, args)
    if args.command == "features":
        print(_FEATURES_GUIDE)
        return 0
    if args.command == "doctor":
        return _run_doctor_command(config, args)
    raise ValidationFailure(f"Unknown command: {args.command}")


def _run_doctor_command(config: AppConfig, args: argparse.Namespace) -> int:
    return _run_doctor(
        config,
        fix=bool(getattr(args, "fix", False)),
        json_output=bool(getattr(args, "json_output", False)),
    )


def _run_doctor(config: AppConfig, *, fix: bool = False, json_output: bool = False) -> int:
    """waggle-mcp doctor — surface configuration and environment issues."""
    issues: list[str] = []
    warnings: list[str] = []
    ok_items: list[str] = []
    checks: dict[str, dict[str, Any]] = {}

    def emit(*args: object, **kwargs: object) -> None:
        if not json_output:
            print(*args, **kwargs)

    def ok(message: str) -> None:
        if not json_output:
            _ok(message)

    def fail(message: str) -> None:
        if not json_output:
            _fail(message)

    from .utils import _BOLD, _CYAN, _GREEN, _RED

    emit()
    emit(_c(_BOLD, "waggle-mcp doctor"))
    emit(_c(_CYAN, "─" * 50))

    # ── 1. Config file locations ─────────────────────────────────────────────
    emit(_c(_BOLD, "\n[1] MCP client config files"))
    waggle_found_in: list[str] = []
    for label, template in _KNOWN_CONFIG_PATHS:
        raw = template.replace("%APPDATA%", os.environ.get("APPDATA", "")).replace("%USERPROFILE%", str(Path.home()))
        path = Path(raw).expanduser()
        exists = path.exists()
        has_waggle = False
        if exists:
            try:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
                if path.suffix == ".toml":
                    has_waggle = "[mcp_servers.waggle]" in raw_text
                else:
                    data = json.loads(raw_text)
                    servers = data.get("mcpServers", data.get("tools", {}) if isinstance(data, dict) else {})
                    has_waggle = isinstance(servers, dict) and "waggle" in servers
                if has_waggle:
                    waggle_found_in.append(label)
                    ok(f"{label}\n     {path}  [waggle entry found]")
                else:
                    emit(f"  {_c(_CYAN, chr(0x2022))} {label}\n     {path}  [exists, no waggle entry]")
            except Exception:
                emit(f"  {_c(_CYAN, chr(0x2022))} {label}\n     {path}  [exists, could not parse]")
        # only show missing paths that are plausible for this platform
        elif (
            (
                sys.platform == "darwin"
                and ("macOS" in label or "Cursor" in label or "Antigravity" in label or "Codex" in label)
            )
            or (sys.platform == "win32" and "Windows" in label)
            or (sys.platform.startswith("linux") and ("Linux" in label or "Cursor" in label))
        ):
            emit(f"  {_c(_CYAN, chr(0x2022))} {label}\n     {path}  [not found]")

    if not waggle_found_in:
        issues.append(
            "No MCP client config file contains a 'waggle' server entry. "
            "Run 'waggle-mcp setup --yes' to create one, or add it manually."
        )
        checks["mcp_config"] = {
            "status": "fail",
            "reason": "No MCP client config file contains a 'waggle' server entry.",
        }
    else:
        ok_items.append(f"Waggle found in: {', '.join(waggle_found_in)}")
        checks["mcp_config"] = {"status": "ok", "found_in": waggle_found_in}

    # ── 2. DB path ───────────────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[2] Database path"))
    db_path = Path(config.db_path)
    db_dir = db_path.parent
    db_exists = db_path.exists()
    db_dir_exists = db_dir.exists()
    if db_exists:
        ok(f"DB file exists: {db_path}")
        ok_items.append("DB file found")
        checks["db_connection"] = {"status": "ok", "path": str(db_path)}
    elif db_dir_exists:
        ok(f"DB directory exists (file will be created on first run): {db_path}")
        ok_items.append("DB directory writable")
        checks["db_connection"] = {"status": "ok", "path": str(db_path)}
    else:
        issues.append(f"DB directory does not exist: {db_dir}. Create it with: mkdir -p <dir>")
        fail(f"DB directory missing: {db_dir}")
        checks["db_connection"] = {
            "status": "fail",
            "reason": f"DB directory does not exist: {db_dir}. Create it with: mkdir -p <dir>",
        }

    # ── 3. Embedding model ───────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[3] Embedding model"))
    model_name = config.model_name
    is_deterministic = model_name.strip().lower() in {"fake", "fake-model", "deterministic", "offline-demo"}
    is_cached = False
    hf_home = (
        os.environ.get("HF_HOME")
        or os.environ.get("SENTENCE_TRANSFORMERS_HOME")
        or str(Path.home() / ".cache" / "huggingface")
    )
    st_cache = Path(os.environ.get("SENTENCE_TRANSFORMERS_HOME", Path(hf_home) / "hub"))

    if is_deterministic:
        ok(f"Model: {model_name!r}  (deterministic — no download, always offline-safe)")
        ok_items.append("Deterministic model — no download needed")
        checks["embedding_model"] = {"status": "ok", "model_id": model_name}
    else:
        # Heuristic: look for a cached sentence-transformers directory
        safe_name = model_name.replace("/", "_").replace("\\", "_")
        possible_dirs = [
            st_cache / f"models--{safe_name.replace('_', '--', 1)}",
            st_cache / safe_name,
            Path(hf_home) / "hub" / f"models--{safe_name.replace('_', '--', 1)}",
        ]
        is_cached = any(p.exists() for p in possible_dirs)
        if is_cached:
            ok(f"Model: {model_name!r}  (cached locally — fast startup)")
            ok_items.append("Embedding model cached")
            checks["embedding_model"] = {"status": "ok", "model_id": model_name}
        else:
            emit(
                f"  {_c(_CYAN, chr(0x2139))} Model: {model_name!r}  — NOT found in local cache.\n"
                f"    First store_node/query_graph call will download ~420 MB from HuggingFace.\n"
                f"    To avoid: set WAGGLE_MODEL=deterministic, or pre-download with:\n"
                f"      python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('{model_name}')\""
            )
            reason = (
                f"Embedding model '{model_name}' not found in cache. "
                "First semantic call will block for a network download. "
                "Set WAGGLE_MODEL=deterministic for offline-safe mode."
            )
            warnings.append(reason)
            checks["embedding_model"] = {"status": "warn", "model_id": model_name, "reason": reason}

    # ── 4. Embedding store ───────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[4] Embedding store"))
    checks["graph_schema"] = {"status": "ok"}
    try:
        graph = _default_graph(config)
        if isinstance(graph, MemoryGraph):
            store_health = graph.get_embedding_store_health()
            if fix and (store_health["transcript_stale_rows"] or store_health["node_stale_rows"]):
                repair = graph.reembed_stale_embeddings(batch_size=100)
                ok(
                    "Re-embedded stale rows: "
                    f"{repair['transcript_rows_updated']} transcript rows, {repair['node_rows_updated']} node rows."
                )
                ok_items.append("Stale embeddings re-embedded")
                store_health = graph.get_embedding_store_health()

            checksum_failures = (
                store_health["node_checksum_failures"]
                + store_health["transcript_checksum_failures"]
                + store_health["window_checksum_failures"]
            )
            if fix and checksum_failures:
                cleared = graph.clear_corrupt_embeddings()
                rebuilt = graph.reembed_stale_embeddings(batch_size=100)
                windows_rebuilt = graph.recompute_stale_window_embeddings()
                ok(
                    "Cleared corrupt embeddings: "
                    f"{cleared['transcript_records']} transcript, {cleared['nodes']} node, "
                    f"{cleared['context_windows']} window rows; rebuilt "
                    f"{rebuilt['transcript_rows_updated']} transcript, {rebuilt['node_rows_updated']} node and "
                    f"{windows_rebuilt} window embeddings."
                )
                ok_items.append("Corrupt embeddings cleared and rebuilt")
                store_health = graph.get_embedding_store_health()
                checksum_failures = (
                    store_health["node_checksum_failures"]
                    + store_health["transcript_checksum_failures"]
                    + store_health["window_checksum_failures"]
                )
            if fix and store_health["window_stale_rows"]:
                # Repair stale window embeddings even when nothing failed its checksum
                # (membership changes flag windows stale without corrupting anything);
                # the corrupt-clear path above only runs on failures.
                windows_rebuilt = graph.recompute_stale_window_embeddings()
                ok(f"Recomputed stale context-window embeddings: {windows_rebuilt} window rows.")
                if windows_rebuilt:
                    ok_items.append("Stale context-window embeddings recomputed")
                store_health = graph.get_embedding_store_health()
                checksum_failures = (
                    store_health["node_checksum_failures"]
                    + store_health["transcript_checksum_failures"]
                    + store_health["window_checksum_failures"]
                )

            legacy_rows = (
                store_health["node_legacy_rows"]
                + store_health["transcript_legacy_rows"]
                + store_health["window_legacy_rows"]
            )
            transcript_models = store_health["transcript_model_counts"]
            node_models = store_health["node_model_counts"]
            emit(f"  Current model id: {store_health['current_model_id']}")
            emit(f"  Transcript model ids: {transcript_models or '{}'}")
            emit(f"  Node model ids: {node_models or '{}'}")
            emit(f"  Stale transcript rows: {store_health['transcript_stale_rows']}")
            emit(f"  Stale node rows: {store_health['node_stale_rows']}")

            store_status = "ok"
            store_reason: str | None = None
            if store_health["mixed_models"]:
                issues.append("Mixed embedding_model_id values detected in the store.")
                fail("Mixed embedding model IDs detected across transcript_records/nodes.")
                store_status = "fail"
                store_reason = "Mixed embedding_model_id values detected across transcript_records/nodes."
            else:
                ok("Store model IDs are consistent.")
                ok_items.append("Embedding store model IDs consistent")

            if checksum_failures:
                issues.append(f"{checksum_failures} embedding row(s) fail the integrity checksum (corruption).")
                fail(
                    "Embedding checksum failures: "
                    f"{store_health['transcript_checksum_failures']} transcript rows, "
                    f"{store_health['node_checksum_failures']} node rows, "
                    f"{store_health['window_checksum_failures']} window rows. "
                    "Run 'waggle-mcp doctor --fix' to clear and rebuild them."
                )
                store_status = "fail"
                if store_reason is None:
                    store_reason = f"{checksum_failures} embedding row(s) fail the integrity checksum (corruption)."
            elif legacy_rows:
                emit("  No checksum failures in checksummed rows; legacy rows cannot be checksum-verified yet.")
                warnings.append(
                    f"{legacy_rows} embedding row(s) use the legacy pre-checksum format "
                    "and cannot be checksum-verified until rewritten."
                )
                if store_status == "ok":
                    store_status = "warn"
                    store_reason = (
                        f"{legacy_rows} embedding row(s) use the legacy pre-checksum format "
                        "and cannot be checksum-verified until rewritten."
                    )
            else:
                ok("All stored embeddings pass the integrity checksum.")
                ok_items.append("Embedding checksums verified")
            if legacy_rows:
                emit(
                    "  Pre-checksum (legacy) rows: "
                    f"{store_health['transcript_legacy_rows']} transcript, {store_health['node_legacy_rows']} node, "
                    f"{store_health['window_legacy_rows']} window (upgraded automatically on next write)."
                )

            graph_schema_check: dict[str, Any] = {
                "status": store_status,
                "current_model_id": store_health["current_model_id"],
                "transcript_model_counts": transcript_models,
                "node_model_counts": node_models,
                "transcript_stale_rows": store_health["transcript_stale_rows"],
                "node_stale_rows": store_health["node_stale_rows"],
                "window_stale_rows": store_health["window_stale_rows"],
                "mixed_models": store_health["mixed_models"],
                "checksum_failures": checksum_failures,
                "transcript_checksum_failures": store_health["transcript_checksum_failures"],
                "node_checksum_failures": store_health["node_checksum_failures"],
                "window_checksum_failures": store_health["window_checksum_failures"],
                "legacy_rows": legacy_rows,
            }
            if store_reason is not None:
                graph_schema_check["reason"] = store_reason
            checks["graph_schema"] = graph_schema_check
    except Exception as exc:
        message = f"Embedding store check failed: {type(exc).__name__}: {exc}"
        issues.append(message)
        fail(message)
        checks["graph_schema"] = {"status": "fail", "reason": message}

    # ── 5. WAGGLE_STARTUP_MODE ───────────────────────────────────────────────
    emit(_c(_BOLD, "\n[5] Startup mode"))
    emit(f"  WAGGLE_STARTUP_MODE = {config.startup_mode!r}")
    if config.is_fast_mode:
        ok("fast mode: zero ML overhead. Schema/tool listing only. Semantic tools return 'unavailable'.")
        ok_items.append("Startup mode: fast")
        checks["startup_mode"] = {"status": "ok", "mode": "fast"}
    elif config.is_strict_mode:
        ok("strict mode: server blocks on startup until embedding model is ready.")
        ok_items.append("Startup mode: strict")
        checks["startup_mode"] = {"status": "ok", "mode": "strict"}
    else:
        ok("normal mode: embedding loads in background. First semantic call may wait up to ~30 s.")
        ok_items.append("Startup mode: normal")
        checks["startup_mode"] = {"status": "ok", "mode": "normal"}

    # ── 6. Windows stdout encoding ───────────────────────────────────────────
    if sys.platform == "win32":
        emit(_c(_BOLD, "\n[6] Windows stdout encoding"))
        enc = getattr(sys.stdout, "encoding", "unknown")
        is_utf8 = enc.lower().replace("-", "") in ("utf8", "utf8")
        if is_utf8:
            ok(f"stdout encoding: {enc}")
            ok_items.append("Windows stdout is UTF-8")
            checks["stdout_encoding"] = {"status": "ok", "encoding": enc}
        else:
            fail(
                f"stdout encoding is {enc!r} (not UTF-8). "
                "Unicode characters (emoji, accented text) will cause UnicodeEncodeError.\n"
                "    Fix: run with 'python -X utf8' or add at script top:\n"
                "      import sys; sys.stdout.reconfigure(encoding='utf-8')"
            )
            reason = f"Windows stdout encoding is {enc!r} — set PYTHONUTF8=1 or use python -X utf8."
            issues.append(reason)
            checks["stdout_encoding"] = {"status": "fail", "encoding": enc, "reason": reason}
    else:
        checks["stdout_encoding"] = {"status": "ok"}

    # ── 7. Known gotchas ─────────────────────────────────────────────────────
    emit(_c(_BOLD, "\n[7] Known API gotchas"))
    emit(_DOCTOR_KNOWN_GOTCHAS)

    # ── Summary ──────────────────────────────────────────────────────────────
    if json_output:
        summary = {"ok": 0, "warn": 0, "fail": 0}
        for check in checks.values():
            summary[check["status"]] += 1
        result = {
            "version": __version__,
            "checks": checks,
            "summary": summary,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if summary["fail"] else 0

    print(_c(_BOLD, "─" * 50))
    if issues:
        print(_c(_RED, f"Found {len(issues)} issue(s):"))
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        if warnings:
            print()
            print(_c(_CYAN, f"Warnings ({len(warnings)}):"))
            for i, warning in enumerate(warnings, 1):
                print(f"  {i}. {warning}")
        print()
        return 1
    if warnings:
        print(_c(_CYAN, f"Warnings ({len(warnings)}):"))
        for i, warning in enumerate(warnings, 1):
            print(f"  {i}. {warning}")
        print()
        return 0
    else:
        print(_c(_GREEN, f"All checks passed ({len(ok_items)} OK). Waggle looks healthy."))
        print()
        return 0


def _run_ingest_transcript_handoff(config: AppConfig, args: argparse.Namespace) -> int:
    max_bytes: int = args.max_input_bytes
    try:
        input_arg: str = args.input
        if input_arg == "-":
            raw = sys.stdin.buffer.read(max_bytes + 1)
        else:
            path = Path(input_arg)
            if not path.exists():
                _emit_cli_error("input_not_found", f"Input file not found: {input_arg}", {})
                return 1
            raw = path.read_bytes()[: max_bytes + 1]
    except OSError as exc:
        _emit_cli_error("input_read_error", str(exc), {})
        return 1

    if len(raw) > max_bytes:
        _emit_cli_error(
            "payload_too_large",
            f"Input exceeds --max-input-bytes ({max_bytes} bytes).",
            {"max_input_bytes": max_bytes},
        )
        return 1

    try:
        payload_dict = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        _emit_cli_error("malformed_json", f"JSON parse error: {exc}", {})
        return 1

    if not isinstance(payload_dict, dict):
        _emit_cli_error("invalid_format", "Input must be a JSON object.", {})
        return 1

    if "messages" not in payload_dict:
        _emit_cli_error("missing_field", "Input JSON must contain a 'messages' key.", {})
        return 1

    try:
        payload = TranscriptIngestionInput.model_validate(payload_dict)
    except Exception as exc:
        _emit_cli_error("validation_error", str(exc), {})
        return 1

    if getattr(args, "project", ""):
        payload.project = args.project.strip()
    if getattr(args, "agent_id", ""):
        payload.agent_id = args.agent_id.strip()
    if getattr(args, "session_id", ""):
        payload.session_id = args.session_id.strip()

    try:
        backend = _build_backend(config)
        result = backend.ingest_transcript_handoff(
            payload,
            export_format=args.export_format,
            output_path=args.output_path,
            max_nodes=args.max_nodes,
        )
    except ValidationFailure as exc:
        _emit_cli_error("validation_error", str(exc), {})
        return 1
    except WaggleError as exc:
        _emit_cli_error(exc.code, str(exc), {"status_code": exc.status_code})
        return 2
    except Exception as exc:
        _emit_cli_error("backend_error", str(exc), {"type": type(exc).__name__})
        return 2

    output: dict[str, Any] = {
        "scope": {
            "project": result.project,
            "agent_id": result.agent_id,
            "session_id": result.session_id,
        },
        "input_message_count": result.input_message_count,
        "transcript_records_written": result.transcript_records_written,
        "transcript_records_skipped": result.transcript_records_skipped,
        "logical_turns_processed": result.logical_turns_processed,
        "unpaired_trailing_blocks": result.unpaired_trailing_blocks,
        "nodes_created": result.nodes_created,
        "nodes_reused": result.nodes_reused,
        "conflicts": result.conflicts,
    }
    if result.export_skipped:
        output["export_skipped"] = True
        output["export_skipped_reason"] = result.export_skipped_reason
    else:
        output["export_skipped"] = False
        output["markdown_path"] = result.markdown_path
        output["json_path"] = result.json_path
        output["export_node_count"] = result.export_node_count
        output["export_edge_count"] = result.export_edge_count
    if result.checkpoint_path:
        output["checkpoint_path"] = result.checkpoint_path
        output["checkpoint_scope"] = result.checkpoint_scope

    print(json.dumps(output, indent=2))
    return 0


def _prompt_choice(question: str, choices: list[str]) -> str:
    from .utils import _BOLD, _CYAN, _GREEN, _RED

    print(f"\n{_c(_BOLD, question)}")
    for i, choice in enumerate(choices, 1):
        print(f"  {_c(_CYAN, str(i) + '.')} {choice}")
    while True:
        raw = input(f"  Enter number [1-{len(choices)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            selected = choices[int(raw) - 1]
            print(f"  {_c(_GREEN, '>')} {selected}")
            return selected
        print(f"  {_c(_RED, 'Invalid choice, try again.')}")


def _prompt_path(question: str, default: str) -> str:
    from .utils import _BOLD, _GREEN

    print(f"\n{_c(_BOLD, question)}")
    raw = input(f"  [{default}]: ").strip()
    result = raw or default
    print(f"  {_c(_GREEN, '>')} {result}")
    return result


def _write_claude_desktop(db_path: str, python_exe: str) -> Path:
    if sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        config_dir = Path.home() / ".config" / "claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "claude_desktop_config.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_cursor(db_path: str, python_exe: str) -> Path:
    config_dir = Path.home() / ".cursor"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_gemini(db_path: str, python_exe: str) -> Path:
    config_dir = Path.home() / ".gemini"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "settings.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
        "trust": False,
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_antigravity(db_path: str, python_exe: str) -> Path:
    config_dir = Path.home() / ".gemini" / "antigravity"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp_config.json"

    existing: dict = {}
    if config_file.exists():
        with suppress(json.JSONDecodeError):
            existing = json.loads(config_file.read_text())

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["waggle"] = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(existing, indent=2))
    return config_file


def _write_codex(db_path: str, python_exe: str) -> Path:
    config_dir = Path.home() / ".codex"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.toml"
    db_path_escaped = db_path.replace("\\", "\\\\")

    toml_block = (
        "[mcp_servers.waggle]\n"
        f'command = "{_default_stdio_command()}"\n'
        'args = ["serve", "--transport", "stdio"]\n'
        "\n"
        "[mcp_servers.waggle.env]\n"
        'WAGGLE_BACKEND = "sqlite"\n'
        f'WAGGLE_DB_PATH = "{db_path_escaped}"\n'
        'WAGGLE_DEFAULT_TENANT_ID = "local-default"\n'
        'WAGGLE_MODEL = "all-MiniLM-L6-v2"\n'
    )
    existing = config_file.read_text() if config_file.exists() else ""
    pattern = re.compile(r"(?ms)^\[mcp_servers\.waggle\]\n.*?(?=^\[(?!mcp_servers\.waggle(?:\.env)?\])[^\n]+\]\n|\Z)")
    replacement = toml_block.rstrip() + "\n"
    if pattern.search(existing):
        updated = pattern.sub(lambda m: replacement, existing, count=1)
    else:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + replacement
    config_file.write_text(updated)
    return config_file


_AGENTS_MEMORY_BLOCK_HEADER = "<!-- waggle:auto-memory:start -->"
_AGENTS_MEMORY_BLOCK_FOOTER = "<!-- waggle:auto-memory:end -->"
_AGENTS_MEMORY_BLOCK = (
    f"{_AGENTS_MEMORY_BLOCK_HEADER}\n"
    "## Waggle Automatic Memory\n\n"
    f"{AUTOMATIC_MEMORY_RULE_TEXT.rstrip()}\n"
    f"{_AGENTS_MEMORY_BLOCK_FOOTER}\n"
)


def _write_codex_agents(root_dir: Path | None = None) -> Path:
    project_root = (root_dir or Path.cwd()).resolve()
    agents_file = project_root / "AGENTS.md"
    existing = agents_file.read_text() if agents_file.exists() else ""
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(_AGENTS_MEMORY_BLOCK_HEADER)}\n.*?^\s*{re.escape(_AGENTS_MEMORY_BLOCK_FOOTER)}\n?"
    )

    block = _AGENTS_MEMORY_BLOCK
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    else:
        separator = "\n\n" if existing.strip() else ""
        updated = existing.rstrip() + separator + block
    agents_file.write_text(updated.rstrip() + "\n")
    return agents_file


def _write_other(db_path: str, python_exe: str) -> Path:
    config_file = Path.home() / "waggle-mcp-config.json"
    snippet = {
        "command": _default_stdio_command(),
        "args": ["serve", "--transport", "stdio"],
        "env": {
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_DB_PATH": db_path,
            "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            "WAGGLE_MODEL": "all-MiniLM-L6-v2",
        },
    }
    config_file.write_text(json.dumps(snippet, indent=2))
    return config_file


_CLIENT_WRITERS = {
    "Claude Desktop": _write_claude_desktop,
    "Cursor": _write_cursor,
    "Gemini CLI": _write_gemini,
    "Antigravity": _write_antigravity,
    "Codex": _write_codex,
    "Other": _write_other,
}

_RESTART_HINTS = {
    "Claude Desktop": "Restart Claude Desktop to activate.",
    "Cursor": "Reload the Cursor window (Cmd/Ctrl+Shift+P → 'Reload Window') to activate.",
    "Gemini CLI": "Restart Gemini CLI, then run /mcp to confirm Waggle is connected.",
    "Antigravity": "Restart Antigravity to activate the AI agent MCP config.",
    "Codex": "Restart Codex to activate.",
    "Other": "Add the JSON config to your MCP client's server list, then restart it.",
}

_CLIENT_ALIASES = {
    "claude": "Claude Desktop",
    "claude-desktop": "Claude Desktop",
    "claude_desktop": "Claude Desktop",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "gemini-cli": "Gemini CLI",
    "gemini_cli": "Gemini CLI",
    "antigravity": "Antigravity",
    "codex": "Codex",
    "other": "Other",
}


def _client_config_probe_paths() -> dict[str, list[Path]]:
    return {
        "Claude Desktop": [
            Path.home() / ".config" / "claude" / "claude_desktop_config.json",
            Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        ],
        "Cursor": [
            Path.home() / ".cursor" / "mcp.json",
            Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "mcp.json",
        ],
        "Gemini CLI": [Path.home() / ".gemini" / "settings.json"],
        "Antigravity": [Path.home() / ".gemini" / "antigravity" / "mcp_config.json"],
        "Codex": [Path.home() / ".codex" / "config.toml"],
    }


def _normalize_setup_clients(raw_clients: str) -> list[str]:
    clients: list[str] = []
    for raw_client in raw_clients.split(","):
        key = raw_client.strip().lower()
        if not key:
            continue
        client = _CLIENT_ALIASES.get(key)
        if client is None:
            supported = ", ".join(sorted(_CLIENT_ALIASES))
            raise ValidationFailure(f"Unsupported setup client: {raw_client!r}. Supported values: auto, {supported}.")
        if client not in clients:
            clients.append(client)
    if not clients:
        raise ValidationFailure("No setup clients were provided.")
    return clients


def _detect_setup_clients() -> list[str]:
    detected: list[str] = []
    for client, paths in _client_config_probe_paths().items():
        if any(path.exists() for path in paths):
            detected.append(client)
    if not detected and (Path.cwd() / "AGENTS.md").exists():
        detected.append("Codex")
    return detected


def _setup_clients_from_args(raw_clients: str) -> list[str]:
    if raw_clients.strip().lower() == "auto":
        detected = _detect_setup_clients()
        return detected or ["Codex"]
    return _normalize_setup_clients(raw_clients)


_HOOK_TOOL_ALIASES = {
    "claude-code": "Claude Code",
    "claude_code": "Claude Code",
    "claudecode": "Claude Code",
}
_ALL_HOOK_TOOLS = ["Claude Code"]


def _hook_tools_from_args(raw_hooks: str, no_hooks: bool) -> list[str]:
    raw = (raw_hooks or "auto").strip().lower()
    if no_hooks:
        raw = "none"
    if raw == "none":
        return []
    if raw == "auto":
        return list(_ALL_HOOK_TOOLS)
    tools: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if not key:
            continue
        tool = _HOOK_TOOL_ALIASES.get(key)
        if tool is None:
            supported = ", ".join(sorted(_HOOK_TOOL_ALIASES))
            raise ValidationFailure(f"Unsupported hook target: {part!r}. Supported values: auto, none, {supported}.")
        if tool not in tools:
            tools.append(tool)
    if not tools:
        raise ValidationFailure("No hook targets were provided.")
    return tools


_CLAUDE_HOOKS_BLOCK_HEADER = "# >>> waggle-managed >>>"
_CLAUDE_HOOKS_BLOCK_FOOTER = "# <<< waggle-managed <<<"


def _hooks_block(hook_dir: Path) -> str:
    return (
        f"{_CLAUDE_HOOKS_BLOCK_HEADER}\n"
        "# Waggle automatic memory hooks — do not edit this block manually.\n"
        "# Run 'waggle-mcp setup --yes' to update or 'waggle-mcp uninstall-hooks' to remove.\n"
        f"{_CLAUDE_HOOKS_BLOCK_FOOTER}"
    )


def _hooks_json_block(hook_dir: Path) -> dict[str, Any]:
    pre_response = str(hook_dir / "pre_response.py")
    post_response = str(hook_dir / "post_response.py")
    pre_compact = str(hook_dir / "pre_compact.py")
    python_exe = _python_exe()
    return {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_exe} {pre_response}",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_exe} {post_response}",
                        }
                    ],
                }
            ],
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{python_exe} {pre_compact}",
                        }
                    ],
                }
            ],
        }
    }


def _find_claude_settings() -> Path | None:
    candidates = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".config" / "claude" / "settings.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path.home() / ".claude" / "settings.json"


def _install_claude_hooks(hook_dir: Path, *, dry_run: bool = False) -> Path | None:
    settings_path = _find_claude_settings()
    if settings_path is None:
        return None

    hook_data = _hooks_json_block(hook_dir)
    if dry_run:
        return settings_path

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = settings_path.read_text() if settings_path.exists() else "{}"
    try:
        existing = json.loads(existing_text)
    except json.JSONDecodeError:
        existing = {}

    existing_hooks = existing.get("hooks", {})
    for event, entries in hook_data["hooks"].items():
        existing_entries = [
            e
            for e in existing_hooks.get(event, [])
            if not any("waggle" in str(h.get("command", "")) for h in e.get("hooks", []))
        ]
        existing_entries.extend(entries)
        existing_hooks[event] = existing_entries
    existing["hooks"] = existing_hooks

    settings_path.write_text(json.dumps(existing, indent=2))
    return settings_path


def _uninstall_claude_hooks() -> Path | None:
    settings_path = _find_claude_settings()
    if settings_path is None or not settings_path.exists():
        return None

    try:
        existing = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return None

    hooks = existing.get("hooks", {})
    changed = False
    for event in list(hooks.keys()):
        filtered = [
            e for e in hooks[event] if not any("waggle" in str(h.get("command", "")) for h in e.get("hooks", []))
        ]
        if len(filtered) != len(hooks[event]):
            changed = True
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    if not changed:
        return None

    existing["hooks"] = hooks
    settings_path.write_text(json.dumps(existing, indent=2))
    return settings_path


def _run_uninstall_hooks() -> int:
    result = _uninstall_claude_hooks()
    if result is None:
        print("No Waggle hooks found in Claude Code settings (nothing to remove).")
    else:
        _ok(f"Waggle hooks removed from {result}")
    return 0


def _replace_model_in_client_config(config_file: Path, model_name: str) -> None:
    if config_file.suffix == ".toml":
        text = config_file.read_text()
        text = re.sub(r'WAGGLE_MODEL = "[^"]*"', f'WAGGLE_MODEL = "{model_name}"', text)
        config_file.write_text(text)
        return

    try:
        payload = json.loads(config_file.read_text())
    except json.JSONDecodeError:
        return

    if config_file.name == "waggle-mcp-config.json":
        env = payload.setdefault("env", {})
        if isinstance(env, dict):
            env["WAGGLE_MODEL"] = model_name
    else:
        servers = payload.get("mcpServers")
        if isinstance(servers, dict) and isinstance(servers.get("waggle"), dict):
            env = servers["waggle"].setdefault("env", {})
            if isinstance(env, dict):
                env["WAGGLE_MODEL"] = model_name
    config_file.write_text(json.dumps(payload, indent=2))


def _run_demo(args: argparse.Namespace) -> int:
    with_embeddings = bool(getattr(args, "with_embeddings", False))
    model_name = "all-MiniLM-L6-v2" if with_embeddings else "deterministic"

    demo_abhi = Path(__file__).resolve().parent.parent.parent / "examples" / "demo.abhi"
    if not demo_abhi.exists():
        demo_abhi = Path(__file__).resolve().parent / "examples" / "demo.abhi"
    if not demo_abhi.exists():
        demo_abhi = Path.cwd() / "examples" / "demo.abhi"
    if not demo_abhi.exists():
        _fail(
            "Could not find examples/demo.abhi. "
            "Run 'python3 examples/generate_demo_abhi.py' from the repo root to regenerate it."
        )
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix="waggle-demo-"))
    demo_db = tmp_dir / "demo.db"

    from .utils import _BOLD, _CYAN

    print()
    print(_c(_BOLD, "waggle-mcp demo"))
    print(_c(_CYAN, "─" * 50))
    print(f"  graph:   {demo_abhi}")
    print(f"  db:      {demo_db}")
    print(f"  model:   {model_name}")
    print()

    try:
        embedding_model = EmbeddingModel(model_name)
        graph = MemoryGraph(
            str(demo_db),
            embedding_model,
            tenant_id="local-default",
            enable_dedup=False,
        )
        imported = graph.import_abhi(input_path=demo_abhi, merge_strategy="skip-existing")
        print(f"  Imported {imported.nodes_created} nodes, {imported.edges_created} edges from demo.abhi")
        print()

        print(_c(_BOLD, "Query 1: What database did we choose?"))
        result1 = graph.query(query="What database did we choose?", max_nodes=6, max_depth=2)
        if result1.nodes:
            for node in result1.nodes[:4]:
                marker = "  [decision]" if node.node_type.value == "decision" else "  [note]    "
                print(f"{marker} {node.label}")
        else:
            print("  (no results)")
        print()

        print(_c(_BOLD, "Query 2: What changed about the database decision?"))
        result2 = graph.query(
            query="What changed about the database decision? contradiction superseded", max_nodes=8, max_depth=2
        )
        contradiction_edges = [e for e in result2.edges if e.relationship in ("contradicts", "updates")]
        if contradiction_edges:
            for edge in contradiction_edges[:3]:
                src = next((n for n in result2.nodes if n.id == edge.source_id), None)
                tgt = next((n for n in result2.nodes if n.id == edge.target_id), None)
                if src and tgt:
                    print(f"  [{edge.relationship}] {src.label}")
                    print(f"    → {tgt.label}")
        elif result2.nodes:
            for node in result2.nodes[:3]:
                print(f"  {node.label}")
        else:
            print("  (no results)")
        print()

        print(_c(_BOLD, "Query 3: What are our team's preferences?"))
        result3 = graph.query(query="team preferences", max_nodes=8, max_depth=1)
        pref_nodes = [n for n in result3.nodes if n.node_type.value == "preference"]
        if pref_nodes:
            for node in pref_nodes[:3]:
                print(f"  [preference] {node.label}")
        elif result3.nodes:
            for node in result3.nodes[:3]:
                print(f"  {node.label}")
        else:
            print("  (no results)")
        print()

        print(_c(_BOLD, "Query 4: Show decisions and their reasons"))
        result4 = graph.query(query="decisions reasons why", max_nodes=10, max_depth=2)
        decision_nodes = [n for n in result4.nodes if n.node_type.value == "decision"]
        reason_edges = [e for e in result4.edges if e.relationship == "derived_from"]
        if decision_nodes:
            for dec in decision_nodes[:3]:
                print(f"  [decision] {dec.label}")
                reason_ids = {e.target_id for e in reason_edges if e.source_id == dec.id}
                for node in result4.nodes:
                    if node.id in reason_ids:
                        print(f"    ↳ [reason] {node.label}")
        else:
            print("  (no results)")
        print()

        studio_url = "http://127.0.0.1:8686/graph?mode=view"
        print(_c(_CYAN, "─" * 50))
        print(f"  Graph Studio: {studio_url}")
        print(f"  (run 'WAGGLE_DB_PATH={demo_db} waggle-mcp ui' to open it)")
        print()
        print(f"  Cleanup: rm -rf {tmp_dir}")
        print()

    except Exception as exc:
        _fail(f"Demo failed: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


def _run_setup(args: argparse.Namespace) -> int:
    if not args.yes and not args.dry_run:
        _fail("Refusing to patch config without --yes. Use --dry-run to preview changes.")
        return 1

    db_path_raw = args.db or DEFAULT_DB_PATH
    db_path = str(Path(db_path_raw).expanduser().resolve())
    python_exe = _python_exe()
    clients = _setup_clients_from_args(args.clients)
    hook_tools = _hook_tools_from_args(getattr(args, "hooks", "auto"), bool(getattr(args, "no_hooks", False)))

    from .utils import _BOLD, _CYAN

    print()
    print(_c(_BOLD, "waggle-mcp setup"))
    print(_c(_CYAN, "─" * 40))
    print(f"  clients: {', '.join(clients)}")
    print(f"  database: {db_path}")
    print(f"  model: {args.model}")
    if args.dry_run:
        print("  mode: dry-run")
        for client in clients:
            _ok(f"Would configure {client}")
        if "Claude Code" in hook_tools:
            hooks_path = _find_claude_settings()
            _ok(f"Would install Claude Code hooks in {hooks_path}")
        if args.project_instructions and "Codex" in clients:
            agents_path = (Path.cwd() / "AGENTS.md").resolve()
            _ok(f"Would write Codex automatic-memory instructions to {agents_path}")
        print()
        return 0

    for client in clients:
        writer = _CLIENT_WRITERS[client]
        try:
            config_file = writer(db_path, python_exe)
            if args.model != "all-MiniLM-L6-v2":
                _replace_model_in_client_config(config_file, args.model)
            _ok(f"{client} config written to {config_file}")
        except OSError as exc:
            _fail(f"Could not write {client} config: {exc}")
            return 1

    if args.project_instructions and "Codex" in clients:
        try:
            agents_file = _write_codex_agents()
            _ok(f"Codex automatic-memory instructions written to {agents_file}")
        except OSError as exc:
            _fail(f"Could not write AGENTS.md instructions: {exc}")
            return 1

    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _ok(f"Database directory ready at {Path(db_path).parent}")
    except OSError as exc:
        _fail(f"Could not create database directory: {exc}")
        return 1

    for client in clients:
        print(f"  {_c(_CYAN, chr(0x27A1))}  {_RESTART_HINTS[client]}")
    print()

    if hook_tools and not args.dry_run:
        if "Claude Code" in hook_tools:
            hook_dir = Path(__file__).resolve().parent / "hooks" / "claude_code"
            if not hook_dir.exists():
                hook_dir = Path(__file__).resolve().parent.parent / "hooks" / "claude_code"
            if hook_dir.exists():
                try:
                    hooks_path = _install_claude_hooks(hook_dir)
                    if hooks_path is not None:
                        _ok(f"Claude Code hooks installed in {hooks_path}")
                except OSError as exc:
                    LOGGER.warning("claude_hooks_install_failed", extra={"error": str(exc)})

    if args.run_doctor:
        doctor_config = AppConfig.from_env()
        doctor_config.db_path = db_path
        doctor_config.model_name = args.model
        doctor_exit = _run_doctor_command(doctor_config, args)
        if doctor_exit:
            print(_c(_CYAN, "Setup completed; doctor reported follow-up warnings above."))
        return 0
    return 0


def _run_init() -> int:
    from .utils import _BOLD, _CYAN

    print()
    print(_c(_BOLD, "waggle-mcp setup wizard"))
    print(_c(_CYAN, "─" * 40))

    clients = list(_CLIENT_WRITERS.keys())
    client = _prompt_choice("Which MCP client are you using?", clients)

    default_db = DEFAULT_DB_PATH
    db_path_raw = _prompt_path("Where should the database be stored?", default_db)
    db_path = str(Path(db_path_raw).expanduser().resolve())

    python_exe = _python_exe()
    print()

    writer = _CLIENT_WRITERS[client]
    try:
        config_file = writer(db_path, python_exe)
        _ok(f"Config written to {config_file}")
    except OSError as exc:
        _fail(f"Could not write config: {exc}")
        return 1

    if client == "Codex":
        try:
            agents_file = _write_codex_agents()
            _ok(f"Automatic memory instructions written to {agents_file}")
        except OSError as exc:
            _fail(f"Could not write AGENTS.md instructions: {exc}")
            return 1

    db_dir = Path(db_path).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        _ok(f"Database directory ready at {db_dir}")
    except OSError as exc:
        _fail(f"Could not create database directory: {exc}")
        return 1

    print(f"  {_c(_CYAN, chr(0x27A1))}  {_RESTART_HINTS[client]}")
    print()
    return 0


_APP: WaggleServer | None = None


def get_app(config: AppConfig | None = None) -> WaggleServer:
    global _APP
    if _APP is None:
        _APP = WaggleServer(config=config or AppConfig.from_env())
    return _APP


async def run_stdio(config: AppConfig) -> None:
    app = get_app(config)
    graph = app._root_graph
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
    import mcp.server.stdio

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.server.run(read_stream, write_stream, app.initialization_options())


def run_http(config: AppConfig) -> None:
    app_server = get_app(config)
    http_app = create_http_application(app_server, config)
    uvicorn.run(http_app, host=config.http_host, port=config.http_port, log_level=config.log_level.lower())


def print_startup_banner(config: AppConfig, args: argparse.Namespace | None = None) -> None:
    if os.environ.get("WAGGLE_BANNER", "").lower() == "false":
        return
    if args is not None and getattr(args, "quiet", False):
        return
    if not sys.stdout.isatty():
        return

    banner = (
        f"Waggle MCP {__version__}\n"
        f"  Backend:    {config.backend}\n"
        f"  Model:      {config.model_name} ({config.embedding_backend})\n"
        f"  DB path:    {config.db_path}\n"
        f"  Transport:  {config.transport}\n"
        f"  Tenant:     {config.default_tenant_id}"
    )
    print(banner)


def main() -> None:
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    from .utils import _assert_runtime_feature_parity

    _assert_runtime_feature_parity()
    parser = _build_parser()
    args = _normalize_pull_strategy_args(parser.parse_args())
    args.quiet = getattr(args, "global_quiet", False) or getattr(args, "quiet", False)
    command = args.command or "serve"

    if command == "setup":
        try:
            sys.exit(_run_setup(args))
        except ValidationFailure as exc:
            _emit_cli_error("validation_error", str(exc), {})
            sys.exit(1)
    if command == "demo":
        try:
            sys.exit(_run_demo(args))
        except Exception as exc:
            _emit_cli_error("internal_error", str(exc), {"type": type(exc).__name__})
            sys.exit(1)
    if command == "uninstall-hooks":
        sys.exit(_run_uninstall_hooks())
    if command == "init":
        sys.exit(_run_init())
    if command == "features":
        print(_FEATURES_GUIDE)
        return
    if command == "doctor":
        config = AppConfig.from_env()
        sys.exit(_run_admin_command(config, args))

    config = AppConfig.from_env()
    if command == "serve" and getattr(args, "transport", None):
        config.transport = str(args.transport).strip().lower()
        config.validate()
    if command in {"serve", "edit-graph", "view-graph", "ui", "graph-studio", "open-studio"}:
        print_startup_banner(config, args)
    log_stream = sys.stderr if config.transport == "stdio" else sys.stdout
    from waggle.logging_utils import configure_logging

    configure_logging(config.log_level, stream=log_stream)
    LOGGER.info("waggle_startup")
    if command in {"edit-graph", "view-graph", "ui", "graph-studio", "open-studio"}:
        try:
            exit_code = _run_graph_editor_command(config, args)
        except ValidationFailure as exc:
            _emit_cli_error("validation_error", str(exc), {})
            sys.exit(1)
        except WaggleError as exc:
            _emit_cli_error(exc.code, str(exc), {"status_code": exc.status_code})
            sys.exit(2)
        except Exception as exc:
            _emit_cli_error("internal_error", str(exc), {"type": type(exc).__name__})
            sys.exit(3)
        sys.exit(exit_code or 0)
    if command != "serve":
        try:
            exit_code = _run_admin_command(config, args)
        except ValidationFailure as exc:
            _emit_cli_error("validation_error", str(exc), {})
            sys.exit(1)
        except WaggleError as exc:
            _emit_cli_error(exc.code, str(exc), {"status_code": exc.status_code})
            sys.exit(2)
        except Exception as exc:
            _emit_cli_error("internal_error", str(exc), {"type": type(exc).__name__})
            sys.exit(3)
        sys.exit(exit_code or 0)
        return
    if config.transport == "http":
        run_http(config)
        return
    if config.backend == "sqlite":
        Path(config.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run_stdio(config))
