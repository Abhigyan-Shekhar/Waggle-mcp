# Codex Plugin Bundled Runtime

The Codex app plugin ships a self-contained Waggle MCP server executable so
plugin users do not need Python, `pipx`, or `waggle-mcp` on `PATH`.

## Runtime Layout

Release artifacts are copied into target directories:

```text
plugins/waggle/runtime/
  darwin-arm64/waggle-server
  darwin-arm64/_internal/...
  darwin-x86_64/waggle-server
  linux-x86_64/waggle-server
  linux-aarch64/waggle-server
  win32-x86_64/waggle-server.exe
```

The plugin `.mcp.json` starts `plugins/waggle/bin/waggle-server-launcher.js`.
That launcher resolves the current platform, applies the default Waggle
environment, and executes the matching bundled binary. It does not fall back to
`waggle-mcp` on `PATH`.

## Build

Build each artifact on a native runner. PyInstaller is the default packager
because it produces Python-free executables and avoids cross-compilation.
Waggle uses PyInstaller `--onedir` mode for the Codex plugin runtime because
local `--onefile` probes exceeded the 3-second cold-start budget due to archive
extraction.

```bash
python -m pip install . pyinstaller
python scripts/build_codex_plugin_runtime.py --build-current --probe
```

The bundled entrypoint is `waggle.entrypoints.server_only`. It intentionally
supports only:

```bash
waggle-server serve --transport stdio
waggle-server --server-info
```

## Release Packaging

The release workflow packages two downloadable Codex assets after the runtime
layout is assembled and validated:

- `waggle-codex-marketplace-<tag>.zip`: a complete local marketplace root with
  `.agents/plugins/marketplace.json` plus `plugins/waggle/`
- `waggle-codex-plugin-<tag>.zip`: the bare `plugins/waggle/` plugin folder

The marketplace bundle is the primary install artifact because Codex can add it
directly with `codex plugin marketplace add /path/to/extracted-bundle`.

## Release Validation

Before packaging a plugin release, assemble all platform artifacts and run:

```bash
python scripts/build_codex_plugin_runtime.py --require-artifacts
```

Validation enforces:

- all five target binaries are present
- each target runtime directory is no larger than 80 MB
- `--server-info` starts within 3 seconds and emits compatibility metadata when
  `--probe` is run on a native runner
- macOS binaries pass `codesign --verify` when `--verify-signatures` is run on
  macOS
- Windows binaries pass Authenticode verification when `--verify-signatures` is
  run on Windows

Linux has no OS-level signing gate in this release flow.

## Signing

macOS release binaries must be signed with a Developer ID certificate and
submitted for notarization before plugin packaging. Windows release binaries
must be Authenticode signed. Store signing material in CI secrets and rotate it
using the provider's normal renewal process.

Required CI secrets:

| Secret name | Purpose |
|---|---|
| `APPLE_DEVELOPER_ID` | Full `Developer ID Application: ...` signing identity |
| `APPLE_ID` | Apple ID used with `notarytool` |
| `APPLE_TEAM_ID` | Apple Team ID |
| `APPLE_NOTARY_PASSWORD` | App-specific password for notarization |
| `WINDOWS_CERT_BASE64` | Base64-encoded Authenticode PFX |
| `WINDOWS_CERT_PASSWORD` | PFX password |

## Compatibility

The server reports:

- `name`
- `version`
- `minimum_supported_protocol_version`
- `runtime_scope`

Plugin-side launch failures should tell users to upgrade or reinstall the
plugin. Bundled runtimes are not auto-updated independently.

## Failure Recovery

If a platform runner is unavailable, do not publish the Codex plugin release.
Build the missing target on a native runner, sign it, copy it into the runtime
layout, then rerun release validation.

If startup probing fails, run the binary directly with `--server-info` and with
`serve --transport stdio` using `WAGGLE_MODEL=deterministic` to separate startup
issues from model download latency.
