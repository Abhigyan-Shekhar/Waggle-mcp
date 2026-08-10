# GitHub Marketplace release checklist

This is a human-only checklist for the repository owner. No item below was performed automatically by the implementation task, and checking a box represents the owner's own verification.

## Repository and identity

- [ ] Confirm or create a separate public repository for this Action; do not publish the `dist/` subdirectory directly.
- [ ] Copy this directory's contents so `action.yml` is at the public repository root.
- [ ] On the publication date, search GitHub Marketplace for the exact name **Waggle Context Handoff** and record the dated result below. If unavailable, consider **Waggle Memory Handoff**.
- [ ] Confirm the repository owner has two-factor authentication enabled.
- [ ] Review and personally accept the GitHub Marketplace Developer Agreement if required. Automation must not accept it.

Name search record:

- Date: 2026-08-10
- Query: `Waggle Context Handoff`
- Search URL: <https://github.com/marketplace?type=actions&query=Waggle%20Context%20Handoff>
- Result: no matching indexed GitHub Marketplace Action was found in the implementation-time exact-name search. Searches for `Waggle Memory Handoff`, `Waggle`, `context handoff`, and `memory handoff` also returned no conflicting Waggle listing.
- Recommendation: keep **Waggle Context Handoff**. Marketplace availability is not a reservation; the owner must repeat the direct search immediately before publication and use **Waggle Memory Handoff** if a conflict appears.

## Release readiness

- [ ] Confirm `waggle-mcp==0.1.25` exists publicly and that its `waggle-mcp ingest-github-event --help` exposes the required command. Update the exact default version if it does not.
- [ ] Run unit, metadata, YAML, lint, type, and all fixture integration tests from a clean checkout.
- [ ] Confirm all remote Actions are pinned to reviewed 40-character commit SHAs.
- [ ] Confirm examples request only `contents: read` and do not execute untrusted pull-request code.
- [ ] Review `SECURITY.md`, the Apache-2.0 license, changelog, and release notes.

## Tags, release, and Marketplace

- [ ] Create a semantic version tag such as `v1.0.0` at the reviewed commit.
- [ ] Create or move the major-version tag `v1` to the same commit.
- [ ] Draft the GitHub release from `v1.0.0`; do not reuse or move the semantic tag.
- [ ] Select accurate Marketplace categories such as **Utilities** and **Continuous integration**.
- [ ] Check **Publish this Action to the GitHub Marketplace** in the release UI.
- [ ] Publish the release only after reviewing the rendered Marketplace listing.
- [ ] Verify the live Marketplace page shows the expected name, branding, inputs, README, and latest major version.
- [ ] From a fresh repository, run the public issue example pinned to a reviewed Action commit SHA and verify the context, checkpoint, summary, and artifact.
