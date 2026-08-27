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
- The public workspace now leads with Current Context, Key Decisions, Recent
  Memories, Pending Human Review, and a live graph preview. A persistent
  six-step Guided Demo advances only from real WebMCP and human-review events.
- The free Render deployment is split into a static frontend at
  `https://waggle-webmcp.onrender.com` and a Docker/ASGI backend at
  `https://waggle-webmcp-api.onrender.com` with exact-origin credentialed CORS.
- The demo backend uses Waggle's deterministic embedding mode so first-use
  seeding stays within the free instance's memory and health-check limits.
- Cross-origin demo sessions are issued as HttpOnly, Secure, SameSite=None
  cookies. The Apache-2.0 `LICENSE` remains present at repository root.

## Current validation

- Public acceptance run: `2026-08-26` against
  `https://waggle-webmcp.onrender.com` at branch commit `7ba0f71`.
- `/health/live`, `/health/ready`, `/`, every `/workspace` route, `/graph`, and
  the production JavaScript asset returned successfully.
- The public fixture returned 25 nodes and 10 edges. Graph Studio loaded those
  25 nodes from the same browser session, exposed the focused Storage
  architecture view, showed its three real edges, and offered `Show full graph`.
- The credentialed CORS response allows only the exact frontend origin. An
  unrelated origin was rejected, and the demo cookie was issued as HttpOnly,
  Secure, and SameSite=None.
- Two independent HTTP sessions received distinct deterministic memory IDs.
  Cross-session mutation was rejected; resetting session A removed only A's
  proposal while session B remained unchanged. Both test sessions were reset.
- A complete public API acceptance flow passed: project brief, authoritative
  recall, proposal, edited human approval, approved application, and corrected
  recall. The resulting graph contained the native `updates` edge with proposal
  and reviewer provenance, and the activity feed contained the full sequence.
- The Codex in-app browser discovered all four tools from the deployed page and
  invoked them through its native WebMCP capability: `get_project_brief`,
  `recall_memory`, `propose_memory_change`, and
  `apply_approved_memory_change`.
- A complete browser-level WebMCP run passed on the public deployment. The guide
  advanced only after the real tool calls and human Edit & Approve action; final
  recall returned "Use SQLite by default; Neo4j remains optional." as an
  authoritative `human_approved_proposal` memory, and focused Graph Studio
  displayed its native `updates` lineage.
- This validates deployed WebMCP discovery and invocation in the Codex browser.
  The consumer ChatGPT surface remains a separate compatibility check and is
  not claimed as complete.
- Two consumer ChatGPT Prompt 1 attempts returned a generic "Waggle is not
  available" response rather than a `get_project_brief` invocation. They are
  recorded as failed discovery attempts, not acceptance evidence. The Guided
  Demo and judge-facing instructions now require verifying all four page-level
  Site tools in ChatGPT's built-in browser before sending Prompt 1.

## Next task

- Repeat the public flow twice in the consumer ChatGPT surface using the exact
  judge prompts after confirming **Site tools → Available site tools**, record
  any surface-specific discovery issue, then capture the final video and
  submission gallery.

## Tests passing

- Python focused integration suite: 25 passed.
- Ruff checks for the WebMCP backend and tests: passed.
- Frontend unit suite: 26 passed.
- Production Vite bundle: built successfully.
- Chromium workspace and Graph Studio browser suite: 14 passed.

## Known issues

- The repository contained substantial unrelated modified and untracked files before this work began. WebMCP changes must remain isolated and must not overwrite them.
- Automated Playwright coverage still uses a `document.modelContext`
  compatibility shim, while a separate public Codex-browser acceptance run has
  now exercised native WebMCP discovery and invocation. Consumer ChatGPT still
  needs its own final compatibility run.
- The existing frontend bundle emits Vite's chunk-size warning; this change does
  not introduce a separate lazy-loaded WebMCP chunk.
- Render's free backend spins down after inactivity, so its first request can
  take 50 seconds or more.
- Free Render web services do not provide a persistent disk. Browser state can
  therefore be lost on restart or redeploy, but the deterministic fixture
  automatically reseeds for the same session on its next request.
- ChatGPT Sites cannot host this implementation unchanged: the existing runtime
  is Python ASGI with native SQLite, while Sites requires a Worker-compatible
  JavaScript runtime and its supported persistence services.
