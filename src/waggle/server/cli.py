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
    clear_session.add_argument("--dry-run", action="store_true", help="Preview the clear operation without deleting data.")

    clear_project = subparsers.add_parser(
        "clear-project",
        help="Delete all graph memory data for one project/repository scope.",
    )
    clear_project.add_argument("--project", required=True)
    clear_project.add_argument("--yes", action="store_true", help="Confirm the destructive clear operation.")
    clear_project.add_argument("--dry-run", action="store_true", help="Preview the clear operation without deleting data.")

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

    validate_abhi = subparsers.add_parser("validate", help="Validate a portable .abhi memory file. (alias: fsck)")
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
        help="Inspect an .abhi memory file without importing it. (alias: show)",
        description=(
            "Show summary stats, node/edge type breakdowns, and metadata counts for an .abhi file "
            "without loading it into the graph — like git show for a commit object."
        ),
    )
    show_abhi.add_argument("--input", dest="input_path", required=True)
    show_abhi.add_argument("--passphrase-env", default="")

    diff_abhi = subparsers.add_parser("diff", help="Compare two .abhi memory files.")
    diff_abhi.add_argument("input_path_a", nargs="?")
    diff_abhi.add_argument("input_path_b", nargs="?")
    diff_abhi.add_argument("--file-a", dest="input_path_a_flag", default="")
    diff_abhi.add_argument("--file-b", dest="input_path_b_flag", default="")

    merge_abhi = subparsers.add_parser("merge", help="Three-way merge .abhi memory files.")
    merge_abhi.add_argument("left_input_path", nargs="?")
    merge_abhi.add_argument("right_input_path", nargs="?")
    merge_abhi.add_argument("--base", dest="base_input_path", default="")
    merge_abhi.add_argument("--left", dest="left_input_path_flag", default="")
    merge_abhi.add_argument("--right", dest="right_input_path_flag", default="")
    merge_abhi.add_argument("--output", dest="output_path", required=True)
    merge_abhi.add_argument(
        "--merge-strategy", choices=["prefer_right", "prefer_left", "last_write_wins"], default="prefer_right"
    )

    query_abhi = subparsers.add_parser("query", help="Execute a query against an .abhi memory file. (alias: grep)")
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
    setup.add_argument("--yes", action="store_true", help="Confirm non-interactive setup. Required unless --dry-run is used.")
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
    setup.add_argument("--no-hooks", action="store_true", help="Deprecated alias for --hooks none. Skip all hook installation.")

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
    doctor.add_argument("--fix", action="store_true", help="Re-embed stale transcript/node rows to the current model ID.")
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


def _normalize_pull_strategy_args(args: argparse.Namespace) -> argparse.Namespace:
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
        warnings.warn(warning_message, FutureWarning, stacklevel=2)
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
