# Release Automation and MCP Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a credential-safe release workflow that validates and builds once, publishes the exact artifacts to PyPI, publishes already-correct tracked metadata to the official MCP Registry, verifies both public records, and attaches the distributions to an owner-controlled GitHub release.

**Architecture:** Keep GitHub Actions responsible for ordering, permissions, environments, and artifact handoff. Put polling, response validation, and archive inspection in small Python CLIs with unit-testable boundaries. A manual run ends after validation; only an exact `v<project-version>` tag push can enter the three ordered remote-state jobs.

**Tech Stack:** Python 3.11, Python standard library (`argparse`, `json`, `tarfile`, `tomllib`, `urllib`, `zipfile`), `pytest`, `PyYAML`, Ruff, PyPA build/twine, GitHub Actions, PyPI Trusted Publishing, MCP Registry `mcp-publisher`, GitHub CLI.

## Global Constraints

- Work only on local branch `codex/release-automation-publishing`, based on `codex/server-json-registry-readiness` commit `64658aa`. Do not push, create a tag, create a GitHub release, or open a PR before Tracks 1 and 2 merge.
- Do not run `twine upload`, `mcp-publisher publish`, an OIDC login, or any equivalent authenticated publishing command during implementation or verification.
- `workflow_dispatch` is always validation-only. There is no input that can enable publication.
- Every publish job must require a `push` event whose ref is a `v*` tag. The validated tag must equal `v` plus `[project].version` from the tagged commit.
- `pypi-publish` must declare `environment: pypi`; `mcp-registry-publish` must declare `environment: mcp-registry`. Required reviewers and wait timers are configured by the owner in GitHub settings, outside the workflow.
- Release metadata is repository state, not runner state. The workflow runs `python scripts/sync_release_metadata.py --check` and never runs `--write`, commits, or pushes.
- Build exactly once in `validate-build`. Every later job downloads the original workflow artifacts and never rebuilds them.
- Preserve the namespace `io.github.Abhigyan-Shekhar/Waggle-mcp` exactly. Do not change `server.json` in this track.
- MCP Registry versions are immutable. Never present a rerun of `mcp-publisher publish` for the same version as safe recovery.
- `release-assets` reuses the tag's draft or published release when one exists. If none exists, it creates a draft. It never publishes a draft.
- Pin every third-party action to a full commit SHA. Pin downloaded executables to an exact release asset and published SHA-256 digest.
- Treat `0.1.22` only as the currently proposed version. The implementation reads the version from the repository and does not hard-code it in workflow logic.
- Back every final implementation claim with literal command output. Identify owner-only/OIDC checks as untested.

## Live Pin Record Captured for This Plan

The following public GitHub API values were fetched on 2026-08-10. At the start of Task 3, fetch them again. If `modelcontextprotocol/registry/releases/latest` has changed, use the new real tag, matching Linux amd64 asset URL, and that asset's published digest together; never update only the version string.

```text
pypa/gh-action-pypi-publish v1.14.2 commit:
dc37677b2e1c63e2034f94d8a5b11f265b73ba33

modelcontextprotocol/registry latest:
v1.8.1
2026-08-06T23:35:18Z
mcp-publisher_linux_amd64.tar.gz
sha256:a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc
https://github.com/modelcontextprotocol/registry/releases/download/v1.8.1/mcp-publisher_linux_amd64.tar.gz

rhysd/actionlint latest local verifier:
v1.7.12
actionlint_1.7.12_darwin_arm64.tar.gz
sha256:aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f
https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_darwin_arm64.tar.gz
```

---

### Task 1: Standalone tag and public-publication verifier

**Files:**

- Create: `scripts/check_release_publication.py`
- Create: `tests/test_check_release_publication.py`

**Interfaces:**

