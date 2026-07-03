# VS Code

Use the Waggle VS Code extension for one-click workspace setup with a bundled release binary (no pip install required by default).

## One-line install

Install the Marketplace extension, open a workspace, then run:

**Waggle: Enable for this Workspace**

## Default behavior

- Downloads `waggle-mcp` from [GitHub Releases](https://github.com/Abhigyan-Shekhar/Waggle-mcp/releases) for your platform
- Starts a local HTTP server (`edit-graph`) when the workspace opens
- Writes `.vscode/mcp.json` for agent MCP over stdio (after you confirm)

## Manual MCP config

If you prefer not to use the extension, add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "waggle": {
      "type": "stdio",
      "command": "waggle-mcp",
      "args": ["serve", "--transport", "stdio"],
      "env": {
        "WAGGLE_DEFAULT_TENANT_ID": "${workspaceFolderBasename}",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db",
        "WAGGLE_STARTUP_MODE": "fast"
      }
    }
  }
}
```

With the extension's binary install, `command` is the cached executable path under VS Code global storage.

## pipx fallback

Set `waggle.installMethod` to `pipx` in VS Code settings if you already use `pipx install waggle-mcp`.

## `waggle.mcpConfigScope`

The `waggle.mcpConfigScope` setting controls which root key the extension uses when creating a new `.vscode/mcp.json`.

| Value               | When to use                                                       |
| ------------------- | ----------------------------------------------------------------- |
| `servers` (default) | Recommended for new VS Code MCP configurations                    |
| `mcpServers`        | Use when your tooling expects the legacy MCP configuration format |

If `.vscode/mcp.json` already contains a `servers` or `mcpServers` object, the extension follows
the existing file structure and ignores this setting.

This setting is mainly used when creating a new `.vscode/mcp.json` file.

## How `.vscode/mcp.json` is merged

The extension does not overwrite your existing MCP configuration.

When you run **Waggle: Enable for this Workspace**, it:

1. Reads the existing `.vscode/mcp.json`.
2. Preserves existing MCP servers.
3. Adds or updates only the `waggle` entry.
4. Writes the updated configuration back to disk.

### Example

Before:

```json
{
  "servers": {
    "github": {
      "type": "http"
    }
  }
}
```

After:

```json
{
  "servers": {
    "github": {
      "type": "http"
    },
    "waggle": {
      "type": "stdio"
    }
  }
}
```


## Verify

```bash
waggle-mcp doctor
```

Or use **Waggle: Run Doctor** in the command palette.

Reload VS Code, switch the agent UI into MCP-capable mode, and confirm the Waggle server is enabled.

## Troubleshooting

- **Binary download fails** — check `waggle.binaryReleaseRepo` and network; try `waggle.binaryVersion` matching an existing tag (e.g. `0.1.15`).
- **Antivirus blocks the binary** — allow the file under VS Code global storage or use `pipx`.
- See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Workspace MCP config is visible in the repo. That keeps the command path, tenant ID, and database path auditable during code review.
