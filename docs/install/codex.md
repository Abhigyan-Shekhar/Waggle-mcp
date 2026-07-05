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

`waggle-mcp setup --yes` writes a managed Waggle memory block into `AGENTS.md` in
the current workspace so Codex can use Waggle from that repo.

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
- `waggle-codex-release-<tag>.json`: release metadata for audit and support

For the easiest install path, download and extract the marketplace bundle, then
run:

```bash
codex plugin marketplace add /path/to/waggle-codex-marketplace-<tag>
```

After that, refresh the plugin directory in Codex and install `Waggle` from the
added marketplace.

The v1 marketplace bundle intentionally contains all supported platform
runtimes. Do not choose a platform-specific bundle unless a future Codex
marketplace schema explicitly supports platform-specific artifact resolution.

To verify a downloaded release manually:

```bash
shasum -a 256 -c waggle-codex-marketplace-<tag>.zip.sha256
gh attestation verify waggle-codex-marketplace-<tag>.zip \
  --repo Abhigyan-Shekhar/Waggle-mcp
```

The repo-hosted v1 release may be unsigned. Manual release-validation workflow
runs are unsigned by default unless `enable_signing` is set, and releases are
also unsigned when Apple Developer ID and Windows Authenticode credentials are
not configured in CI. In that case macOS Gatekeeper and Windows SmartScreen can
show warnings. Verify the checksum and GitHub attestation before installing. If
signing credentials are later enabled, Windows builds use OV Authenticode
signing unless EV cloud signing is explicitly added.

To upgrade, install the newer marketplace bundle from the GitHub release and
refresh the plugin directory in Codex. Waggle memory is stored outside the
plugin bundle at `WAGGLE_DB_PATH`, so supported upgrades must preserve local
memory data.

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
