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

## First-run OS warnings (unsigned binary)

The bundled Waggle runtime binary is currently unsigned. This means macOS and Windows will show a security warning on first launch. This is expected — it does not mean the binary is malicious.

### macOS (Gatekeeper)

You may see: *"waggle-runtime cannot be opened because it is from an unidentified developer."*

To approve:

1. Open **System Settings → Privacy & Security**
2. Scroll to the bottom — you'll see a message about the blocked binary
3. Click **Allow Anyway**
4. Re-launch Codex — macOS will ask once more; click **Open**

Or via terminal:

```bash
xattr -dr com.apple.quarantine /path/to/waggle-runtime
```

### Windows (SmartScreen)

You may see: *"Windows protected your PC"*

To approve:

1. Click **More info**
2. Click **Run anyway**

Then retry the Codex plugin install.

> These warnings appear only on first run. Once approved, the binary launches without prompting.

This repository also ships a Codex app plugin manifest at `.codex-plugin/plugin.json`
with its MCP companion config in `.mcp.json`.

For the Codex app plugin, Waggle bundles its own plugin-local MCP server runtime.
Users do not need to install `waggle-mcp` from PyPI separately. The plugin
launcher resolves a bundled executable under `plugins/waggle/runtime/<target>/`
and starts it with `serve --transport stdio`.

Bundled runtime updates are delivered only through plugin upgrades. If a bundled
binary is stale or missing, reinstall or upgrade the Waggle Codex plugin.

Tagged Waggle releases publish two Codex plugin assets. The current Codex
marketplace artifacts are published on the
[`v0.1.17` release](https://github.com/Abhigyan-Shekhar/Waggle-mcp/releases/tag/v0.1.17):

- `waggle-codex-marketplace-v0.1.17.zip`: a complete local marketplace root that
  can be added with `codex plugin marketplace add`
- `waggle-codex-plugin-<tag>.zip`: the bare `plugins/waggle` plugin folder
- `waggle-codex-release-<tag>.json`: release metadata for audit and support

> `v0.1.16` was a partial release and should not be used as a Codex
> marketplace install source. Use `v0.1.17` instead.

For the easiest install path, download and extract the marketplace bundle
from the [`v0.1.17` release](https://github.com/Abhigyan-Shekhar/Waggle-mcp/releases/tag/v0.1.17),
then run:

```bash
codex plugin marketplace add /path/to/waggle-codex-marketplace-v0.1.17
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
