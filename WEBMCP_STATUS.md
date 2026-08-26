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
- Phase 5 workspace UX completed: `/` and `/workspace` now lead with a calm
  project overview, while Memories, Proposals, and Activity provide focused
  governed-memory views and Graph Studio remains available at `/graph`.
- WebMCP activity, proposal creation, human review, application, and corrected
  memory refresh update the workspace without a page reload.
- Phase 6 isolated judge mode completed locally: a fresh browser receives an
  opaque secure session cookie and an automatically seeded 25-memory workspace.
- Every browser session maps the public `waggle-webmcp` alias to a distinct
  tenant and physical project namespace; graph state, proposals, and audit
  events are isolated together.
- The deterministic fixture centers on the authoritative decision "Use Neo4j
  as the primary storage engine." and supports the complete recall, proposal,
  human approval, application, and corrected-recall story.
- Server-side reset clears and reseeds only the current browser's workspace in
  one SQLite transaction. Refresh preserves state; reset restores exact IDs and
  content; a second browser remains unaffected.
- The existing workspace now exposes only the required `Challenge Demo`
  indicator and `Reset Demo` control. The Phase 5 information architecture is
  otherwise frozen.
- The Render Blueprint now deploys one Docker/ASGI origin with a persistent
  SQLite volume and secure demo mode enabled. The Apache-2.0 `LICENSE` remains
  present at repository root.

## Current blocker

- The Blueprint has not been applied to a public Render service from this task,
  so the final HTTPS URL, real ChatGPT discovery, and live invocation remain
  external acceptance items.

## Next task

- Deploy this branch from `render.yaml`, run the public checks in
  `docs/webmcp-judge-runbook.md`, and immediately validate all four tools in a
  real ChatGPT WebMCP session.

## Tests passing

- Python focused integration suite: 21 passed.
- Ruff checks for the WebMCP backend and tests: passed.
- Frontend unit suite: 16 passed.
- Production Vite bundle: built successfully.
- Chromium workspace and Graph Studio browser suite: 14 passed.

## Known issues

- The repository contained substantial unrelated modified and untracked files before this work began. WebMCP changes must remain isolated and must not overwrite them.
- Automated browser coverage uses a `document.modelContext` compatibility shim.
  Discovery and invocation in a real hosted ChatGPT WebMCP session still need a
  manual compatibility test once a public deployment is available.
- The existing frontend bundle emits Vite's chunk-size warning; this change does
  not introduce a separate lazy-loaded WebMCP chunk.
