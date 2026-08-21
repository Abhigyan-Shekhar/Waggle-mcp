# Claude Desktop

Use this when you want Waggle available inside Claude Desktop through the desktop MCP config file.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

```bash
pipx install waggle-mcp
waggle-mcp setup --yes --clients claude-desktop
```

## Manual config

Add this to your Claude Desktop config:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/claude/claude_desktop_config.json`

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

Restart Claude Desktop and confirm Waggle tools appear.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

The server runs locally over stdio. Memory stays on disk under your configured database path unless you export it.

## Claude web/mobile

Claude web and mobile cannot launch this local stdio server directly. If you need web/mobile access, use the self-hosted remote connector guide:

[Claude self-hosted connector](../claude-self-hosted-connector.md)
