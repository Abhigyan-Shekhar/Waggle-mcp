# Cursor

Use this when you want Waggle connected to Cursor through its native MCP support.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

```bash
pipx install waggle-mcp
pipx ensurepath
```

Restart the terminal after the first `pipx ensurepath`, then run:

```bash
waggle-mcp setup --yes --clients cursor
```

## Manual config

Create `~/.cursor/mcp.json` and add:

```json
{
  "mcpServers": {
    "waggle": {
      "command": "waggle-mcp",
      "args": ["serve", "--transport", "stdio"],
      "env": {
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db"
      }
    }
  }
}
```

## Verify

```bash
waggle-mcp doctor
```

Restart Cursor and confirm the Waggle server is enabled in MCP settings.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Cursor launches Waggle locally on your machine. Review the configured `env` block so the database path and tenant namespace are explicit.
