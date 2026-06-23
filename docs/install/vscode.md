# VS Code

Use this when you want Waggle enabled from a VS Code extension with a one-click setup flow for the current workspace.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

Install the Marketplace extension, then run:

```bash
Waggle: Enable for this Workspace
```

## Manual config

VS Code MCP examples commonly use `.vscode/mcp.json` with a `servers` root:

```json
{
  "servers": {
    "waggle": {
      "type": "stdio",
      "command": "waggle-mcp",
      "args": ["serve", "--transport", "stdio"],
      "env": {
        "WAGGLE_DEFAULT_TENANT_ID": "${workspaceFolderBasename}",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db"
      }
    }
  }
}
```

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

The extension will install `waggle-mcp` if needed, write `.vscode/mcp.json` after confirmation, and run `waggle-mcp doctor`.

Reload VS Code, switch the agent UI into MCP-capable mode, and confirm the Waggle server is enabled.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Workspace MCP config is visible in the repo. That keeps the command path, tenant ID, and database path auditable during code review.
