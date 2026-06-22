# Install Waggle

Waggle is local graph memory for coding agents.

Use it to give Claude, Cursor, Codex, Copilot, and other MCP agents persistent repo memory.

No cloud account. No API key. Local by default.

## Install methods

- [VS Code](./vscode.md)
- [Smithery](./smithery.md)
- [Claude Code](./claude-code.md)
- [Claude Desktop](./claude-desktop.md)
- [Codex](./codex.md)
- [Cursor](./cursor.md)
- [Antigravity](./antigravity.md)
- [Generic MCP clients](./generic-mcp.md)
- [Troubleshooting](./troubleshooting.md)
- [Windows setup & troubleshooting](./troubleshooting.md#windows-specific-troubleshooting)

## One-line install

For Cursor, Antigravity, Claude, generic MCP clients, and direct Codex CLI
configuration:

```bash
pipx install waggle-mcp
waggle-mcp doctor
```

The Codex app plugin bundles a plugin-local server runtime and does not require
a separate PyPI installation.

## Universal stdio config

```json
{
  "mcpServers": {
    "waggle": {
      "command": "waggle-mcp",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

## Verify

```bash
waggle-mcp doctor
waggle-mcp serve --transport stdio
```

## Final checklist

- `waggle-mcp` is on your `PATH`
- `waggle-mcp doctor` reports a writable database path
- Your client config points to `waggle-mcp serve --transport stdio`
- `WAGGLE_DB_PATH` and `WAGGLE_DEFAULT_TENANT_ID` are set if you want non-default storage or tenancy
- The client shows Waggle tools after reload or restart
