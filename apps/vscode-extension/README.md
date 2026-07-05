# Waggle: Local Memory for AI Agents

Local graph memory for coding agents using MCP.

Waggle gives VS Code agents persistent repo memory without a cloud account or API key.

## What it does

- Downloads a standalone `waggle-mcp` binary from GitHub Releases (no pip/Python required by default)
- Auto-starts a local HTTP server when a workspace opens (`waggle.autoStart`)
- Writes `.vscode/mcp.json` for agent MCP (stdio transport) after you confirm
- **Waggle: Query Memory** — search the graph via the local HTTP API
- **Waggle: Observe Conversation** — persist a user/assistant turn via MCP
- Graph Studio in the activity bar sidebar and via **Open Graph Studio**
- `pipx` install remains available (`waggle.installMethod`: `pipx`)

## Requirements

- VS Code `1.96+`
- Network access on first run (to download the release binary for your OS)
- Optional: `pipx` + Python `3.11+` if you use `installMethod: pipx`

## Quick start

1. Install the extension
2. Open a workspace folder
3. Run **Waggle: Enable for this Workspace** (or accept the startup prompt)
4. Reload VS Code if your MCP consumer requires it

## Settings

| Setting                 | Default   | Description                                    |
| ----------------------- | --------- | ---------------------------------------------- |
| `waggle.mcpConfigScope` | `servers` | Root key used when creating `.vscode/mcp.json` |

### `waggle.mcpConfigScope`

Controls which root key the extension uses when creating a new `.vscode/mcp.json`.

Supported values:

* `servers` (default) — Use the VS Code MCP configuration format.
* `mcpServers` — Use the legacy MCP configuration format expected by some tools.

If `.vscode/mcp.json` already contains a `servers` or `mcpServers` object, the extension follows

the existing file and ignores this setting.

This setting is mainly used when creating a new `.vscode/mcp.json` file.

### `.vscode/mcp.json` merge behavior

When you run **Waggle: Enable for this Workspace**, the extension merges the Waggle configuration into the existing `.vscode/mcp.json` file instead of overwriting unrelated MCP servers.

Process:

1. Read the existing `.vscode/mcp.json`.
2. Determine whether `servers` or `mcpServers` should be used.
3. Preserve existing server entries.
4. Add or update only the `waggle` entry.
5. Write the updated configuration back to disk.

Example:

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

## Commands

- **Waggle: Enable for this Workspace**
- **Waggle: Install Waggle** (download binary or pipx)
- **Waggle: Run Doctor**
- **Waggle: Query Memory**
- **Waggle: Observe Conversation**
- **Waggle: Open Graph Studio**
- **Waggle: Show Status**
- **Waggle: Export Memory**
- **Waggle: Open Install Docs**

## Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `waggle.installMethod` | `binary` | `binary` or `pipx` |
| `waggle.autoStart` | `true` | Start HTTP server on workspace open |
| `waggle.binaryReleaseRepo` | `Abhigyan-Shekhar/Waggle-mcp` | GitHub repo for release assets |
| `waggle.binaryVersion` | *(extension version)* | Pin a specific release tag |
| `waggle.dbPath` | `~/.waggle/waggle.db` | SQLite path |
| `waggle.tenantId` | workspace folder name | Tenant / project scope |

## Development

```bash
cd apps/vscode-extension
npm install
npm run compile
npm test
```

Press F5 to launch the Extension Development Host.

### Bundled binary layout

Release CI publishes `waggle-mcp-{platform}` assets and `bundle-metadata.json`. The extension maps your OS/arch via `scripts/asset-map.json` and caches under global storage.

## Packaging

```bash
npm run package
```

Binaries are **not** embedded in the VSIX; they are downloaded on first use from GitHub Releases.

## Privacy

- Local by default
- No cloud account required
- Memory stored in `WAGGLE_DB_PATH`