- `load_project_version(root: Path) -> str`
- `validate_tag(root: Path, tag: str) -> str`; returns the project version or raises `ReleaseCheckError`.
- `poll_pypi(project: str, version: str, *, fetch_json: JsonFetcher = fetch_json, sleep: Sleeper = time.sleep, attempts: int = 20, interval: float = 15.0) -> None`
- `poll_registry(name: str, version: str, package: str, *, fetch_json: JsonFetcher = fetch_json, sleep: Sleeper = time.sleep, attempts: int = 12, interval: float = 10.0) -> None`
- `fetch_json(url: str, timeout: float) -> tuple[int, object]`; default timeout passed by pollers is exactly 10 seconds. HTTP 404 is returned as a status for retry; transport, JSON, and non-404 HTTP failures become deterministic `ReleaseCheckError` messages.
- CLI success for `tag --tag <tag>` prints only the version to stdout. All failures print `Release publication check failed: <reason>` to stderr and exit 1.

- [ ] Write the unit tests first. Use injected `fetch_json` and `sleep` callables; no test may access the network or actually sleep. Name and cover the tests `test_validate_tag_returns_project_version_for_exact_tag`, `test_validate_tag_rejects_mismatched_and_unprefixed_tags`, `test_pypi_retries_404_then_accepts_exact_project_and_version`, `test_pypi_stops_after_twenty_attempts`, `test_pypi_rejects_wrong_name_wrong_version_and_malformed_payload`, `test_registry_url_encodes_namespace_and_accepts_exact_package`, `test_registry_retries_404_then_stops_after_twelve_attempts`, `test_registry_rejects_wrong_name_version_identifier_and_package_version`, `test_cli_tag_prints_only_version_on_success`, and `test_cli_failure_is_nonzero_and_deterministic`.

- [ ] In Registry fixtures, mirror the public version endpoint shape and assert the encoded URL exactly:

  ```python
  {
      "server": {
          "name": "io.github.Abhigyan-Shekhar/Waggle-mcp",
          "version": "0.1.22",
          "packages": [
              {"registryType": "pypi", "identifier": "waggle-mcp", "version": "0.1.22"}
          ],
      }
  }
  # Expected path segment contains io.github.Abhigyan-Shekhar%2FWaggle-mcp.
  ```

- [ ] Run the focused test before implementation and confirm collection fails because `scripts.check_release_publication` does not exist:

  ```bash
  .venv/bin/python -m pytest tests/test_check_release_publication.py -q
  ```

- [ ] Implement the smallest standard-library-only module that satisfies the tests. Use `urllib.parse.quote(value, safe="")` for every path component. PyPI must query `https://pypi.org/pypi/<project>/<version>/json`; Registry must query `https://registry.modelcontextprotocol.io/v0/servers/<name>/versions/<version>`.
- [ ] Validate PyPI `info.name` and `info.version`. Validate Registry `server.name`, `server.version`, and exactly one matching package object with `registryType == "pypi"`, the requested identifier, and the requested version.
- [ ] Retry only not-yet-visible responses (404). Reject 200 responses with malformed or mismatched metadata immediately. Sleep only between attempts, never after the final attempt.
- [ ] Add argparse subcommands exactly matching the design: `tag --tag`, `pypi --project --version`, and `registry --name --version --package`.
- [ ] Run focused tests and quality checks:

  ```bash
  .venv/bin/python -m pytest tests/test_check_release_publication.py -q
  .venv/bin/python -m ruff check scripts/check_release_publication.py tests/test_check_release_publication.py
  .venv/bin/python -m ruff format --check scripts/check_release_publication.py tests/test_check_release_publication.py
  ```

  Expected: all commands exit 0.

- [ ] Commit only Task 1 files:

  ```bash
  git add scripts/check_release_publication.py tests/test_check_release_publication.py
  git commit -m "feat: add release publication verifier"
  ```

---

### Task 2: Inspect the complete wheel and source distribution

**Files:**

- Modify: `MANIFEST.in`
- Modify: `scripts/check_registry_readiness.py`
- Modify: `tests/test_registry_readiness.py`

**Interfaces:**

