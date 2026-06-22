# Waggle Reference

This page keeps the lower-level operational and configuration material out of the top-level README.

## Production evaluation docs

For self-hosted production planning:

- [Production deployment guide](deployment/production.md)
- [Security model](security/security-model.md)
- [Hardening checklist](security/hardening-checklist.md)

## Installation variants

### Local / development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
waggle-mcp setup --yes
```

If `.venv` already exists from a different Python version, remove it and recreate it. Reusing a stale environment can leave wrapper scripts pointing at the wrong interpreter.

When you run `waggle-mcp setup --yes` or `waggle-mcp init` from a Codex workspace, it also writes a managed Waggle automatic-memory block into `AGENTS.md` in the current directory so Codex threads in that repo pick it up by default.

### Neo4j backend

```bash
pip install -e ".[dev,neo4j]"

WAGGLE_TRANSPORT=http \
WAGGLE_BACKEND=neo4j \
WAGGLE_DEFAULT_TENANT_ID=workspace-default \
WAGGLE_NEO4J_URI=bolt://localhost:7687 \
WAGGLE_NEO4J_USERNAME=neo4j \
WAGGLE_NEO4J_PASSWORD=change-me \
waggle-mcp
```

> **Known gap:** `src/waggle/neo4j_graph.py` contains a module-level
> `def update_node(...)` at line 1867 whose body encloses the tail of the
> file (lines 1959–4277) as dead code.  The following methods are defined
> inside this region and are **not** accessible on `Neo4jMemoryGraph`
> instances: `delete_node`, `update_edge`, `delete_edge`,
> `list_recent_nodes`, `list_context_scopes`, `get_stats`,
> `list_transcript_records`, `search_transcript_records`.
> Additionally, `add_node` and `add_edge` call private helpers trapped in
> the same dead-code region and will fail at runtime with
> `AttributeError`.  See `tests/test_neo4j_stubs.py` for the full
> documented list.

### Docker

```bash
docker build -t waggle-mcp:latest .

docker run --rm waggle-mcp:latest --help

docker run --rm -p 8080:8080 \
  -e WAGGLE_TRANSPORT=http \
  -e WAGGLE_BACKEND=neo4j \
  -e WAGGLE_DEFAULT_TENANT_ID=workspace-default \
  -e WAGGLE_NEO4J_URI=bolt://host.docker.internal:7687 \
  -e WAGGLE_NEO4J_USERNAME=neo4j \
  -e WAGGLE_NEO4J_PASSWORD=change-me \
  waggle-mcp:latest
