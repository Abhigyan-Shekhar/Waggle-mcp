# Waggle WebMCP Status

## Completed

- Phase 0 repository diagnosis completed.
- Dedicated challenge branch created: `codex/waggle-webmcp`.
- Official OpenAI WebMCP registration contract verified.
- Phase 1 vertical slice completed: `get_project_brief` is registered from the
  existing Graph Studio page, calls the Waggle HTTP application, reads real
  scoped graph state, and returns a structured project brief.
- Unsupported browsers retain the normal Graph Studio experience.
- Read authorization, project scoping, validity filtering, provenance IDs, and
  audit logging are preserved in the WebMCP path.
- Phase 2 authoritative recall completed: `recall_memory` uses Waggle's existing
  scoped graph retrieval, projects only current authority, and exposes direct
  supersession provenance.
- The reusable governance fixture `Decision v1 -> Decision v2 -> Decision v3`
  proves normal recall returns only v3.
- Phase 3 proposal workflow completed: `propose_memory_change` persists pending
  application state with a target fingerprint and provenance without modifying
  authoritative graph memory.
- Exact duplicate proposals are idempotent, distinct changes remain allowed, and
  pending proposal cards appear live in the temporary Graph Studio shell and
  survive reload.
- Phase 4 governance lifecycle completed: humans can reject, approve, or
  edit-and-approve an immutable payload; stale target fingerprints block review
  and application; and approved application atomically creates native `updates`
  lineage plus proposal and audit provenance.
- `apply_approved_memory_change` accepts only a proposal ID, is idempotent, and
  cannot supply or alter human-approved content.

## Current blocker

- None for the Phase 4 governance lifecycle.

## Next task

- Begin the Workspace-first UX phase while preserving the now-verified governance
  services and keeping Graph Studio available as a dedicated route or tab.

## Tests passing

- Python focused integration suite: 16 passed.
- Ruff checks for the WebMCP backend and tests: passed.
- Frontend unit suite: 16 passed.
- Production Vite bundle: built successfully.
- Playwright WebMCP browser test: 1 passed.

## Known issues

- The repository contained substantial unrelated modified and untracked files before this work began. WebMCP changes must remain isolated and must not overwrite them.
- The current Graph Studio is only the Phase 1 host page; it is not the final
  workspace-first challenge experience.
- Automated browser coverage uses a `document.modelContext` compatibility shim.
  Discovery and invocation in a real hosted ChatGPT WebMCP session still need a
  manual compatibility test once a public deployment is available.
- The existing frontend bundle emits Vite's chunk-size warning; this change does
  not introduce a separate lazy-loaded WebMCP chunk.