- Add `check_artifacts(dist_dir: Path) -> list[str]`.
- Add CLI `artifacts --dist-dir <path>` using the existing `_print_result` exit contract.
- Preserve `check_wheel` behavior and existing CLI subcommands unchanged.

- [ ] Extend the test fixture helpers to create a wheel and `.tar.gz` sdist with these minimum valid members:

  ```text
  wheel:
    waggle/__init__.py
    rlm/__init__.py
    waggle_mcp-0.1.22.dist-info/METADATA
    waggle_mcp-0.1.22.dist-info/WHEEL
    waggle_mcp-0.1.22.dist-info/RECORD

  sdist (under exactly one waggle_mcp-0.1.22/ root):
    README.md
    pyproject.toml
    PKG-INFO
    src/waggle/__init__.py
    src/rlm/__init__.py
  ```

- [ ] Write failing tests for a valid pair; zero/multiple wheels; zero/multiple sdists; ZIP and TAR absolute paths; `..` traversal; caches; `.env`/`.venv`; `.db`/`.sqlite`/`.sqlite3`; credential/key files (`credentials.json`, `*.pem`, `*.key`, `id_rsa`); and forbidden development roots (`.git`, `benchmark_results`, `build`, `dist`, `dist-release`, `graph-ui`, `node_modules`, `tests`). Also test missing `METADATA`, missing `pyproject.toml`, missing `src/waggle`, and multiple sdist top-level roots.
- [ ] Run the new focused selection and confirm failures because the function/subcommand is absent:

  ```bash
  .venv/bin/python -m pytest tests/test_registry_readiness.py -q -k artifacts
  ```

- [ ] Implement normalized archive-path validation before checking members. Reject backslashes, absolute POSIX paths, drive-letter paths, empty/dot components, and any `..` component. Inspect member names only; never extract archives.
- [ ] Apply forbidden checks to normalized path components and basenames so a nested secret is rejected. Keep explicit exceptions limited to packaging metadata needed above.
- [ ] Require exactly one `*.whl` and one `*.tar.gz` in the supplied directory. Report all archive issues in stable sorted order.
- [ ] Add `prune tests` to `MANIFEST.in`. A real pre-plan build proved the current sdist contains the full test suite, so the archive checker would correctly reject it until the manifest excludes that development-only root. Do not prune `src/waggle` or `src/rlm`.
- [ ] Run focused and regression tests:

  ```bash
  .venv/bin/python -m pytest tests/test_registry_readiness.py -q
  .venv/bin/python -m ruff check scripts/check_registry_readiness.py tests/test_registry_readiness.py
  .venv/bin/python -m ruff format --check scripts/check_registry_readiness.py tests/test_registry_readiness.py
  ```

  Expected: all existing wheel/manifest/stdio tests and new artifact tests pass.

- [ ] Commit only Task 2 files:

  ```bash
  git add MANIFEST.in scripts/check_registry_readiness.py tests/test_registry_readiness.py
  git commit -m "feat: inspect release distributions"
  ```

---

### Task 3: Structural policy tests and four-job publishing workflow

**Files:**

- Modify: `pyproject.toml`
- Create: `tests/test_publish_workflow.py`
- Create: `.github/workflows/publish-python-and-mcp.yml`

**Interfaces:**

- Add `pyyaml>=6.0` to the `dev` extra because repository validation code/tests import `yaml` directly.
- Parse the workflow test with `yaml.load(..., Loader=yaml.BaseLoader)` so GitHub's `on` key is not interpreted as a YAML 1.1 boolean.
- Workflow jobs are exactly `validate-build`, `pypi-publish`, `mcp-registry-publish`, and `release-assets`.

