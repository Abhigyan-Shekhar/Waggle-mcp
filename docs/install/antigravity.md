# Antigravity

Use this when you want Waggle connected to Antigravity or another Gemini-adjacent local MCP client.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

```bash
pipx install waggle-mcp
pipx ensurepath
```

Restart the terminal after the first `pipx ensurepath`, then run:

```bash
waggle-mcp setup --yes --clients antigravity
```

## Manual config

Treat Antigravity as a generic stdio MCP client:

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

Restart Antigravity and confirm that Waggle tools are visible.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Waggle remains local-first here too. The client launches a local stdio process and reads or writes only the paths you configure.
