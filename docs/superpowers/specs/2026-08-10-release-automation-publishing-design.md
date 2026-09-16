# Release Automation and MCP Publishing Design

**Date:** 2026-08-10

**Status:** Approved for implementation planning

## Purpose

Add a release workflow and an owner-focused publishing guide for the Python
package and official MCP Registry entry. The workflow will build and validate
the exact source selected by a release tag, publish the already-built artifacts
to PyPI using short-lived GitHub OIDC credentials, publish the matching
`server.json` using MCP Registry GitHub OIDC, verify both public results, and
attach the original distributions to a draft GitHub release.

This track authors and locally verifies release automation. It does not execute
any authenticated publication, create or push a tag, configure a trusted
publisher, authenticate a maintainer, push the feature branch, or open a pull
request while Tracks 1 and 2 remain unmerged.

## Current Repository Context

Track 3 starts from local branch `codex/server-json-registry-readiness`, whose
tip is commit `64658aa`. That branch contains Track 1's
`scripts/sync_release_metadata.py` and Track 2's
`scripts/check_registry_readiness.py`, package inspection, installed-wheel
doctor check, and installed stdio smoke test.

The latest local version tag is `v0.1.21`. Track 1 records `0.1.22` as the
proposed next version, pending owner confirmation; this design does not treat
that proposal as final. The published namespace remains exactly
`io.github.Abhigyan-Shekhar/Waggle-mcp` and must not be renamed. The current
official schema version remains `2025-12-11`, as declared by the Registry's
live `pkg/model/constants.go` on `main`.

The live Registry currently advertises PyPI package version `0.1.8`, while
PyPI's release history contains only `0.0.1`. This is a known pre-existing
remote-state mismatch. Authoring this workflow does not resolve it; a future
successful release through the workflow will.

## Design Invariants

1. A manual `workflow_dispatch` run is always validation-only. It cannot reach
   PyPI, the MCP Registry, or GitHub release creation.
2. Only a pushed tag beginning with `v` can reach publish jobs, and a validation
   step must prove the tag is exactly `v` plus `pyproject.toml`'s version.
3. Release metadata must already be correct in the tagged commit. Every release
   path runs `python scripts/sync_release_metadata.py --check`; no workflow step
   runs `--write`, commits generated changes, or pushes to `main`.
4. PyPI publication must succeed and the exact version must become publicly
   visible before MCP Registry authentication or publication begins.
5. The Registry publication job must run the metadata check again immediately
   before authentication so runner-local drift cannot be published.
6. Both authenticated publish jobs use dedicated GitHub environments:
   `environment: pypi` and `environment: mcp-registry`. The owner configures
   required reviewers and optional wait timers in repository settings.
7. Each job receives only the permissions it needs. OIDC permissions are
   granted at job scope and are absent from validation and release-asset jobs.
8. Build artifacts are created once. Publish and release jobs download the
   artifacts produced by the validated build job and never rebuild them.
9. MCP Registry versions are immutable. The workflow contains an explicit
   comment that publishing the same Registry version again is not a safe retry.
10. The workflow may create a draft GitHub release or update an existing release
    for the tag, but it never publishes a draft release. That final public action
    remains owner-controlled.

## Files and Responsibilities

### `.github/workflows/publish-python-and-mcp.yml`

Defines the trigger policy, the four ordered jobs, per-job permissions,
environment gates, artifact handoffs, and authenticated commands. Every
third-party action is pinned to a verified full commit SHA and annotated with
its human-readable release version, following existing repository convention.

### `scripts/check_release_publication.py`

Provides testable release checks that would otherwise become opaque inline YAML
or shell. It has three subcommands:

- `tag --tag <tag>` loads `pyproject.toml`, requires the tag to equal
  `v<project-version>`, and fails with a deterministic diagnostic on mismatch.
- `pypi --project waggle-mcp --version <version>` polls the public PyPI JSON API
  with bounded attempts and verifies the returned project name and exact
  version.