- [ ] Re-fetch the three live pin sources and paste their literal output into implementation notes:

  ```bash
  curl -fsSL https://api.github.com/repos/pypa/gh-action-pypi-publish/releases/latest | jq -r '.tag_name, .published_at'
  curl -fsSL https://api.github.com/repos/pypa/gh-action-pypi-publish/commits/v1.14.2 | jq -r '.sha'
  curl -fsSL https://api.github.com/repos/modelcontextprotocol/registry/releases/latest | jq -r '.tag_name, .published_at, (.assets[] | select(.name == "mcp-publisher_linux_amd64.tar.gz") | [.name, .digest, .browser_download_url] | @tsv)'
  ```

  Expected from the plan-time fetch is PyPI action `v1.14.2` at `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` and MCP publisher `v1.8.1` with digest `a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc`. If the fresh MCP tag, release page, asset name, URL, or digest disagree, stop and investigate rather than guessing.

- [ ] Write `tests/test_publish_workflow.py` before the workflow. Assert all of these invariants structurally:

  ```python
  assert set(workflow["on"]) == {"workflow_dispatch", "push"}
  assert workflow["on"]["push"]["tags"] == ["v*"]
  assert workflow["concurrency"]["cancel-in-progress"] == "false"
  assert set(jobs) == {"validate-build", "pypi-publish", "mcp-registry-publish", "release-assets"}
  assert jobs["pypi-publish"]["needs"] == "validate-build"
  assert jobs["mcp-registry-publish"]["needs"] == "pypi-publish"
  assert jobs["release-assets"]["needs"] == "mcp-registry-publish"
  assert jobs["pypi-publish"]["environment"] == "pypi"
  assert jobs["mcp-registry-publish"]["environment"] == "mcp-registry"
  assert jobs["pypi-publish"]["permissions"] == {"id-token": "write"}
  assert jobs["mcp-registry-publish"]["permissions"] == {"contents": "read", "id-token": "write"}
  assert jobs["release-assets"]["permissions"] == {"contents": "write"}
  ```

- [ ] Additionally assert top-level permissions are empty; `validate-build` has only `contents: read`; neither `validate-build` nor `release-assets` has `id-token`; and each publish job's `if` string requires all three clauses: `github.event_name == 'push'`, `github.ref_type == 'tag'`, and `startsWith(github.ref_name, 'v')`. Assert the complete workflow text contains `sync_release_metadata.py --check`, does not contain `sync_release_metadata.py --write`, `git commit`, or `git push`, and contains both the draft release path and the immutable Registry warning.
- [ ] Run the structural test and confirm it fails because the workflow does not exist:

  ```bash
  .venv/bin/python -m pytest tests/test_publish_workflow.py -q
  ```

- [ ] Implement workflow-level policy:

  ```yaml
  name: Publish Python package and MCP Registry entry

  on:
    workflow_dispatch:
    push:
      tags:
        - "v*"

  permissions: {}

  concurrency:
    group: release-${{ github.ref }}
    cancel-in-progress: false
  ```

- [ ] Implement `validate-build` with `permissions: {contents: read}`, `timeout-minutes: 30`, and output `version: ${{ steps.release.outputs.version }}`. Use these pinned actions already established in the repository:

  ```yaml
  actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
  actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
  ```

  Checkout must set `persist-credentials: false`. Set up Python 3.11. Install the project dev extra plus `build>=1.2.2` and `twine>=6.1.0`.

- [ ] In `validate-build`, set the version output with one shell step. On tag push, execute `python scripts/check_release_publication.py tag --tag "$GITHUB_REF_NAME"`; on manual dispatch, read `[project].version` with `tomllib`. Append only `version=<value>` to `$GITHUB_OUTPUT`.
- [ ] Run these validation/build stages in order: `sync_release_metadata.py --check`; download the exact schema URL declared in `server.json`; `check_registry_readiness.py schema`; `check_registry_readiness.py project`; Ruff check; Ruff format check; full pytest; `python -m build --outdir dist`; `python -m twine check dist/*`; `check_registry_readiness.py artifacts --dist-dir dist`; then set `wheel_path="$(find dist -maxdepth 1 -type f -name '*.whl' -print -quit)"` and run `check_registry_readiness.py wheel --wheel "$wheel_path" --readme README.md`.
- [ ] Create a fresh `${RUNNER_TEMP}/release-venv`, install only `dist/*.whl`, and reuse Track 2's exact offline `waggle-mcp doctor` and stdio smoke environment. No smoke database or venv may be created in the checkout.
- [ ] Upload `dist/` as `python-distributions` and `scripts/check_release_publication.py` as `release-verifier`, both with `retention-days: 7` and `if-no-files-found: error`.
- [ ] Implement `pypi-publish` with `needs: validate-build`, the exact tag-push condition, `environment: pypi`, `permissions: {id-token: write}`, and `timeout-minutes: 10`. Download `python-distributions` with:

  ```yaml
  actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
  ```

  Publish with:

  ```yaml
  pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
  ```

  Set `packages-dir: dist/`. Do not check out the repository, build, install project dependencies, or reference a password/API token. After the action succeeds, download `release-verifier` and run `python3 check_release_publication.py pypi --project waggle-mcp --version "${{ needs.validate-build.outputs.version }}"`.
