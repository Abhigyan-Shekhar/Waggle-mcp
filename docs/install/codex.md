# Codex

Use this when you want Waggle connected to Codex as a local stdio MCP server.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

For direct Codex CLI or source-based MCP setup:

```bash
pipx install waggle-mcp
waggle-mcp setup --yes
```

`waggle-mcp setup --yes` writes a managed Waggle memory block into `AGENTS.md` in the current workspace so Codex can use Waggle from that repo.

### Managed `AGENTS.md` Block

When run inside a workspace, the setup command inserts a managed section inside the `AGENTS.md` file wrapped in specific HTML comment delimiters:

```markdown
<!-- waggle:auto-memory:start -->
## Waggle Automatic Memory
...
<!-- waggle:auto-memory:end -->
```

* **What it is for**: This block provides instructions telling AI agents (like Codex or Antigravity) to automatically call Waggle tools (`prime_context`, `query_graph`, `observe_conversation`) during active chat threads rather than requiring manual user actions.
* **Do not edit manually**: Do not manually modify any text inside the `<!-- waggle:auto-memory:start -->` and `<!-- waggle:auto-memory:end -->` delimiters. Any manual changes inside this block will be overwritten when `waggle-mcp setup --yes` or `waggle-mcp init` is run again.
* **What is safe to customize**: You can add your own custom rules, project descriptions, or team conventions anywhere *outside* this block (either above the start marker or below the end marker). These custom instructions are completely safe and will not be touched by Waggle.

For more details on how these rules govern agent behavior, see the [Automatic Memory Rules Guide](../automatic-memory-rules.md).

## Codex app plugin

This repository also ships a Codex app plugin manifest at `.codex-plugin/plugin.json`
with its MCP companion config in `.mcp.json`.

For the Codex app plugin, Waggle bundles its own plugin-local MCP server runtime.
Users do not need to install `waggle-mcp` from PyPI separately. The plugin
launcher resolves a signed executable under `plugins/waggle/runtime/<target>/`
and starts it with `serve --transport stdio`.

Bundled runtime updates are delivered only through plugin upgrades. If a bundled
binary is stale or missing, reinstall or upgrade the Waggle Codex plugin.

Tagged Waggle releases now publish two Codex plugin assets:

- `waggle-codex-marketplace-<tag>.zip`: a complete local marketplace root that
  can be added with `codex plugin marketplace add`
- `waggle-codex-plugin-<tag>.zip`: the bare `plugins/waggle` plugin folder

For the easiest install path, download and extract the marketplace bundle, then
run:

```bash
codex plugin marketplace add /path/to/waggle-codex-marketplace-<tag>
```

After that, refresh the plugin directory in Codex and install `Waggle` from the
added marketplace.

## Manual config

For direct Codex CLI usage outside the bundled app plugin, add Waggle to
`~/.codex/config.toml`:

```toml
[mcp_servers.waggle]
command = "waggle-mcp"
args = ["serve", "--transport", "stdio"]

[mcp_servers.waggle.env]
WAGGLE_BACKEND = "sqlite"
WAGGLE_DB_PATH = "~/.waggle/waggle.db"
WAGGLE_DEFAULT_TENANT_ID = "local-default"
WAGGLE_MODEL = "all-MiniLM-L6-v2"
```

A pre-filled example is available at
[`examples/codex_config.example.toml`](../../examples/codex_config.example.toml).

## Verify

```bash
waggle-mcp doctor
```

Restart Codex and confirm Waggle tools such as `prime_context`, `query_graph`,
and `observe_conversation` are available.

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Waggle stores memory locally by default in SQLite. Set `WAGGLE_DB_PATH`
explicitly if you want Codex and other MCP clients to share the same local
memory graph.
