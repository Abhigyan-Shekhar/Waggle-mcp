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

## Client support matrix

Every path runs Waggle as a local **stdio** server, so begin with `pipx install waggle-mcp` in all cases. The table shows the quickest setup for each client and where its MCP config lives.

| Client | Quickest install path | MCP config location |
|---|---|---|
| [VS Code](./vscode.md) | Marketplace extension (`Waggle: Enable for this Workspace`) | `.vscode/mcp.json` (`servers` root) |
| [Smithery](./smithery.md) | `smithery.yaml` bundle (stdio) | root `smithery.yaml` |
| [Claude Code](./claude-code.md) | CLI &mdash; `claude mcp add ... waggle` | CLI-managed (`.mcp.json` / user config) |
| [Claude Desktop](./claude-desktop.md) | `waggle-mcp setup --yes --clients claude-desktop` | `claude_desktop_config.json` (OS path in guide) |
| [Codex](./codex.md) | `waggle-mcp setup --yes` (writes `AGENTS.md` block) | `~/.codex/config.toml` |
| [Cursor](./cursor.md) | `waggle-mcp setup --yes --clients cursor` | `~/.cursor/mcp.json` |
| [Antigravity](./antigravity.md) | `waggle-mcp setup --yes --clients antigravity` | generic stdio JSON |
| [Generic MCP clients](./generic-mcp.md) | manual stdio JSON | client's own MCP config |

Prefer to wire it up by hand? Each guide above also documents a manual config block.

## One-line install

```bash
pipx install waggle-mcp
waggle-mcp doctor
```

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
