# Repository Map

This document is for contributors, not end users. Its job is to answer four questions quickly:

1. Which files implement each Waggle feature?
2. Which files are safe for a newcomer to edit?
3. Which files have a large blast radius and should be touched carefully?
4. If you change one area, which tests and docs should move with it?

## Read this first

If you are opening the repo for the first time, do not start by skimming random files.

- Start with `README.md` for product scope.
- Then read `CONTRIBUTING.md` for setup and test commands.
- Then use the feature map below to jump directly to the subsystem you want.

## Root layout policy

The repo root is intentionally small. If a file does not need to be discovered by packaging tools, container tooling, or external MCP registries, it should usually not live at the root.

Expected root-level categories:

- project entrypoints and metadata: `README.md`, `pyproject.toml`, `MANIFEST.in`, `LICENSE`
- contributor/community docs: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`, `CHANGELOG.md`, `AGENTS.md`
- deployment entrypoints: `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml`, `render.yaml`
- registry/distribution manifests that external services expect at the root: `smithery.yaml`, `server.json`, `glama.json`, `llms-install.txt`

Everything else should usually live under one of these folders:

- `apps/` for user-facing product surfaces such as the MCP UI bundles and editor extensions
- `docs/` for narrative documentation
- `examples/` for user-facing examples and sample configs
- `scripts/` for operational, benchmark, and one-off utilities
- `deploy/` for deployment-specific assets beyond root entrypoints
- `tests/` for verification and regression coverage

If you add a new top-level file, assume it is in the wrong place until you can justify why a tool outside the repo needs it at the root.

## Quick safety guide

### Usually safe for first contributions

- `docs/**`
- `tests/**`
- `src/waggle/retrieval/hybrid.py`
- `src/waggle/graph.py`
- `src/waggle/intelligence.py`
- `src/waggle/config.py`
- `src/waggle/logging_utils.py`
- `src/waggle/errors.py`

These are still important files, but they are relatively easy to reason about if you stay within one feature and update tests.

### Touch carefully

- `src/waggle/server.py`
- `src/waggle/orchestrator.py`
- `src/waggle/chat_runtime.py`
- `src/waggle/recursive_context.py`
- `src/waggle/models.py`
- `src/waggle/serializer.py`
- `src/waggle/runtime_context.py`
- `src/waggle/neo4j_graph.py`

These files sit on major code paths. A small change here can break multiple tools, clients, or test suites.

### Avoid unless your issue is specifically about them

- `src/rlm/**`
- `apps/vscode-extension/package-lock.json`
- `apps/mcp/graph-ui/node_modules/**`
- generated build outputs such as `.vsix`, packaged bundles, local exports, and transient debug artifacts

Reasons:

- `src/rlm/**` is vendored/adapted support code with a wider reasoning surface.
- lockfiles and generated outputs create noisy diffs and are easy to change accidentally.
- `node_modules` and transient artifacts should not be used as source-of-truth code.

## Feature map

This is the main onboarding section. If you want to work on a feature, start with the files listed for that feature and the tests listed beside it.

### 1. MCP server and CLI surface

What this feature does:
- Exposes Waggle as an MCP server
- Registers tools and resources
- Implements CLI commands like setup, doctor, and serve

Primary files:
- `src/waggle/server.py`
- `src/waggle/config.py`
- `src/waggle/__init__.py`
- `pyproject.toml`

Change here when:
- You add or modify a tool
- You change CLI flags or startup behavior
- You change packaging metadata or command entrypoints

Tests to read first:
- `tests/test_server.py`
- `tests/test_stdio_integration.py`
- `tests/test_packaging_metadata.py`

Blast radius:
- High. Changes can affect every client integration.

### 2. Graph storage and memory correctness

What this feature does:
- Stores nodes, edges, transcripts, and evidence
- Handles traversal, updates, scoping, and validity windows
- Powers the core persistent memory behavior

Primary files:
- `src/waggle/graph.py`
- `src/waggle/models.py`
- `src/waggle/evidence.py`
- `src/waggle/locks.py`
- `src/waggle/neo4j_graph.py`

Change here when:
- Query results are wrong
- Contradictions, updates, or validity windows behave incorrectly
- Storage or traversal performance needs improvement

Tests to read first:
- `tests/test_graph.py`
- `tests/test_edges.py`
- `tests/test_temporal_validity.py`
- `tests/test_valid_to.py`
- `tests/test_dedup.py`

Blast radius:
- High. This is the heart of the product.

### 3. Retrieval quality

What this feature does:
- Finds relevant memory from graph, text, and hybrid search
- Combines ranking signals
- Controls whether context returned to the model is useful

Primary files:
- `src/waggle/retrieval/hybrid.py`
- `src/waggle/embeddings.py`
- `src/waggle/intelligence.py`
- `src/waggle/token_efficiency_benchmark.py`

Change here when:
- Search results are low quality
- Embedding fallback behavior is wrong
- Ranking, recall, or token efficiency needs work

Tests to read first:
- `tests/test_hybrid_retrieval.py`
- `tests/test_recursive_context.py`

Blast radius:
- Medium to high. Retrieval regressions are subtle and can look like “memory is bad” even when storage is correct.

### 4. Recursive context assembly

What this feature does:
- Breaks a task into retrieval subqueries
- Expands graph neighborhoods
- Compresses results into a token-budgeted context pack

Primary files:
- `src/waggle/recursive_context.py`
- `src/waggle/context_bundle.py`
- `src/waggle/rlm.py`
- `src/waggle/runtime_context.py`

Change here when:
- `build_context` returns noisy, incomplete, or badly ranked context
- Token budgeting or packing format needs work

Tests to read first:
- `tests/test_recursive_context.py`
- `tests/test_demo.py`

Blast radius:
- High. This area shapes the model-facing experience directly.

### 5. Automatic memory orchestration

What this feature does:
- Retrieves context before an answer
- Stores durable memory after a completed turn
- Wires Waggle into conversational runtimes

Primary files:
- `src/waggle/orchestrator.py`
- `src/waggle/chat_runtime.py`
- `src/waggle/runtime_context.py`
- `docs/automatic-memory-rules.md`
- `docs/memory-orchestration.md`

Change here when:
- Memory is not being recalled automatically
- Sessions fail to ingest completed turns
- Scope or runtime state is inconsistent across clients

Tests to read first:
- `tests/test_chat_runtime.py`
- `tests/test_observe_conversation_refactor.py`
- `tests/test_ingest_transcript_handoff.py`

Blast radius:
- High. This is where “Waggle should remember automatically” either works or fails.

### 6. Import, export, and `.abhi` workflows

What this feature does:
- Exports memory to portable snapshots
- Imports and verifies snapshots
- Supports diff and merge flows

Primary files:
- `src/waggle/abhi.py`
- `src/waggle/serializer.py`
- `src/waggle/markdown_vault.py`
- `src/waggle/backfill.py`

Change here when:
- Export/import round-trips fail
- Diff/merge behavior is incorrect
- External memory transfer needs improvement

Tests to read first:
- `tests/test_abhi_diff_merge.py`
- `tests/test_diff_merge_fixes.py`
- `tests/test_export_import_v2.py`
- `tests/test_backfill.py`

Blast radius:
- Medium to high. Bugs here can corrupt portability or trust in exported state.

### 7. Hooks and client integrations

What this feature does:
- Installs or runs client-specific memory hooks
- Integrates Waggle with Claude Code and related clients

Primary files:
- `src/waggle/hooks/claude_code/common.py`
- `src/waggle/hooks/claude_code/pre_response.py`
- `src/waggle/hooks/claude_code/post_response.py`
- `src/waggle/hooks/claude_code/pre_compact.py`
- `docs/hooks.md`
- `docs/install/**`

Change here when:
- Client setup is broken
- Hook ordering or payload handling is wrong
- Memory behavior differs across supported clients

Tests to read first:
- `tests/test_hooks.py`
- `tests/test_stdio_integration.py`

Blast radius:
- Medium to high. These files are integration-heavy and often fail at runtime rather than import-time.

### 8. Graph Studio and UI-facing surfaces

What this feature does:
- Serves the graph UI and related assets
- Powers the visual inspection/admin experience

Primary files:
- `src/waggle/graph_ui.py`
- `apps/mcp/graph-ui/**`
- `assets/**` (Storage directory for version-controlled documentation assets)

Change here when:
- UI pages fail to load
- Static assets are broken
- The Graph Studio flow needs improvement or UI changes require refreshing screenshots

Asset & Preview Guidelines:
- **Screenshots:** Screenshot assets belong in `assets/`. They must remain lightweight and be updated manually when the UI changes. Ensure matching documentation files are updated alongside them.
- **Historical Previews:** The standalone `sample-preview.html` file is historical and has been removed. Local UI reviews must use the current Graph Studio development workflow documented in [apps/mcp/graph-ui/README.md](../apps/mcp/graph-ui/README.md).

Tests and checks:
- Read `apps/mcp/graph-ui/README.md`
- If changing frontend code, verify the UI manually after the change

Blast radius:
- Medium. Usually isolated, but packaging and asset paths can leak into the server surface.
### 9. Packaging and external distributions

What this feature does:
- Publishes the Python package
- Builds the VS Code extension
- Builds the Claude Desktop bundle
- Produces release binaries and container images

Primary files:
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `.github/workflows/publish-vscode-extension.yml`
- `.github/workflows/package-claude-desktop-extension.yml`
- `.github/workflows/release-binaries.yml`
- `.github/workflows/publish-image.yml`
- `apps/vscode-extension/**`
- `apps/mcp/claude-desktop-extension/**`

Change here when:
- Release automation is broken
- Package metadata is wrong
- Extension packaging needs work

Tests and checks:
- `tests/test_packaging_metadata.py`
- package-specific README files

Blast radius:
- High for release flows, lower for isolated docs or UI metadata.

## File-by-file guide for `src/waggle/`

Use this when you already know you are inside `src/waggle` and need a quick explanation of a specific file.

| File | What it does | Touch risk |
| --- | --- | --- |
| `__init__.py` | Package version and top-level exports. | Medium |
| `abhi.py` | Portable snapshot import/export, diff, merge. | Medium |
| `auth.py` | Authentication helpers for protected deployments. | Medium |
| `backfill.py` | Backfill/import logic for existing data. | Medium |
| `chat_runtime.py` | Runtime turn handling and orchestration wiring. | High |
| `config.py` | Environment-driven configuration loading. | Medium |
| `context_bundle.py` | Structured context pack formatting. | Medium |
| `drive_sync.py` | Google Drive sync and token file handling. | Medium |
| `embeddings.py` | Embedding model loading and deterministic fallback. | Medium |
| `errors.py` | Shared exceptions and error types. | Low |
| `evidence.py` | Evidence record handling for memory provenance. | Medium |
| `graph.py` | SQLite graph engine and traversal. | High |
| `graph_ui.py` | Graph Studio server/UI integration entrypoints. | Medium |
| `intelligence.py` | Candidate extraction and relationship heuristics. | Medium |
| `locks.py` | Local state locking helpers. | Medium |
| `logging_utils.py` | Logging config and formatting helpers. | Low |
| `markdown_vault.py` | Markdown export/import helpers. | Medium |
| `metrics.py` | Internal metrics and counters. | Low |
| `models.py` | Shared core data models. | High |
| `neo4j_graph.py` | Neo4j graph backend. | High |
| `orchestrator.py` | Automatic memory retrieval/ingestion flow. | High |
| `rate_limit.py` | Rate-limiting helpers. | Low |
| `recursive_context.py` | Context assembly pipeline. | High |
| `rlm.py` | RLM integration layer. | Medium |
| `runtime_context.py` | Runtime scope and session metadata handling. | High |
| `serializer.py` | Serialization and interchange logic. | High |
| `server.py` | CLI entrypoints and MCP tool registration. | High |
| `token_efficiency_benchmark.py` | Benchmark helpers for context efficiency. | Low |

## Non-source paths contributors often ask about

| Path | What it is | Contributor guidance |
| --- | --- | --- |
| `tests/fixtures/` | Fixture data for regression coverage. | Safe to extend, but do not rewrite existing fixtures casually. |
| `third_party/rlm/` | Upstream reference material. | Prefer reading over editing. |
| `examples/` | Example config and sample usage. | Safe for docs-oriented contributions. |
| `deploy/` | Infra manifests and observability helpers. | Touch only if your issue is deployment-specific. |
| `templates/waggle-plus/` | Template/package material. | Treat as distribution-facing. Keep changes intentional. |
| `apps/mcp/graph-ui/node_modules/` | Installed frontend dependencies. | Do not edit by hand. |

## If you change X, also check Y

- If you change `server.py`, also check `tests/test_server.py`, `tests/test_stdio_integration.py`, and `docs/reference.md`.
- If you change `graph.py`, also check `tests/test_graph.py`, `tests/test_edges.py`, and temporal-validity tests.
- If you change `orchestrator.py` or `chat_runtime.py`, also check `docs/automatic-memory-rules.md`, `docs/memory-orchestration.md`, and runtime tests.
- If you change `.abhi` behavior, also check import/export tests and any docs that describe the format.
- If you change install or hook behavior, also check `docs/install/**`, `docs/hooks.md`, and integration tests.
- If you change release or packaging files, also check the package-specific README files and workflow YAMLs.

## Common onboarding mistakes

- Editing generated outputs instead of source files.
- Making a broad change in `server.py` without reading the matching tests first.
- Changing vendored `src/rlm/**` code for a problem that actually lives in `recursive_context.py`.
- Updating behavior without updating the corresponding docs under `docs/install/`, `docs/reference.md`, or `docs/hooks.md`.
- Treating `apps/mcp/graph-ui/node_modules/**` as repository source.

## Recommended first reads by goal

- “I want to add or change an MCP tool”:
  - `src/waggle/server.py`
  - `tests/test_server.py`
  - `docs/reference.md`

- “I want to improve memory quality”:
  - `src/waggle/graph.py`
  - `src/waggle/retrieval/hybrid.py`
  - `src/waggle/recursive_context.py`
  - `tests/test_graph.py`
  - `tests/test_hybrid_retrieval.py`

- “I want to fix automatic memory not being used”:
  - `src/waggle/orchestrator.py`
  - `src/waggle/chat_runtime.py`
  - `docs/automatic-memory-rules.md`
  - `tests/test_chat_runtime.py`

- “I want to help as a docs contributor”:
  - `README.md`
  - `CONTRIBUTING.md`
  - `docs/install/`
  - `docs/reference.md`