- [ ] Implement `mcp-registry-publish` with `needs: pypi-publish`, the exact tag-push condition, `environment: mcp-registry`, `permissions: {contents: read, id-token: write}`, and `timeout-minutes: 10`. Checkout with persisted credentials disabled and set up Python 3.11.
- [ ] Download the freshly verified `mcp-publisher_linux_amd64.tar.gz` into `$RUNNER_TEMP`, verify its SHA-256 before extraction, extract only `mcp-publisher`, and run `mcp-publisher --help` to prove the binary starts. With the plan-time values, the checksum line is:

  ```bash
  echo "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc  ${RUNNER_TEMP}/mcp-publisher_linux_amd64.tar.gz" | sha256sum --check --strict
  ```

- [ ] Immediately before authentication, run `python scripts/sync_release_metadata.py --check`. Then run `mcp-publisher login github-oidc`, followed by `mcp-publisher publish`. Put this comment directly above publish:

  ```yaml
  # MCP Registry versions are immutable. Do not rerun this publish command for a version that may already exist.
  ```

  Finally run the Registry verifier for namespace `io.github.Abhigyan-Shekhar/Waggle-mcp`, the validated version, and package `waggle-mcp`.
- [ ] Implement `release-assets` with `needs: mcp-registry-publish`, the exact tag-push condition, `permissions: {contents: write}`, and `timeout-minutes: 5`. Download the original distributions. With `GH_TOKEN: ${{ github.token }}`, use `gh release view "$GITHUB_REF_NAME"`; when absent, run `gh release create "$GITHUB_REF_NAME" --draft --verify-tag --generate-notes --title "Waggle $GITHUB_REF_NAME"`; then run `gh release upload "$GITHUB_REF_NAME" dist/* --clobber`. Never invoke `gh release edit --draft=false` or equivalent.
- [ ] Run the workflow policy test. Expected: pass.
- [ ] Install the plan-recorded actionlint binary into a temporary directory, verify the download checksum before extraction, and lint the workflow:

  ```bash
  actionlint .github/workflows/publish-python-and-mcp.yml
  ```

  Expected: no output and exit 0. If the host is not Darwin arm64, select the matching asset from the same release and verify that asset's published digest.
- [ ] Run Ruff and the workflow-focused tests:

  ```bash
  .venv/bin/python -m pytest tests/test_publish_workflow.py tests/test_check_release_publication.py tests/test_registry_readiness.py -q
  .venv/bin/python -m ruff check scripts/check_release_publication.py scripts/check_registry_readiness.py tests/test_check_release_publication.py tests/test_registry_readiness.py tests/test_publish_workflow.py
  .venv/bin/python -m ruff format --check scripts/check_release_publication.py scripts/check_registry_readiness.py tests/test_check_release_publication.py tests/test_registry_readiness.py tests/test_publish_workflow.py
  ```

- [ ] Commit Task 3 files:

  ```bash
  git add pyproject.toml tests/test_publish_workflow.py .github/workflows/publish-python-and-mcp.yml
  git commit -m "ci: add gated PyPI and MCP publishing workflow"
  ```

---

