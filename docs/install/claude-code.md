# Claude Code

Use this when you want Waggle connected to Claude Code as a local stdio MCP server.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

```bash
pipx install waggle-mcp
pipx ensurepath
```

Restart the terminal after the first `pipx ensurepath`, then run:

```bash
claude mcp add --transport stdio waggle -- waggle-mcp serve --transport stdio
```

## Manual config

CLI add command with environment variables:

```bash
claude mcp add --transport stdio \
  --env WAGGLE_DEFAULT_TENANT_ID=local-default \
  --env WAGGLE_DB_PATH=~/.waggle/waggle.db \
  waggle -- waggle-mcp serve --transport stdio
```

JSON add example:

```bash
claude mcp add-json waggle '{
  "type": "stdio",
  "command": "waggle-mcp",
  "args": ["serve", "--transport", "stdio"],
  "env": {
    "WAGGLE_DEFAULT_TENANT_ID": "local-default",
    "WAGGLE_DB_PATH": "~/.waggle/waggle.db"
  }
}'
```

## Verify

```bash
claude mcp get waggle
waggle-mcp doctor
```

Inside Claude Code, use `/mcp` to confirm the server is connected.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Claude Code starts Waggle as a local subprocess. Review project-scoped `.mcp.json` files before approving them.