- `registry --name <namespace> --version <version> --package waggle-mcp` polls
  the official version endpoint using a URL-encoded namespace and verifies the
  exact server name, server version, PyPI package identifier, and PyPI package
  version.

The network checks use a 10-second request timeout and injectable sleep/HTTP
boundaries for unit tests. PyPI polling makes 20 attempts separated by 15
seconds; Registry polling makes 12 attempts separated by 10 seconds. Timeout,
malformed responses, or mismatched metadata produce nonzero exit status. The
helper never publishes or authenticates.

The script is standalone and uses only the Python standard library. The
validated build job uploads it as a separate `release-verifier` artifact so the
PyPI job can perform public verification without checking out the repository or
installing project dependencies while `id-token: write` is available.

### `tests/test_check_release_publication.py`

Covers accepted and rejected tag/version combinations, URL encoding, transient
404 retry behavior, bounded timeout behavior, malformed JSON, mismatched PyPI
metadata, mismatched Registry metadata, and the command-line exit contract.
All network and waiting behavior is replaced with deterministic test doubles.

### `scripts/check_registry_readiness.py` and
### `tests/test_registry_readiness.py`

Extend Track 2's package inspection with an `artifacts` subcommand. It requires
exactly one wheel and one `.tar.gz` source distribution, rejects absolute paths,
path traversal, caches, local databases, environment files, credentials, keys,
and other explicitly forbidden development artifacts, and requires the package
metadata and expected Waggle package roots. Existing wheel README and
`mcp-name` checks remain unchanged. Tests build small synthetic wheel and sdist
archives covering required members and each rejected path class.

### `docs/publishing/mcp-registry.md`

Documents contributor validation, owner setup, automation behavior, manual
publication, verification, recovery, the human-only checklist, and an unsent
GitHub MCP Registry inclusion-request draft. It distinguishes repository-owner
actions from actions available to any maintainer with merge access.

## Workflow Architecture

### Triggers and concurrency

The workflow accepts:

- `workflow_dispatch`, with no publish or dry-run toggle; the event itself is
  always validation-only.
- `push.tags: ["v*"]`; the tag validation helper enforces the exact version
  shape and equality rather than relying on GitHub's glob syntax as validation.

Concurrency is grouped by ref, with `cancel-in-progress: false`. A later run
must not cancel a release that may already have crossed an irreversible publish
boundary.

### Job 1: `validate-build`

This job runs for both supported events with `contents: read` and
`timeout-minutes: 30`. It performs these stages in order:

1. Check out the exact ref without persisting credentials.
2. Set up Python 3.11, matching Track 2's package-readiness job and the
   repository's minimum supported version.
3. Install lint, test, build, and Registry validation dependencies.
4. Run the tag/version check on tag events.
5. Run `sync_release_metadata.py --check` on every event.
6. Fetch the schema declared by `server.json` and run Track 2's schema and
   project checks.
7. Run Ruff lint and format checks.
8. Run the complete pytest suite.
9. Build one wheel and one source distribution into `dist/`.
10. Run `twine check` and Track 2's extended artifact inspection; require
    exactly one wheel and one sdist, reject unsafe or forbidden development
    files, and verify the wheel's embedded README and `mcp-name` marker.
11. Install the wheel into a new virtual environment, run `waggle-mcp doctor`
    offline, and smoke-test MCP initialization and tool listing over stdio using
    Track 2's existing commands and deterministic local configuration.
12. Upload `dist/` as a `python-distributions` workflow artifact and the
    standalone `scripts/check_release_publication.py` as a `release-verifier`
    artifact, both with seven-day retention, and expose the validated version
    as a job output.

No later job can start if any stage fails. On `workflow_dispatch`, all later
jobs are structurally skipped and the workflow ends after this job.

### Job 2: `pypi-publish`

This job has `needs: validate-build`, a tag-push-only job condition,
`environment: pypi`, job-scoped `id-token: write`, and
`timeout-minutes: 10`. It downloads the exact `python-distributions` artifact
and passes it to the PyPA trusted-publishing action. The job contains no
password, API-token secret, checkout, build, or project dependency installation.

