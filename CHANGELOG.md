# Changelog

## v0.1.17

### Added

- Added Apache-2.0 licensing for Waggle Core via `LICENSE`.
- Added `docs/commercial.md` to clarify the public Core vs future paid Plus split.
- Updated README positioning to present this repository as public Waggle Core
  and Waggle Plus as coming soon.

- **Temporal validity enforcement** (`valid_to` / `valid_from` filtering):
  - `query_graph` and `aggregate_graph` now exclude nodes whose `valid_to` has
    passed by default.
  - New parameter `include_invalidated: bool = False` on both tools — set to
    `true` to retrieve expired nodes.
  - New parameter `as_of: Optional[datetime] = None` on both tools — when
    provided, returns only nodes whose validity window contains that point in
    time (overrides `include_invalidated`).
  - `resolve_conflict` accepts a new optional `winner` parameter (node ID).
    When provided and the edge type is `CONTRADICTS` or `UPDATES`, the losing
    node's `valid_to` is set to `now()`, superseding it from future default
    queries.  Passing a `winner` that is not an endpoint of the edge raises
    `ValueError`.

### Feature flag (temporary — removal scheduled)

- `WAGGLE_ENFORCE_VALID_TO` environment variable:
  - Default / unset / `"true"` → enforcement is **active** (new behaviour).
  - `"false"` → enforcement is **disabled** (legacy behaviour: expired nodes
    appear in default queries).  A deprecation warning is logged.
  - **This flag will be removed in the next minor release.**  Update any
    integrations that rely on the legacy behaviour before upgrading.

### Changed

- `resolve_conflict` now records `winner` in edge metadata when provided.
- Supersession events are logged at `INFO` level with both node IDs and the
  resolution timestamp.
