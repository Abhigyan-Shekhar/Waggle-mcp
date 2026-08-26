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

## Current blocker

- None for the Phase 1 vertical slice.

## Next task

- Begin Phase 2 with a similarly narrow, read-only `recall_memory` tool, then
  introduce the workspace shell and human governance flows in later phases.

## Tests passing

- Python focused integration suite: 8 passed.
- Ruff checks for the WebMCP backend and tests: passed.
- Frontend unit suite: 12 passed.
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
