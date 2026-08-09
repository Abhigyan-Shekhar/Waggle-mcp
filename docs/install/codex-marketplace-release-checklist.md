# Codex Marketplace Release Checklist

Use this checklist before announcing a Waggle Codex plugin marketplace bundle.
The current distribution model is a downloadable local marketplace root that
users add with `codex plugin marketplace add`.

## Version Policy

- Codex plugin version: `0.1.3`
- GitHub release tag for the Codex skills bundle: `v0.1.21`
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
python3 scripts/package_codex_plugin.py --bundle-version v0.1.21 --output-dir dist/codex-plugin
python3 -m pytest tests/test_package_codex_plugin.py tests/test_packaging_metadata.py -q
```

For native platform smoke tests, use a clean user environment—not the
development checkout—and follow the public path exactly:

1. Download the published marketplace ZIP and checksum.
2. Verify and extract it into a new directory.
3. Run `codex plugin marketplace add /path/to/waggle-codex-marketplace-v0.1.21`.
4. Run `codex plugin add waggle@waggle`.
5. Start a new Codex task in an unrelated existing repository.
6. Confirm Codex exposes:

- `prime_context`
- `query_graph`
- `observe_conversation`

Record cold and retry startup latency from the runtime probe. A first launch
over 10 seconds is a release-quality warning that must appear in the release
notes even when retry succeeds.

Confirm these behavioral memory round trips:

### Constraint recall

1. In session 1, say: `We're using SQLite because this project must remain fully local.`
2. Start session 2 in the same repository.
3. Ask: `Should we migrate the memory backend to Postgres?`
4. Confirm Codex retrieves the local-only constraint before answering.

### Failed-approach recall

1. In session 1, attempt implementation A and establish the verified reason it failed.
2. Start session 2 in the same repository.
3. Ask how the feature should be implemented.
4. Confirm Codex retrieves the failed approach and avoids repeating it.

Also confirm a direct scoped round trip:

1. Ask Codex to remember a project-scoped decision.
2. Start a new Codex thread in the same workspace.
3. Confirm Waggle can retrieve that decision with the same project scope.

## Release Artifacts

Expected files:

- `waggle-codex-marketplace-v0.1.21.zip`
- `waggle-codex-marketplace-v0.1.21.zip.sha256`
- `waggle-codex-plugin-v0.1.21.zip`
- `waggle-codex-plugin-v0.1.21.zip.sha256`
- `waggle-codex-release-v0.1.21.json`

The marketplace zip is the primary user-facing artifact. The bare plugin zip is
for debugging, audits, and future installer compatibility.

## Unsigned Runtime Policy

The current Codex plugin bundle is intentionally unsigned. Apple Developer ID
notarization and Windows Authenticode signing require paid accounts or
certificates, so they are not release blockers for this self-hosted marketplace
bundle.

This release is also self-hosted through GitHub Releases. Do not introduce a
paid hosted backend for the default Codex plugin path; the bundled stdio MCP
server runs locally with SQLite storage.

Because the runtime is unsigned:

- Keep checksum files attached to the release.
- Keep GitHub build provenance attestations enabled.
- Keep first-run macOS Gatekeeper and Windows SmartScreen steps in the install
  and troubleshooting docs.
- Do not describe unsigned OS warnings as bugs.

## Announcement Checklist

- Present Waggle as a self-hosted Codex MCP plugin, not as an OpenAI-curated
  directory listing or signed native installer.
- State that the default install path has no required hosting or certificate
  cost.
- Link users to the `v0.1.21` GitHub release.
- Tell users to download the marketplace zip, extract it, and run:

```bash
codex plugin marketplace add /path/to/waggle-codex-marketplace-v0.1.21
codex plugin add waggle@waggle
```

- State clearly that `v0.1.16` and `v0.1.19` were partial releases and should
  not be used as Codex marketplace install sources.
- State clearly that the plugin version shown in Codex is `0.1.3`, while the
  GitHub release tag is `v0.1.21`.
