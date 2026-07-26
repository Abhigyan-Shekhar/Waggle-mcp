# Codex Marketplace Release Checklist

Use this checklist before announcing a Waggle Codex plugin marketplace bundle.
The current distribution model is a downloadable local marketplace root that
users add with `codex plugin marketplace add`.

## Version Policy

- Codex plugin version: `0.1.1`
- GitHub release tag used for the current public Codex bundle: `v0.1.17`
- These versions are intentionally separate. The GitHub tag follows the main
  repository release history, including earlier private-repository trial
  releases. The Codex plugin manifest version tracks the plugin surface itself.
- Do not bump the Codex plugin version just to match the GitHub release tag.
  Bump it only when the plugin manifest, launcher, bundled runtime contract, or
  user-visible Codex plugin behavior changes.

## Release Inputs

- `.agents/plugins/marketplace.json` points `waggle` to `./plugins/waggle`.
- Root and plugin `.codex-plugin/plugin.json` files have the same plugin
  version.
- `plugins/waggle/.mcp.json` starts the bundled launcher with stdio transport.
- `plugins/waggle/bin/waggle-server-launcher.js` resolves only plugin-local
  runtime binaries and does not depend on `waggle-mcp` being on `PATH`.
- All five runtime targets are assembled before packaging:
  - `darwin-arm64/waggle-server`
  - `darwin-x86_64/waggle-server`
  - `linux-x86_64/waggle-server`
  - `linux-aarch64/waggle-server`
  - `win32-x86_64/waggle-server.exe`

## Validation Commands

Run these from the repository root:

```bash
python3 scripts/build_codex_plugin_runtime.py --require-artifacts
python3 scripts/package_codex_plugin.py --bundle-version v0.1.17 --output-dir dist/codex-plugin
python3 -m pytest tests/test_package_codex_plugin.py tests/test_packaging_metadata.py -q
```

For native platform smoke tests, install from the extracted marketplace bundle
and confirm Codex exposes:

- `prime_context`
- `query_graph`
- `observe_conversation`

Also confirm a basic memory round trip:

1. Ask Codex to remember a project-scoped decision.
2. Start a new Codex thread in the same workspace.
3. Confirm Waggle can retrieve that decision with the same project scope.

## Release Artifacts

Expected files:

- `waggle-codex-marketplace-v0.1.17.zip`
- `waggle-codex-marketplace-v0.1.17.zip.sha256`
- `waggle-codex-plugin-v0.1.17.zip`
- `waggle-codex-plugin-v0.1.17.zip.sha256`
- `waggle-codex-release-v0.1.17.json`

The marketplace zip is the primary user-facing artifact. The bare plugin zip is
for debugging, audits, and future installer compatibility.

## Unsigned Runtime Policy

The current Codex plugin bundle is intentionally unsigned. Apple Developer ID
notarization and Windows Authenticode signing require paid accounts or
certificates, so they are not release blockers for this self-hosted marketplace
bundle.

Because the runtime is unsigned:

- Keep checksum files attached to the release.
- Keep GitHub build provenance attestations enabled.
- Keep first-run macOS Gatekeeper and Windows SmartScreen steps in the install
  and troubleshooting docs.
- Do not describe unsigned OS warnings as bugs.

## Announcement Checklist

- Link users to the `v0.1.17` GitHub release.
- Tell users to download the marketplace zip, extract it, and run:

```bash
codex plugin marketplace add /path/to/waggle-codex-marketplace-v0.1.17
```

- State clearly that `v0.1.16` was a partial trial release and should not be
  used as a Codex marketplace install source.
- State clearly that the plugin version shown in Codex is `0.1.1`, while the
  GitHub release tag is `v0.1.17`.