After the upload action succeeds, the job downloads the validated
`release-verifier` artifact and invokes its PyPI subcommand with the runner's
Python. A bounded poll absorbs normal index propagation delay. The job succeeds
only when the exact version is publicly visible and its metadata matches the
expected project.

### Job 3: `mcp-registry-publish`

This job has `needs: pypi-publish`, a tag-push-only job condition,
`environment: mcp-registry`, and job-scoped `contents: read` plus
`id-token: write`, with `timeout-minutes: 10`. It checks out the tagged commit
without persisted credentials and installs a pinned `mcp-publisher` binary with
checksum verification.

The design intentionally does not name a publisher version. Immediately before
writing the implementation plan, fetch the live
`modelcontextprotocol/registry` releases page and GitHub Releases API again.
The plan must pin the release that is actually marked latest at that time and
the exact SHA-256 digest published by GitHub for its
`mcp-publisher_linux_amd64.tar.gz` asset. Record the literal source output used
to select both values. Do not reuse a version or checksum from this design,
search-result snippets, prior conversation, or memory. If the releases page,
API tag, asset name, and asset digest do not agree, stop instead of guessing.

After installing that verified asset, the job performs these ordered steps:

1. Run `python scripts/sync_release_metadata.py --check` and fail on any drift.
2. Run `mcp-publisher login github-oidc`.
3. Run `mcp-publisher publish` with a workflow comment stating that Registry
   versions are immutable and this command is not safe to repeat for the same
   version.
4. Invoke the Registry subcommand from `check_release_publication.py` and
   validate the exact namespace, version, PyPI identifier, and PyPI version from
   the public Registry API.

No workflow step modifies, commits, or pushes repository metadata. If the tag's
tracked metadata is stale, publication stops before authentication.

### Job 4: `release-assets`

This job has `needs: mcp-registry-publish`, a tag-push-only job condition, and
only `contents: write`, with `timeout-minutes: 5`. It downloads the original
distributions and uses the GitHub CLI with `GH_TOKEN` to look up the release
matching the tag.

If the release does not exist, the job creates a **draft** release for the
already-existing tag, with generated notes, then uploads the wheel and sdist.
If a draft or published release already exists, it reuses that release and
uploads or replaces the two named distribution assets. The job never changes a
draft to published. The human checklist ends with the owner reviewing notes and
publishing the draft after all automated and client checks are satisfactory.

## Failure Handling and Recovery

- Validation or build failure produces no remote release state. Fix the tagged
  commit by choosing a new version and tag; do not move or reuse a published
  release tag.
- PyPI environment rejection or OIDC failure produces no Registry publication.
- If PyPI upload succeeds but public polling times out, first inspect PyPI. Do
  not blindly rerun the upload for an immutable version. If the version exists,
  use the documented owner recovery path to verify PyPI and continue Registry
  publication manually.
- If PyPI succeeds but Registry authentication, publication, or verification
  fails, leave PyPI intact. Correct the Registry-side cause and use the
  documented manual owner commands for the same `server.json` version, provided
  the version has not already been registered.
- If Registry publication succeeds but Registry verification times out, query
  the exact public version endpoint before retrying. Never issue a second
  publish merely because verification was delayed.
- If both registries succeed but release asset attachment fails, rerun only the
  failed final job when GitHub permits it or use the documented `gh release`
  commands. Do not rerun either publishing job.
- If a wrong version reaches PyPI or the Registry, neither remote version is
  overwritten. Prepare a corrected higher version and document the bad release
  according to each registry's supported yank or moderation process.

## Publishing Guide Content

`docs/publishing/mcp-registry.md` will contain:

1. The immutable namespace and GitHub ownership-verification model.
2. How `sync_release_metadata.py --check` keeps `pyproject.toml`, `server.json`,
   and the README marker aligned before a tag exists.
3. Exact local validation, lint, test, build, inspection, install, doctor, and
   stdio smoke commands contributors can run without credentials.