### Task 4: Owner and contributor publishing guide

**Files:**

- Create: `docs/publishing/mcp-registry.md`

**Interfaces:**

- Contributor instructions contain only credential-free validation.
- Owner instructions clearly label external GitHub/PyPI configuration and authenticated commands.
- The inclusion request remains an unsent template and contains no claim that a new release exists.

- [ ] Re-open the current primary sources while writing: PyPI Trusted Publisher setup/usage, MCP Registry authentication, GitHub Actions, publisher commands, versioning, official API, and the latest GitHub `github-mcp-server` Q&A guidance on initial `github.com/mcp` onboarding. Record access date 2026-08-10. State that initial GitHub MCP Registry onboarding is manually curated while later official-registry versions sync after onboarding, as currently clarified in GitHub discussion #1257.
- [ ] Document the immutable namespace and GitHub ownership check, including why the repository owner—not a general contributor—must configure identities and approvals.
- [ ] Explain version sync accurately: contributors update `pyproject.toml`, run `python scripts/sync_release_metadata.py --write` in a normal release PR, review and commit the generated diff, then use `--check`. The publishing workflow itself only runs `--check` and never writes or pushes.
- [ ] Include a copy-paste contributor validation block covering metadata, schema/project validation, Ruff, full pytest, build, twine, artifact inspection, wheel README inspection, clean venv install, offline doctor, and stdio smoke. Use a temporary directory for schema, venv, database, and build output.
- [ ] Document first-time owner setup for PyPI Trusted Publishing with repository `Abhigyan-Shekhar/Waggle-mcp`, workflow `publish-python-and-mcp.yml`, environment `pypi`, and no long-lived token. Document GitHub environments `pypi` and `mcp-registry`, required reviewers, and optional wait timers as settings outside YAML.
- [ ] Walk through the four automated jobs and explicitly state: manual dispatch stops after `validate-build`; only a matching pushed tag can publish; PyPI must be publicly verified before Registry auth; Registry metadata must already be committed; release assets reuse an existing release or create a draft; automation never publishes the draft.
- [ ] Add manual owner recovery commands for interactive `mcp-publisher login github`, `mcp-publisher publish`, exact Registry API verification, and `gh release upload`. Keep `twine upload` out of the normal recovery path after an uncertain PyPI result; direct the owner to query PyPI first.
- [ ] Cover failures: tag mismatch, metadata drift, missing README marker, archive contamination, PyPI/GitHub OIDC claim mismatch, environment approval rejection, propagation timeout, PyPI succeeded/Registry failed, Registry publish succeeded/verification timed out, and release asset failure. For every partial state, say which publish command must not be repeated.
- [ ] Include the known pre-existing state without implying this work fixed it: official Registry advertises PyPI `0.1.8`, while PyPI has only `0.0.1`; the proposed next repo version is `0.1.22` pending owner confirmation.
- [ ] Add a concise owner-only checklist in this exact order: confirm chosen version; confirm tracked metadata; confirm PyPI trusted-publisher identity and `pypi` environment gate; push the exact tag; approve environments; verify PyPI project/version and live README marker; if using manual recovery authenticate/publish with `mcp-publisher`; verify exact Registry namespace/version/package; install from PyPI in at least one real MCP client; review and publish the draft GitHub release; request initial GitHub MCP Registry onboarding through the current GitHub Q&A/manual-curation process.
- [ ] Add an unsent inclusion-request draft containing: one-sentence value proposition; `Abhigyan-Shekhar/Waggle-mcp`; `io.github.Abhigyan-Shekhar/Waggle-mcp`; `waggle-mcp`; local-first/no hosted backend; supported clients; security/privacy posture; `pipx install waggle-mcp` (plus the actual client config command if documented in this repository); and placeholders expressed as owner actions—not fake URLs—for inserting verified PyPI and Registry evidence before sending.
- [ ] Review every imperative and label it “Contributor,” “Maintainer,” or “Repository owner.” Confirm no sentence says a publication occurred.
- [ ] Commit the guide:

  ```bash
  git add docs/publishing/mcp-registry.md
  git commit -m "docs: add MCP publishing runbook"
  ```

