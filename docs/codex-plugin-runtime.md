# Codex Plugin Bundled Runtime

The Codex app plugin ships a self-contained Waggle MCP server executable so
plugin users do not need Python, `pipx`, or `waggle-mcp` on `PATH`.

## Runtime Layout

Release artifacts are copied into target directories:

```text
plugins/waggle/runtime/
  darwin-arm64/waggle-server
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
Waggle uses PyInstaller `--onefile` mode for the Codex plugin runtime. The
startup probe budget is 10 seconds to accommodate onefile archive extraction on
first launch.

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

Codex marketplace entries are treated as one fixed plugin source for this v1
release. Do not generate a custom platform-to-artifact index unless Codex
documents support for platform-specific artifact resolution. The release
manifest emitted next to the archives records this decision and is for release
auditability, not installer routing.

## Release Validation

Before packaging a plugin release, assemble all platform artifacts and run:

```bash
python scripts/build_codex_plugin_runtime.py --require-artifacts
```

Validation enforces:

- all five target binaries are present
- each target runtime directory is no larger than 80 MB
- Unix runtimes keep executable permissions after packaging
- `--server-info` starts within 10 seconds and emits compatibility metadata when
  `--probe` is run on a native runner
- macOS binaries pass `codesign --verify` when `--verify-signatures` is run on
  macOS
- Windows binaries pass Authenticode verification when `--verify-signatures` is
  run on Windows

Linux has no OS-level signing gate in this release flow.

## Signing

The current Codex plugin release is intentionally unsigned. Apple Developer ID
notarization and Windows Authenticode signing require paid accounts or
certificates, so signing is not a release blocker for the self-hosted Codex
marketplace bundle.

Users should expect macOS Gatekeeper and Windows SmartScreen warnings on first
launch. Keep checksum files, GitHub build provenance attestations, and the
first-run approval steps in the install docs instead of treating signing as a
required launch task.

The workflow still contains optional signing hooks so a future maintainer can
enable paid signing without redesigning the release pipeline. Until those
credentials are intentionally configured, unsigned release artifacts are the
expected output.

The release workflow also generates GitHub build provenance attestations for
the Codex plugin artifacts and verifies them with `gh attestation verify` before
publishing. `.sha256` files remain a manual verification fallback.

## Versioning

The Codex plugin manifest version is intentionally separate from the GitHub
release tag. The current plugin version is `0.1.1`; the current public GitHub
release tag for the complete Codex marketplace bundle is `v0.1.17`.

Earlier GitHub releases were trial releases while the Waggle repository was
private. Do not align the plugin version to the GitHub tag unless the plugin
surface itself changes.

## Compatibility

The server reports:

- `name`
- `version`
- `minimum_supported_protocol_version`
- `runtime_scope`

Plugin-side launch failures should tell users to upgrade or reinstall the
plugin and include OS, architecture, plugin version, database path, binary path,
and exit details where available. Bundled runtimes are not auto-updated
independently.

## Local Database Safety

Waggle stores plugin memory in the user's local SQLite database. SQLite runs in
WAL mode with a busy timeout, and schema initialization uses a cross-process
lock. Supported upgrades must preserve user data. If a future release requires a
schema migration, implement it as a verified temp-copy migration followed by an
atomic replacement; do not mutate the only copy in place.

If the runtime detects a newer unsupported schema, a corrupted database, or an
interrupted migration, it must fail clearly without further mutation and point
the user to backup or recovery instructions. Multiple Codex windows may contend
for the same database; until a shared local runtime architecture is implemented,
release tests must verify that concurrent starts either serialize safely through
SQLite/lock behavior or fail clearly without corruption.

## Failure Recovery

If a platform runner is unavailable, do not publish the Codex plugin release.
Build the missing target on a native runner, sign it, copy it into the runtime
layout, then rerun release validation.

If startup probing fails, run the binary directly with `--server-info` and with
`serve --transport stdio` using `WAGGLE_MODEL=deterministic` to separate startup
issues from model download latency.

If a release is broken after publication, yank the GitHub release assets or
publish a corrected release that points users back to the last-known-good
marketplace bundle. Do not repoint a mutable default-branch index for plugin
routing; v1 release assets are immutable GitHub Release artifacts.