4. Owner-only setup for PyPI trusted publishing, including the repository,
   workflow filename, and `pypi` environment identity that must match PyPI.
5. First-time interactive `mcp-publisher login github` instructions for manual
   recovery, described but not executed.
6. The automated four-job workflow, environment approvals, permissions, public
   verification, and draft-release behavior.
7. Manual publication and verification commands for PyPI and the MCP Registry,
   clearly separated from normal contributor tasks.
8. Common tag mismatch, metadata drift, OIDC claim mismatch, package-marker,
   propagation delay, immutable-version, and partial-release failures.
9. The recovery rules above, emphasizing when a publish command must not be
   repeated.
10. A concise owner checklist covering trusted-publisher confirmation, the
    chosen version, the PyPI README marker, Registry authentication and
    publication, exact API verification, installation from PyPI in at least one
    real MCP client, review of the draft GitHub release, and the current GitHub
    MCP Registry inclusion process.
11. An unsent inclusion-request draft with Waggle's value proposition,
    repository, MCP namespace, PyPI package, local-first/no-hosted-backend
    explanation, supported clients, security/privacy posture, install command,
    and a final sentence stating that the owner will replace it with links to
    the verified PyPI and Registry records before sending.

The guide links primary sources: PyPI trusted-publisher documentation, official
MCP Registry authentication/GitHub Actions/API/versioning documentation, and
GitHub's current MCP Registry inclusion guidance. The inclusion process is
re-verified while writing because GitHub has described both automatic ingestion
from the official Registry and a direct inclusion-request contact.

## Verification Strategy

Implementation verification is divided into credential-free checks and owner
checks.

Credential-free checks run locally and include:

- Unit tests for every new release-check helper path.
- Existing Track 1 and Track 2 focused tests.
- Ruff lint and format checks for modified Python files.
- Static GitHub Actions validation with `actionlint`.
- YAML parsing and assertions that manual dispatch cannot satisfy any publish
  job condition, both publish jobs declare their required environments, OIDC is
  job-scoped, the Registry job depends on PyPI, and the release job depends on
  Registry verification.
- The complete pytest suite.
- A real wheel/sdist build, distribution inspection, clean-environment wheel
  install, offline doctor, and stdio smoke test.
- Public read-only queries to PyPI and the MCP Registry for documenting the
  pre-existing state.

Owner-only checks cannot be truthfully completed in this track:

- PyPI trusted-publisher configuration and environment protection rules.
- PyPI OIDC token exchange and actual upload.
- MCP Registry GitHub OIDC authentication and actual publication.
- GitHub environment approvals.
- Testing the newly published package from a real MCP client.
- Reviewing and publishing the generated draft GitHub release.
- Sending the GitHub MCP Registry inclusion request.

The final implementation report presents literal output for every
credential-free command and labels each owner-only check as untested rather than
implying that a release occurred.

## Branch and Delivery Strategy

Work proceeds on `codex/release-automation-publishing`, created from
`codex/server-json-registry-readiness`. The branch may be implemented, tested,
and committed locally, but it must not be pushed and no pull request may be
opened while Tracks 1 and 2 remain unmerged.

After both dependencies merge, refresh `main`, rebase this branch onto the new
`main`, and rerun every unit, lint, workflow, build, inspection, install, smoke,
and public read-only verification command from scratch. Only a clean rebased
result is eligible to push and open as a Track 3 pull request.

## Primary References

- [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/)
- [Publishing with a PyPI trusted publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [MCP Registry GitHub Actions](https://modelcontextprotocol.io/registry/github-actions)
- [MCP Registry authentication](https://modelcontextprotocol.io/registry/authentication)
- [Official MCP Registry API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md)
- [Current MCP Registry schema constants](https://github.com/modelcontextprotocol/registry/blob/main/pkg/model/constants.go)
- [GitHub MCP Registry publication guide](https://github.blog/ai-and-ml/generative-ai/how-to-find-install-and-manage-mcp-servers-with-the-github-mcp-registry/)
