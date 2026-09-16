# Generic MCP Clients

Use this when your MCP client accepts a JSON stdio server definition.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

```bash
pipx install waggle-mcp
pipx ensurepath
```

Restart the terminal after the first `pipx ensurepath` before starting Waggle.

## Manual config

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

Reload the client and confirm it can see Waggle tools such as `prime_context`, `query_graph`, and `observe_conversation`.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Waggle stores memory locally by default in SQLite. Set `WAGGLE_DB_PATH` explicitly if you want the storage location to be obvious and auditable.
