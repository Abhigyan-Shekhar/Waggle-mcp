# Waggle: Local Memory for AI Agents

Local graph memory for coding agents using MCP.

Waggle gives VS Code agents persistent repo memory without a cloud account or API key.

This extension is the one-click onboarding path for Waggle in VS Code. It installs the Waggle CLI when needed, writes the MCP config for the current workspace after confirmation, runs `doctor`, and leaves the workspace ready to use the underlying MCP server.

## What it does

- Detects whether `waggle-mcp` is installed
- Offers a consented one-click setup flow
- Installs Waggle with `pipx` when needed
- Safely creates or updates `.vscode/mcp.json`
- Preserves non-Waggle MCP servers already configured in the workspace
- Runs `waggle-mcp doctor` and shows output in a dedicated output channel
- Launches Graph Studio locally
- Exports local memory to `.abhi`

## What it is for

Use Waggle when you want coding agents in VS Code to remember:

- architecture decisions
- repo-specific constraints
- user preferences and corrections
- contradictions and superseded context
- session handoff context across chats

## Requirements

- VS Code `1.96+`
- `pipx` available on `PATH` for one-click install
- Python `3.11+` for the Waggle CLI

## Quick start

1. Install the extension
2. Run `Waggle: Enable for this Workspace`
3. Confirm the install/configure flow
4. Reload VS Code if your MCP consumer requires it
5. Waggle runs `doctor` as part of setup

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

- `Waggle: Enable for this Workspace`
- `Waggle: Install Waggle`
- `Waggle: Run Doctor`
- `Waggle: Open Graph Studio`
- `Waggle: Show Status`
- `Waggle: Export Memory`
- `Waggle: Open Install Docs`

## Development

```bash
npm install
npm run compile
```

## Packaging

```bash
npm run package
```

## Privacy

- Local by default
- No cloud account required
- No API key required
- Memory stored in `WAGGLE_DB_PATH`, defaulting to `~/.waggle/waggle.db`

## Marketplace notes

This extension is meant to behave like MCP-style one-click setup for VS Code users. Users can also install the generated `.vsix` manually before Marketplace publication.