```

## Manual client configuration

### Claude Desktop

```json
{
  "mcpServers": {
    "waggle": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "waggle.server"],
      "env": {
        "WAGGLE_TRANSPORT": "stdio",
        "WAGGLE_BACKEND": "sqlite",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db",
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

### Claude Code

Claude Code supports MCP servers directly. The two practical ways to add Waggle are:

```bash
# Project-local (default)
claude mcp add waggle --scope local --env WAGGLE_TRANSPORT=stdio --env WAGGLE_BACKEND=sqlite --env WAGGLE_DB_PATH=~/.waggle/waggle.db --env WAGGLE_DEFAULT_TENANT_ID=local-default --env WAGGLE_MODEL=all-MiniLM-L6-v2 -- /path/to/.venv/bin/python -m waggle.server

# Shared project config in .mcp.json
claude mcp add waggle --scope project --env WAGGLE_TRANSPORT=stdio --env WAGGLE_BACKEND=sqlite --env WAGGLE_DB_PATH=~/.waggle/waggle.db --env WAGGLE_DEFAULT_TENANT_ID=local-default --env WAGGLE_MODEL=all-MiniLM-L6-v2 -- /path/to/.venv/bin/python -m waggle.server
```

Equivalent `.mcp.json` entry:

```json
{
  "mcpServers": {
    "waggle": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "waggle.server"],
      "env": {
        "WAGGLE_TRANSPORT": "stdio",
        "WAGGLE_BACKEND": "sqlite",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db",
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

Useful Claude Code commands after setup:

```bash
claude mcp list
claude mcp get waggle
```

### Codex

```toml
[mcp_servers.waggle]
command = "/path/to/.venv/bin/python"
args    = ["-m", "waggle.server"]
env     = {
  WAGGLE_TRANSPORT         = "stdio",
  WAGGLE_BACKEND           = "sqlite",
  WAGGLE_DB_PATH           = "~/.waggle/waggle.db",
  WAGGLE_DEFAULT_TENANT_ID = "local-default",
  WAGGLE_MODEL             = "all-MiniLM-L6-v2"
}
```

A pre-filled example is in [examples/codex_config.example.toml](../examples/codex_config.example.toml).

### Gemini CLI

Gemini CLI supports MCP servers through `~/.gemini/settings.json` or the `gemini mcp add` command.

```bash
gemini mcp add waggle \
  -e WAGGLE_TRANSPORT=stdio \
  -e WAGGLE_BACKEND=sqlite \
  -e WAGGLE_DB_PATH=~/.waggle/waggle.db \
  -e WAGGLE_DEFAULT_TENANT_ID=local-default \
  -e WAGGLE_MODEL=all-MiniLM-L6-v2 \
  waggle-mcp serve
```

Equivalent `~/.gemini/settings.json` entry:

```json
{
  "mcpServers": {
    "waggle": {
      "command": "waggle-mcp",
      "args": ["serve"],
      "env": {
        "WAGGLE_TRANSPORT": "stdio",
        "WAGGLE_BACKEND": "sqlite",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db",
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_MODEL": "all-MiniLM-L6-v2"
      },
      "trust": false
    }
  }
}
```

After restarting Gemini CLI, run `/mcp` to confirm Waggle is connected.

### Cursor

Cursor supports MCP in both the editor and the CLI. In the editor, open `Cursor Settings -> Features -> MCP Servers` and add a new stdio server with:

- Name: `waggle`
- Command: `/path/to/.venv/bin/python`
- Arguments: `-m`, `waggle.server`

Environment variables:

```text
WAGGLE_TRANSPORT=stdio
WAGGLE_BACKEND=sqlite
WAGGLE_DB_PATH=~/.waggle/waggle.db
WAGGLE_DEFAULT_TENANT_ID=local-default
WAGGLE_MODEL=all-MiniLM-L6-v2
```

If you prefer JSON configuration, use the same `mcpServers` object shape shown for Claude Desktop above.

### Antigravity

Antigravity supports custom MCP servers through its MCP manager.

Steps:
- Open the agent panel
- Open the `...` menu
- Choose `Manage MCP Servers`
- Choose `View raw config`
- Add Waggle to the config file

Configuration:

```json
{
  "mcpServers": {
    "waggle": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "waggle.server"],
      "env": {
        "WAGGLE_TRANSPORT": "stdio",
        "WAGGLE_BACKEND": "sqlite",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db",
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

## Using Waggle In MCP Clients

After Waggle is installed as an MCP server, the normal workflow is conversational. Users usually do not run `waggle-mcp` commands during everyday work. They talk to the agent normally, and the agent decides when to call Waggle's MCP tools.

### Codex

- Work in a normal Codex thread.
- Codex can use `observe_conversation`, `store_node`, `store_edge`, `query_graph`, and `prime_context` to persist and retrieve memory.
- Later tasks can recover connected graph context even when the original thread is no longer in the current window.
- `waggle-mcp setup --yes` and `waggle-mcp init` write that Codex rule to `AGENTS.md` in the current workspace by default. If you configure Waggle manually, add a Codex instruction/rule telling the agent to call `prime_context` at session start, `query_graph` before context-dependent answers, and `observe_conversation` after durable turns.

### Claude Code

- Claude Code can use Waggle as a persistent MCP memory layer.
- It is useful for carrying decisions, constraints, and project state across sessions.
- `prime_context` and `export_context_bundle` are especially useful when starting a new task or handing work to another model.

### Cursor

- Cursor can use Waggle over MCP while you work in the editor.
- That lets the agent recover earlier facts and connected rationale instead of relying only on the current chat.

### Antigravity

- Antigravity can use Waggle as a persistent graph memory backend over MCP.
- Conversation memory can be extracted with `observe_conversation`, and linked context can be exported with `export_context_bundle`.
- For automatic recall, add a User Rule / custom instruction telling the agent to use `prime_context`, `query_graph`, and `observe_conversation` in the background; `mcp_config.json` alone only registers the tool.

### Important behavior

- `store_node` saves one node directly, but does not create edges by itself.
- Edges come from:
  - explicit `store_edge` calls
  - `observe_conversation`
  - `decompose_and_store`
  - automatic contradiction/update detection in some cases
- The graph-aware retrieval tools are what return connected context to the model:
  - `query_graph`
  - `get_related`
  - `get_node_history`
  - `prime_context`
  - `export_context_bundle`

For a built-in CLI explainer, run:

```bash
waggle-mcp features
```

To check a local installation, run:

```bash
waggle-mcp doctor
```

Automation and bug reports can request structured output:

```bash
waggle-mcp doctor --json
```

`--json` (alias `--as-json`) suppresses the human-readable report and prints a single JSON object to stdout. The exit code is unchanged: `0` if no check has status `fail`, `1` otherwise.

Example output:

```json
{
  "version": "0.0.1",
  "checks": {
    "db_connection": {"status": "ok", "path": "/home/user/.waggle/waggle.db"},
    "embedding_model": {"status": "ok", "model_id": "deterministic"},
    "graph_schema": {"status": "ok"},
    "mcp_config": {
      "status": "fail",
      "reason": "No MCP client config file contains a 'waggle' server entry."
    },
    "startup_mode": {"status": "ok", "mode": "normal"},
    "stdout_encoding": {"status": "ok"}
  },
  "summary": {"ok": 5, "warn": 0, "fail": 1}
}
```

Doctor JSON fields:

| Field | Type | Description |
| --- | --- | --- |
| `version` | string | The installed `waggle-mcp` package version. |
| `checks` | object | One key per check. Each value has a `status` of `"ok"`, `"warn"`, or `"fail"`, plus check-specific fields such as `reason`, `model_id`, `path`, `mode`, or `found_in`. |
| `summary` | object | Counts of checks by status: `{"ok": int, "warn": int, "fail": int}`. |

Checks performed:

| Check | Description |
| --- | --- |
| `mcp_config` | Whether any known MCP client config file has a `waggle` server entry. |
| `db_connection` | Whether the configured database file or its parent directory exists. |
| `embedding_model` | Whether the configured embedding model is deterministic or already cached locally. |
| `graph_schema` | Whether the embedding store's `embedding_model_id` values are consistent (no mixed models). |
| `startup_mode` | The configured `WAGGLE_STARTUP_MODE` (`fast`, `strict`, or `normal`). Always `ok`. |
| `stdout_encoding` | Whether stdout is UTF-8 encoded. The check only runs on Windows; on other platforms this is always `ok`. |

## Automatic memory orchestration

For production behavior where the model/runtime handles memory calls automatically (instead of users manually invoking tools), use the event-driven orchestration pattern documented in [memory-orchestration.md](./memory-orchestration.md).

The reference implementation is [orchestrator.py](../src/waggle/orchestrator.py) plus [chat_runtime.py](../src/waggle/chat_runtime.py) and provides:

- async ingestion queue (`on_assistant_turn`)
- pre-model retrieval with token budget (`build_context`)
- scope isolation via `tenant/project/agent/session/model`
- concrete chat loop wrapper (`OrchestratedChatRuntime`)

This is the preferred product integration. Exposing MCP tools alone is not enough to make memory automatic. If a session appears to have empty memory, the likely cause is that the client did not load the automatic-memory instructions or the runtime is bypassing `build_context(...)` / `on_assistant_turn(...)`.

The MCP server also exposes this behavior as:

- prompt: `waggle_memory_policy`
- resource: `graph://memory-policy`

Recommended rule text for Codex and Antigravity:

```text
Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call prime_context.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call query_graph with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call observe_conversation automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call build_context before answers and on_assistant_turn after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.
```

A reusable copy also lives in [automatic-memory-rules.md](./automatic-memory-rules.md).

## Environment variables

See [Environment variables](./environment-variables.md) for the complete `WAGGLE_*` configuration reference, including defaults, value types, when each variable applies, and example values.

### Extraction

No extra extraction runtime is required. `observe_conversation` uses the built-in deterministic parser and stores only structured facts that map cleanly onto Waggle node types.

## Admin commands

```bash
# Create a tenant
waggle-mcp create-tenant --tenant-id workspace-a --name "Workspace A"

# Issue an API key (raw key returned once)
waggle-mcp create-api-key --tenant-id workspace-a --name "ci-agent" \
  --expires-in-days 30 --created-by "ops@example.com" \
  --scopes "graph:read,graph:write,admin:read"

# List keys for a tenant
waggle-mcp list-api-keys --tenant-id workspace-a

# Revoke a key
waggle-mcp revoke-api-key --api-key-id <id>

# Show retention status
waggle-mcp retention-status --tenant-id workspace-a

# Enable 90-day retention with a 24-hour prune interval
waggle-mcp set-retention --tenant-id workspace-a --enabled --days 90 --interval-hours 24

# Run pruning immediately
waggle-mcp prune-retention --tenant-id workspace-a

# List recent prune runs
waggle-mcp list-retention-runs --tenant-id workspace-a --limit 10

# Query audit events
waggle-mcp list-audit-events --tenant-id workspace-a --type api_key.created --limit 50

# Migrate SQLite data → Neo4j
WAGGLE_BACKEND=neo4j WAGGLE_NEO4J_URI=bolt://localhost:7687 \
WAGGLE_NEO4J_USERNAME=neo4j WAGGLE_NEO4J_PASSWORD=change-me \
  waggle-mcp migrate-sqlite --db-path ./memory.db --tenant-id workspace-a
```

`create-api-key` returns the raw key once along with non-secret metadata such as the key `prefix`, `expires_at`, `created_by`, and `scopes`. `list-api-keys` deliberately omits `key_hash` and returns only redacted administrative fields so keys can be rotated and audited without exposing the stored verifier.
`retention-status` and `set-retention` manage the per-tenant retention policy. `prune-retention` deletes aged graph records, transcript records, and old files in the configured export directory, then stores a prune summary you can inspect with `list-retention-runs`.
`list-audit-events` queries the append-only audit stream for a tenant, with filters for event type, actor, resource, and status.

HTTP admin endpoints are also available in the self-hosted app surface:

- `GET /api/admin/retention`
- `PUT /api/admin/retention`
- `POST /api/admin/retention/prune`
- `GET /api/admin/retention/runs`
- `GET /api/admin/audit-events`
If `X-API-Key` is provided, the request is scoped to that key's tenant. Otherwise the endpoints use `tenant_id` from the query string or body and fall back to the configured default tenant.

The HTTP graph surface also emits read-side audit events for snapshot fetches, transcript reads, query/debug views, diff reads, and export downloads.

Supported API key scopes:

- `graph:read`
- `graph:write`
- `admin:read`
- `admin:write`

When an API key is presented:

- MCP read calls require `graph:read`
- MCP write calls require `graph:write`
- `/api/graph/*` read routes require `graph:read`
- `/api/graph/*` write routes require `graph:write`
- `/api/admin/*` read routes require `admin:read`
- `/api/admin/*` write routes require `admin:write`

## Full tool surface

| Tool | What it does |
|------|--------------|
| `observe_conversation` | Ingest a conversation turn into graph memory |
| `query_graph` | Semantic + temporal search across graph, replay, or fusion |
| `store_node` | Manually save a fact, preference, decision, or note |
| `store_edge` | Link two nodes with a typed relationship |
| `get_related` | Traverse edges from a specific node |
| `get_node_history` | Inspect evidence, validity window, and related context |
| `list_context_scopes` | Enumerate stored `agent_id`, `project`, and `session_id` scopes |
| `timeline` | Build a chronological memory view |
| `list_conflicts` | List unresolved contradiction and update edges |
| `resolve_conflict` | Mark a contradiction or update edge as resolved |
| `update_node` | Update content or tags on an existing node |
| `delete_node` | Remove a node and all its edges |
| `decompose_and_store` | Break long content into atomic nodes automatically |
| `graph_diff` | See what changed in the last N hours |
| `prime_context` | Generate a compact brief for a new conversation |
| `get_topics` | Detect topic clusters via community detection |
| `get_stats` | Node/edge counts and most-connected nodes |
| `export_graph_html` | Interactive browser visualization |
| `export_graph_backup` | Portable JSON backup |
| `import_graph_backup` | Restore from a JSON backup |
| `export_context_bundle` | Export Markdown/JSON context packs for another AI |
| `export_markdown_vault` | Export one-file-per-node Markdown vaults |
| `import_markdown_vault` | Re-import edited Markdown vault files |

## Architecture snapshot

```text
waggle-mcp
├── Core domain
│   ├── graph CRUD (nodes, edges, evidence)
│   ├── dedup (semantic + exact)
│   ├── conflict detection (auto-contradiction)
│   ├── context assembly (query, prime, timeline)
│   ├── export/import (JSON, Markdown, GraphML)
│   └── local embeddings (SentenceTransformers + SHA-256 fallback)
├── Transport
│   ├── stdio MCP (local clients)
│   └── HTTP MCP (server-to-server)
└── Platform
    ├── auth (API keys + tenant isolation)
    ├── storage (SQLite + Neo4j)
    └── operations (rate limiting, logging, metrics)
```

Backend defaults:
- local/dev → SQLite
- production → Neo4j

Repository layout:

```text
waggle-mcp/
├── assets/
├── deploy/
├── docs/
├── scripts/
├── apps/
│   ├── mcp/graph-ui/
│   ├── mcp/claude-desktop-extension/
│   └── vscode-extension/
├── src/waggle/
├── tests/
├── Dockerfile
├── pyproject.toml
└── README.md
```
## Deduplication methodology

Deduplication in Waggle is intentionally conservative to prevent false merges of distinct but similar facts.

1. **Exact Content**: Case-insensitive, whitespace-normalized equality check.
2. **Same-Label High-Similarity**: If labels are identical or acronym matches, a lower similarity threshold (`0.90` default) is used.
3. **Semantic Similarity**: General node-to-node comparison using cosine similarity. The global default threshold is `0.97`, with type-aware and canonical-concept gates for safer paraphrase merges. The current 32-case fixture maintains zero false positives across the threshold sweep.

The system prefers creating "Derived From" or "Updates" edges over destructive merging when similarity is ambiguous.

## Context Assembly: Graph vs Flat

Naive RAG often stuffs irrelevant chunks into the prompt, wasting tokens and confusing reasoning. Waggle's graph retrieval builds a structured context subgraph focused on the query's dependency chain.

### Before: Naive Chunks (151 tokens)
> [Chunk 1] Keep SQLite for local development for now.
> [Chunk 2] Production is moving to PostgreSQL for parity.
> [Chunk 3] PostgreSQL is the production database.
> [Chunk 4] User: What is our database choice? Agent: You chose PostgreSQL.

### After: Waggle Graph (58 tokens)
> **Decisions**
> - [id:db_postgres] "PostgreSQL production" - PostgreSQL is the prod DB.
>   - *Updates*: "SQLite local only"
>   - *Contradicts*: "SQLite local only" (superseded-state)

---

## Memory ingestion paths

Waggle has two complementary ingestion paths. Use the right one for the situation:

| Path | When to use | Tool/command |
|------|-------------|-------------|
| **Live chat memory** | After every completed turn during an active session | `observe_conversation` MCP tool (or via orchestration) |
| **Rollover handoff** | When a chat window is full or a session is ending | `waggle-mcp ingest-transcript-handoff` CLI |

The client is responsible for detecting rollover and calling Waggle with the raw transcript JSON. Waggle does not monitor session length.

Both paths share the same extraction and edge-linking internals, so memory semantics are aligned.

---

## Rollover transcript handoff: `ingest-transcript-handoff`

> **Backend support:** SQLite only in v1. Neo4j support for this command is deferred and not yet implemented. A Neo4j backend will return an error if this command is invoked against it.

### Usage

```bash
# From a file
waggle-mcp ingest-transcript-handoff --input transcript.json --session-id my-session

# From stdin
cat transcript.json | waggle-mcp ingest-transcript-handoff --input -

# With scope overrides and custom export path
waggle-mcp ingest-transcript-handoff \
  --input transcript.json \
  --project my-project \
  --agent-id claude-3-7 \
  --session-id session-abc \
  --export-format markdown \
  --max-nodes 30 \
  --output-path ./handoff-bundle
```

This command now does three things for rollover:
- ingests the transcript into the live SQLite DB
- exports the requested Markdown/JSON handoff bundle
- emits a session-scoped `.abhi` checkpoint and refreshes the local checkpoint manifest

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--input PATH\|-` | `-` (stdin) | Path to the JSON transcript file, or `-` for stdin |
| `--project` | `""` | Scope override: project name |
| `--agent-id` | `""` | Scope override: agent identifier |
| `--session-id` | `""` | Scope override: session identifier |
| `--output-path` | auto | Optional export path prefix |
| `--export-format` | `both` | `markdown`, `json`, or `both` |
| `--max-nodes` | `25` | Max nodes in the exported context bundle |
| `--max-input-bytes` | `16777216` | Hard input cap (16 MiB). Oversized payloads fail with exit code 1 |

CLI scope flags override any scope fields in the JSON payload.

### Input JSON shape

```json
{
  "project": "optional-project",
  "agent_id": "optional-agent-id",
  "session_id": "optional-session-id",
  "messages": [
    {
      "role": "user|assistant|system|tool",
      "content": "non-empty text",
      "timestamp": "optional ISO-8601 string",
      "message_id": "optional stable client id"
    }
  ]
}
```

- `system` and `tool` roles are accepted and stored as transcript provenance. They are **not** used as extraction inputs and do **not** split or interrupt `user`/`assistant` blocks (v1 behavior).
- All four roles are stored in transcript provenance. Only `user` and `assistant` messages participate in logical turn extraction.
- An empty `messages` array (`[]`) is valid and returns exit code `0` with all-zero counts and `export_skipped: true`.

### Success output (stdout)

```json
{
  "scope": { "project": "", "agent_id": "", "session_id": "my-session" },
  "input_message_count": 4,
  "transcript_records_written": 4,
  "transcript_records_skipped": 0,
  "logical_turns_processed": 2,
  "unpaired_trailing_blocks": 0,
  "nodes_created": 3,
  "nodes_reused": 1,
  "conflicts": 0,
  "export_skipped": false,
  "markdown_path": "/path/to/bundle.md",
  "json_path": "/path/to/bundle.json",
  "export_node_count": 7,
  "export_edge_count": 4
}
```

When no export is produced: `"export_skipped": true, "export_skipped_reason": "no_messages"`.

### Failure output (stderr)

```json
{
  "code": "payload_too_large",
  "message": "Input exceeds --max-input-bytes (16777216 bytes).",
  "details": { "max_input_bytes": 16777216 }
}
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Input or validation failure (malformed JSON, missing field, unsupported role, oversized payload) |
| `2` | Backend / graph / export failure |
| `3` | Unexpected internal error |

### Block-windowing algorithm (v1)

The command uses a deterministic algorithm to convert the ordered message list into logical extraction turns:

1. Persist every message to `transcript_records`.
2. Build an **extractive stream** by keeping only `user` and `assistant` messages. `system` and `tool` messages are skipped.
3. **Collapse** consecutive same-role extractive messages into one block, joining text with `\n\n`.
4. Scan collapsed blocks left to right:
   - `user` followed by `assistant` → one **logical turn** (extracted from both).
   - Leading `assistant` block with no prior `user` → transcript-only, skipped for extraction.
   - Trailing `user` block with no following `assistant` → transcript-only, counted as `unpaired_trailing_blocks`.
5. After consuming one `user → assistant` pair, continue from the next remaining block.

**Example:** `user → user → assistant → user → assistant` becomes two logical turns:
- `(user+user) → assistant`
- `user → assistant`

### Tool-interleaving behavior (v1 simplification)

- `user → tool → tool → assistant` becomes **one logical turn**: `user → assistant`
- `user → assistant → tool → tool → assistant` becomes **one logical turn**: `user → (assistant + assistant)`

This is a known v1 simplification. Tool messages do not split or interrupt extractive blocks. A `tool_boundary_splits_blocks` option is a planned v2 refinement.

### Idempotency and dedup contract

- If `message_id` is present, it is used as the stable transcript identity.
- If `message_id` is absent, a deterministic positional fingerprint is computed from `(role, content, raw_position, timestamp-or-empty)`.
- Uniqueness is enforced per `(tenant_id, session_id, message_identity)`.
- Re-ingesting the **identical transcript** with the same session ID and same positions is a no-op at the transcript layer: no new records are written, no turns are reprocessed.

**Fingerprint limitation (v1):** Positional fingerprints are only idempotent for identical reruns. If the client prepends, removes, reorders, or partially resubmits messages, those changed positions are treated as new input. Use stable `message_id` values to avoid this.

### Retention policy

Core now supports per-tenant retention policies through the admin CLI:

- `waggle-mcp retention-status --tenant-id <tenant>`
- `waggle-mcp set-retention --tenant-id <tenant> --enabled --days 90 --interval-hours 24`
- `waggle-mcp prune-retention --tenant-id <tenant>`
- `waggle-mcp list-retention-runs --tenant-id <tenant>`

Retention pruning currently covers:

- graph nodes
- graph edges
- context windows
- context-window edges
- transcript provenance records
- old files in the configured export directory

Core does not yet run pruning automatically in the background. Operators should trigger it from their scheduler of choice until a built-in recurring job surface exists.

### Token budget

v1 does not have a `--max-context-tokens` flag. Export is bounded by `--max-nodes`. Token-budgeted export is a planned later enhancement.

### v2 backlog

The following refinements are intentional omissions, not oversights:

- `tool_boundary_splits_blocks` option: allow tool messages to terminate an assistant block.
- Token-budgeted export flag (in addition to `--max-nodes`).
- Stdin read timeouts (callers can wrap `ingest-transcript-handoff` with a wall-clock timeout).
- Streaming NDJSON input format.
- Neo4j backend support.