---

### Task 5: Credential-free end-to-end verification and local handoff

**Files:**

- Modify only files from Tasks 1-4 if verification exposes a defect.

**Interfaces:**

- Produces literal local verification output, a clean local branch, and a PR draft description. Produces no remote state.

- [ ] Create a fresh Python 3.11 venv outside the repository checkout and install build tooling plus the project dev extra. Confirm `python -V` reports 3.11.x and `mcp` resolves to the declared `>=2,<3` range.
- [ ] Run all static and unit checks from that clean environment:

  ```bash
  python scripts/sync_release_metadata.py --check
  python -m ruff check .
  python -m ruff format --check .
  WAGGLE_MODEL=deterministic python -m pytest -q
  actionlint .github/workflows/publish-python-and-mcp.yml
  ```

  Expected: every command exits 0; actionlint emits no diagnostics.

- [ ] Fetch the schema URI declared in `server.json` into a fresh temp directory and run `check_registry_readiness.py schema` and `project`. Preserve the raw curl and checker output.
- [ ] Build into a fresh temp `dist` directory, run `twine check`, the new `artifacts` check, and the existing `wheel` check. Preserve `ls -l` plus all checker output to prove exactly one wheel and one sdist were inspected.
- [ ] Create another clean venv, install only the built wheel, run offline deterministic `waggle-mcp doctor`, and run the stdio checker with a temp SQLite database/work directory. Confirm no database, venv, or built artifact appeared in the checkout.
- [ ] Run the manual-dispatch policy test again and inspect the rendered workflow to confirm all three remote-state jobs use the exact tag-push condition, both publishing environments are present, OIDC exists only on publish jobs, and `--write`, `git commit`, and `git push` are absent.
- [ ] Perform only read-only public queries for the pre-existing PyPI and Registry state. Do not invoke any authentication or publishing command. Report the raw results separately from workflow verification.
- [ ] Review `git diff codex/server-json-registry-readiness...HEAD`, `git status --short`, and the commit list. Confirm `server.json` is unchanged and no build, database, credential, key, or environment file is tracked.
- [ ] Prepare this local-only handoff text:

  ```text
  Branch: codex/release-automation-publishing
  PR title: Add gated PyPI and MCP Registry release automation

  PR description:
  - validates, builds, inspects, installs, and smoke-tests one release artifact set
  - publishes through environment-gated OIDC jobs only on an exact v* tag push
  - verifies PyPI before publishing already-committed MCP Registry metadata
  - reuses an existing GitHub release or creates a draft and never publishes it
  - adds contributor/owner publishing and partial-failure recovery documentation

  Verification: include literal outputs from the clean Python 3.11 suite, actionlint,
  build/inspection/install/smoke run, and read-only public queries.

  No publish action occurred or was attempted. This branch was not pushed and no PR
  was opened because Tracks 1 and 2 are not yet merged.
  ```

- [ ] Do not push. After Tracks 1 and 2 merge, fetch `main`, rebase this branch onto the new `main`, recreate the clean Python 3.11 environment, and rerun every Task 5 command from scratch. Only then is the branch eligible for push and PR creation.

## Plan Self-Review Checklist

- [ ] Coverage: every approved design invariant maps to a Task 3 structural assertion, workflow step, or Task 4 owner instruction.
- [ ] Placeholders: there are no guessed publisher versions, checksums, action SHAs, endpoints, namespaces, environments, job names, or dependency edges. The inclusion draft's evidence slots are explicitly future owner actions because publication is out of scope.
- [ ] Type/contract consistency: helper signatures match test injection points and CLI arguments; job outputs/`needs` references use the same `version` name; artifact paths match upload/download destinations; Registry response assertions match the endpoint fixture shape.
- [ ] Safety: no implementation verification step authenticates, publishes, tags, pushes, creates a release, or opens a PR.
