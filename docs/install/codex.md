# Codex

Use this when you want Waggle connected to Codex as a local stdio MCP server.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

```bash
pipx install waggle-mcp
waggle-mcp setup --yes
```

`waggle-mcp setup --yes` writes a managed Waggle memory block into `AGENTS.md` in
the current workspace so Codex can use Waggle from that repo.

## Manual config

Add Waggle to `~/.codex/config.toml`:

```toml
[mcp_servers.waggle]
command = "waggle-mcp"
args = ["serve", "--transport", "stdio"]

[mcp_servers.waggle.env]
WAGGLE_BACKEND = "sqlite"
WAGGLE_DB_PATH = "~/.waggle/waggle.db"
WAGGLE_DEFAULT_TENANT_ID = "local-default"
WAGGLE_MODEL = "all-MiniLM-L6-v2"
```

A pre-filled example is available at
[`examples/codex_config.example.toml`](../../examples/codex_config.example.toml).

## Verify

```bash
waggle-mcp doctor
```

Restart Codex and confirm Waggle tools such as `prime_context`, `query_graph`,
and `observe_conversation` are available.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Waggle stores memory locally by default in SQLite. Set `WAGGLE_DB_PATH`
explicitly if you want Codex and other MCP clients to share the same local
memory graph.
