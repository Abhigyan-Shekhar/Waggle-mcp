# PyPI and MCP Registry publishing

This runbook separates credential-free contributor work from repository-owner actions. It describes a proposed release of `waggle-mcp`; it does not assert that a new version has been published.

Primary references were rechecked on 2026-08-10:

- [PyPI: add a GitHub trusted publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
- [PyPI: publish with a trusted publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [MCP Registry quickstart and publisher commands](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx)
- [MCP Registry authentication](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/authentication.mdx)
- [MCP Registry GitHub Actions guidance](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/github-actions.mdx)
- [MCP Registry PyPI ownership marker](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx)
- [MCP Registry version immutability](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/versioning.mdx)
- [MCP Registry official API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md)
- [MCP Registry releases](https://github.com/modelcontextprotocol/registry/releases)
- [GitHub MCP directory onboarding clarification, discussion #1257](https://github.com/github/github-mcp-server/discussions/1257)
- [Example GitHub MCP directory onboarding request, discussion #2844](https://github.com/github/github-mcp-server/discussions/2844)

The immutable Registry name is `io.github.Abhigyan-Shekhar/Waggle-mcp`. GitHub authentication proves control of the `Abhigyan-Shekhar` namespace and repository. A general contributor can prepare and validate tracked metadata, but only the repository owner can configure the PyPI identity, GitHub environments, reviewers, and namespace authentication.

## Version preparation

**Maintainer:** In a normal release PR, update `[project].version` in `pyproject.toml`, then synchronize the generated Registry versions:

```bash
python scripts/sync_release_metadata.py --write
git diff -- pyproject.toml server.json README.md
python scripts/sync_release_metadata.py --check
```

**Maintainer:** Review and commit the generated diff before creating a tag. `--write` is a release-PR operation only. The publishing workflow runs `--check`; it never rewrites, commits, or pushes metadata from an ephemeral runner.

**Repository owner:** Confirm the chosen version before merging the release PR. The proposed next repository version is `0.1.22`, pending owner confirmation.

## Contributor validation

**Contributor:** Run this block from the repository root. It uses temporary directories for the schema, distributions, virtual environment, database, and stdio work area.

```bash
set -euo pipefail

release_tmp="$(mktemp -d)"
trap 'rm -rf "$release_tmp"' EXIT

python scripts/sync_release_metadata.py --check
schema_url="$(python -c 'import json; print(json.load(open("server.json"))["$schema"])')"
curl --fail --location --retry 3 --silent --show-error \
  --output "$release_tmp/server.schema.json" "$schema_url"
python scripts/check_registry_readiness.py schema \
  --schema "$release_tmp/server.schema.json"
python scripts/check_registry_readiness.py project
python -m ruff check .
python -m ruff format --check .
WAGGLE_MODEL=deterministic python -m pytest -q
python -m build --outdir "$release_tmp/dist"
python -m twine check "$release_tmp"/dist/*
python scripts/check_registry_readiness.py artifacts \
  --dist-dir "$release_tmp/dist"
wheel_path="$(find "$release_tmp/dist" -maxdepth 1 -type f -name '*.whl' -print -quit)"
python scripts/check_registry_readiness.py wheel \
  --wheel "$wheel_path" --readme README.md
python -m venv "$release_tmp/install-venv"
"$release_tmp/install-venv/bin/python" -m pip install "$wheel_path"
HF_HUB_OFFLINE=1 \
TRANSFORMERS_OFFLINE=1 \
WAGGLE_BACKEND=sqlite \
WAGGLE_DB_PATH="$release_tmp/doctor-waggle.db" \
WAGGLE_DEFAULT_TENANT_ID=release-smoke \
WAGGLE_MODEL=deterministic \
WAGGLE_STARTUP_MODE=fast \
WAGGLE_TRANSPORT=stdio \
  "$release_tmp/install-venv/bin/waggle-mcp" doctor --json
"$release_tmp/install-venv/bin/python" \
  scripts/check_registry_readiness.py stdio \
  --command "$release_tmp/install-venv/bin/waggle-mcp" \
  --work-dir "$release_tmp/stdio-smoke"
```

## First-time owner configuration

**Repository owner:** On PyPI, configure a GitHub Trusted Publisher for project `waggle-mcp` with:

- owner: `Abhigyan-Shekhar`
- repository: `Waggle-mcp`
- workflow: `publish-python-and-mcp.yml`
- environment: `pypi`

**Repository owner:** Do not create a long-lived PyPI token. The workflow requests a short-lived OIDC identity with job-level `id-token: write`.

**Repository owner:** In GitHub repository settings, create environments named `pypi` and `mcp-registry`. Configure required reviewers and, if useful, wait timers. These approval rules live outside the workflow YAML. Make the PyPI publisher's environment claim exactly `pypi`; a different spelling causes an OIDC claim mismatch.

**Repository owner:** Protect tag creation and workflow changes according to the repository's release policy. GitHub environment approval is the final human gate after the tag-only workflow condition.

## Automated release flow

**Maintainer:** A manual `workflow_dispatch` runs only `validate-build`. It cannot reach any remote-state job.

**Repository owner:** Only a pushed tag matching `v*` can enter the publish chain, and the tag must exactly equal `v` plus `pyproject.toml`'s version.

1. `validate-build` checks committed metadata, lints and tests, builds one wheel and one sdist, inspects them, installs the wheel, and smoke-tests stdio.
2. `pypi-publish` waits at the `pypi` environment, publishes those exact artifacts with trusted publishing, and verifies the exact public PyPI version.
3. `mcp-registry-publish` starts only after public PyPI verification, waits at `mcp-registry`, rechecks committed metadata before authentication, publishes `server.json`, and verifies the exact Registry namespace, version, and PyPI package.
4. `release-assets` reuses a GitHub release already created by the owner for the tag. If none exists, it creates a draft release, then attaches the original wheel and sdist. Automation never publishes that draft.

**Repository owner:** Review and publish any draft GitHub release manually after all public checks pass.

## Owner-only recovery

**Repository owner:** After an uncertain PyPI result, query PyPI before doing anything else:

```bash
VERSION=0.1.22
curl --fail --silent --show-error \
  "https://pypi.org/pypi/waggle-mcp/$VERSION/json"
```

**Repository owner:** If that exact version exists, do not rerun any PyPI upload. PyPI versions and their files cannot be replaced. `twine upload` is deliberately not part of this recovery path. If the exact version is absent, diagnose the trusted-publisher identity or rerun the gated workflow only after confirming that no file was accepted.

**Repository owner:** If PyPI is correct but Registry publication needs manual recovery, first install a currently released `mcp-publisher` from its official release asset and verify the published checksum. Then run, from a clean checkout of the exact release tag:

```bash
VERSION=0.1.22
python scripts/sync_release_metadata.py --check
mcp-publisher login github
mcp-publisher publish
python scripts/check_release_publication.py registry \
  --name io.github.Abhigyan-Shekhar/Waggle-mcp \
  --version "$VERSION" \
  --package waggle-mcp
```

**Repository owner:** If `mcp-publisher publish` returned an uncertain result, run the read-only verification first. Do not repeat the publish command when the version exists; Registry versions are immutable.

**Repository owner:** Recover a release-asset failure without republishing PyPI or Registry metadata:

```bash
TAG=v0.1.22
gh release view "$TAG" || gh release create "$TAG" \
  --draft --verify-tag --generate-notes --title "Waggle $TAG"
gh release upload "$TAG" dist/* --clobber
```

**Repository owner:** Leave a newly created release as a draft until its notes and attached files have been reviewed.

## Failure decisions

| Failure | Role and response | Command that must not be repeated |
|---|---|---|
| Tag does not equal `v` plus the tracked version | **Maintainer:** delete or supersede the incorrect local tag and prepare the correct tag only after reviewing metadata. | **Repository owner:** do not rerun the workflow with the mismatched tag. |
| Metadata drift | **Maintainer:** return to a normal PR, run `--write`, review the diff, commit it, then run `--check`. | **Repository owner:** do not publish from runner-only metadata. |
| Missing README marker | **Maintainer:** restore the single exact `mcp-name` marker and rebuild. | **Repository owner:** do not upload the contaminated artifact set. |
| Archive contamination | **Maintainer:** remove the forbidden packaged path or packaging rule and rebuild from clean source. | **Repository owner:** do not upload any artifact from the failed build. |
| PyPI or GitHub OIDC claim mismatch | **Repository owner:** compare owner, repository, workflow filename, environment `pypi`, and tag ref claims. | **Repository owner:** do not fall back to an unreviewed long-lived token. |
| Environment approval rejected or timed out | **Repository owner:** resolve the reviewer or wait-timer policy and start a new run only if no publication happened. | **Repository owner:** do not bypass the environment gate. |
| PyPI propagation timeout | **Repository owner:** query the exact PyPI JSON endpoint until its state is known. | **Repository owner:** do not repeat the PyPI upload if the version or either artifact exists. |
| PyPI succeeded, Registry failed | **Repository owner:** preserve the PyPI result, fix only Registry authentication/metadata, and use Registry recovery. | **Repository owner:** do not repeat the PyPI publish action. |
| Registry publish succeeded, verification timed out | **Repository owner:** query the exact Registry version until its state is known. | **Repository owner:** do not repeat `mcp-publisher publish` if the immutable version exists. |
| Release asset upload failed | **Repository owner:** repair only the release or asset upload. | **Repository owner:** do not repeat either package publication. |

## Known pre-existing public state

**Maintainer:** Treat this as a known mismatch, not as evidence that this branch repaired public state: the official Registry advertises PyPI version `0.1.8`, while PyPI currently has only `0.0.1`. The proposed next repository version is `0.1.22`, pending owner confirmation.

## Owner-only release checklist

1. **Repository owner:** Confirm the chosen version.
2. **Repository owner:** Confirm tracked metadata with `python scripts/sync_release_metadata.py --check`.
3. **Repository owner:** Confirm the PyPI trusted-publisher identity and the `pypi` environment gate.
4. **Repository owner:** Push the exact `v<version>` tag.
5. **Repository owner:** Approve the `pypi` and `mcp-registry` environments when their jobs reach the gates.
6. **Repository owner:** Verify the exact PyPI project/version and the live README `mcp-name` marker.
7. **Repository owner:** If using manual recovery, authenticate and publish with `mcp-publisher` only after checking public state.
8. **Repository owner:** Verify the exact Registry namespace, version, and `waggle-mcp` package.
9. **Repository owner:** Install from PyPI in at least one real MCP client.
10. **Repository owner:** Review and publish the draft GitHub release.
11. **Repository owner:** Request initial GitHub MCP directory onboarding through the current GitHub Q&A/manual-curation process.

The GitHub MCP directory at `github.com/mcp` does not automatically onboard a newly published official-Registry server. GitHub's current clarification in discussion #1257 says initial onboarding is manually curated; after onboarding, later official-Registry versions should sync. The template below is unsent.

## Unsent GitHub MCP directory inclusion request

> **Title:** Onboarding request: `io.github.Abhigyan-Shekhar/Waggle-mcp` for `github.com/mcp`
>
> Please consider Waggle for the GitHub MCP directory: it gives AI coding clients persistent, graph-backed memory for decisions, reasons, updates, and contradictions across sessions.
>
> - Repository: `Abhigyan-Shekhar/Waggle-mcp`
> - Official Registry name: `io.github.Abhigyan-Shekhar/Waggle-mcp`
> - PyPI package: `waggle-mcp`
> - Runtime: local-first stdio server; no hosted Waggle backend or cloud account is required for the default SQLite setup
> - Supported clients documented by the project: Claude Code, Claude Desktop, Codex, Cursor, Gemini CLI, Antigravity, and VS Code
> - Install: `pipx install waggle-mcp`
> - Client setup: `waggle-mcp setup --yes`
> - Security and privacy: memory and local embeddings remain on the user's machine by default; no API key is required for the local setup; users control the configured database path and should review the repository's security model before shared or networked deployment
>
> **Repository owner:** Before sending, replace this line with the verified PyPI project/version URL and evidence that the live package README contains the exact `mcp-name` marker.
>
> **Repository owner:** Before sending, replace this line with the verified official Registry API URL/output for the exact namespace and version.
>
> The official Registry publication is a prerequisite, but this request recognizes that initial `github.com/mcp` onboarding is manually curated under the current guidance in discussion #1257. Thank you for reviewing it.
