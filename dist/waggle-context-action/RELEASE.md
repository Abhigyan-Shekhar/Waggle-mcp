# Release process

Only the repository owner performs releases. No script in this repository publishes, pushes, tags, accepts legal terms, or changes Marketplace settings.

1. Complete [MARKETPLACE_RELEASE_CHECKLIST.md](MARKETPLACE_RELEASE_CHECKLIST.md).
2. Confirm the exact default `waggle-version` exists on PyPI and includes `waggle-mcp ingest-github-event`.
3. Run the complete CI suite and the fixture workflow from a clean commit.
4. Review `action.yml`, permissions, remote Action SHAs, documentation, and changelog.
5. Create a signed semantic tag such as `v1.0.0`, then update the moving `v1` tag to the same reviewed commit.
6. Draft a GitHub release from the semantic tag, include compatibility/security notes, and publish only after the Marketplace checks are complete.

Never move an immutable semantic tag. If a release is faulty, publish a new patch release.
