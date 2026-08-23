# Project Memory CLI

Use these commands when you want Waggle to behave like local project memory from
the terminal, without first opening an MCP client.

This workflow is intentionally small:

1. seed memory from high-signal repository files,
2. confirm memory exists,
3. search it,
4. inspect a node,
5. review recent memory events.

## Bootstrap A Repository

```bash
waggle-mcp bootstrap
```

By default, bootstrap scans the current directory and stores concise memory
nodes for high-signal project files:

- `README.md`
- `AGENTS.md`
- `CLAUDE.md`
- `CONTRIBUTING.md`
- `CHANGELOG.md`
- `SECURITY.md`
- `SUPPORT.md`
- common project config files such as `pyproject.toml`, `package.json`,
  `Cargo.toml`, `go.mod`, and `requirements.txt`
- markdown files under `docs/`
- a short git summary, when the directory is a git repository

Bootstrap does not scan arbitrary source files by default.

Preview the operation first:

```bash
waggle-mcp bootstrap --dry-run
```

Useful options:

```bash
waggle-mcp bootstrap \
  --project my-repo \
  --model deterministic \
  --max-files 12 \
  --max-file-bytes 32768
```

Disable git summary ingestion:

```bash
waggle-mcp bootstrap --no-include-git
```

## Check Memory Stats

```bash
waggle-mcp stats
```

This prints:

- node and edge counts
- known project, agent, and session scopes
- node type breakdown
- recent memories
- highly connected memories

For scripts:

```bash
waggle-mcp stats --json
```

## Search Project Memory

```bash
waggle-mcp search "database decision"
```

Search defaults to hybrid retrieval. It can return graph nodes and transcript
hits, depending on what exists in the local database.

Scope a search to one project:

```bash
waggle-mcp search "why did we choose SQLite" --project my-repo
```

Use deterministic embeddings for fast local checks:

```bash
waggle-mcp search "agent instructions" --model deterministic
```

For scripts:

```bash
waggle-mcp search "agent instructions" --json
```

## Inspect A Memory Node

Search and timeline output include node IDs. Inspect one node:

```bash
waggle-mcp inspect-node <node-id>
```

The text view shows:

- node ID, type, label, and scope
- bounded content preview
- tags
- metadata
- evidence records
- related nodes
- edges

Show full content in the terminal:

```bash
waggle-mcp inspect-node <node-id> --full
```

For scripts:

```bash
waggle-mcp inspect-node <node-id> --json
```

## Review The Timeline

```bash
waggle-mcp timeline
```

Focus the timeline around a query:

```bash
waggle-mcp timeline --query "database decision"
```

Focus around a node:

```bash
waggle-mcp timeline --node-id <node-id>
```

Filter event kinds:

```bash
waggle-mcp timeline --events created
waggle-mcp timeline --events updated
waggle-mcp timeline --events evidence
waggle-mcp timeline --events edges
```

For scripts:

```bash
waggle-mcp timeline --json
```

## Temporary Or Alternate Databases

All project-memory commands accept `--db`:

```bash
waggle-mcp bootstrap --db /tmp/waggle-demo.db --model deterministic
waggle-mcp stats --db /tmp/waggle-demo.db --model deterministic
waggle-mcp search "persistent memory" --db /tmp/waggle-demo.db --model deterministic
```

This is useful for demos, tests, and trying Waggle without touching your normal
`~/.waggle` database.

## Telemetry

These commands do not send telemetry unless anonymous telemetry is explicitly
enabled.

When enabled, `bootstrap` may record `memory_stored` and `search` may record
`memory_retrieved`. `stats`, `timeline`, and `inspect-node` are inspection
commands and are not counted as active memory operations.

See [telemetry.md](telemetry.md) for the exact opt-in telemetry contract.
