# AGENTS.md

> Agent-oriented project guide for **Waggle-mcp** — persistent graph-structured memory for AI coding agents.

This file provides AI coding agents with the context, commands, conventions, and workflows needed to contribute effectively to this repository. It complements the human-focused `README.md` and `CONTRIBUTING.md`.

---

## Project Overview

Waggle-mcp is an MCP (Model Context Protocol) server that gives LLMs persistent graph-structured memory. It stores decisions, preferences, contradictions, and contextual information across sessions using a local SQLite graph (with optional Neo4j backend) and sentence-transformer embeddings.

**Key components:**

| Component | Path | Description |
|-----------|------|-------------|
| Core MCP server | `src/waggle/` | Python package with graph engine, retrieval, and MCP tool registration |
| RLM integration | `src/rlm/` | Remote Language Model sandbox integration layer |
| Graph Studio UI | `apps/mcp/graph-ui/` | Vite/React frontend for visualizing the memory graph |
| VS Code extension | `apps/vscode-extension/` | One-click workspace integration for VS Code |
| Tests | `tests/` | pytest suite with fixtures under `tests/fixtures/` |
| Documentation | `docs/` | Install guides, reference, architecture docs |
| Deployment | `deploy/` | Docker, infra manifests, and observability helpers |
| Scripts | `scripts/` | Benchmarks, utilities, and verification tools |

---

## Setup Commands

```bash
# Clone and set up local development environment
git clone https://github.com/Abhigyan-Shekhar/Waggle-mcp.git
cd Waggle-mcp
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows PowerShell
python -m pip install --upgrade pip
pip install -e ".[dev]"

# Run tests (uses deterministic embeddings — no ML model download required)
WAGGLE_MODEL=deterministic pytest -q

# Lint and format checks
ruff check src/ tests/
ruff format --check src/ tests/
```

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|--------|
| `WAGGLE_MODEL` | Set to `deterministic` for offline SHA-256 embeddings in tests | `all-MiniLM-L6-v2` |
| `WAGGLE_EMBEDDING_BACKEND` | Controls embedding inference backend (`pytorch` or `onnx`) | `pytorch` |
| `PYTHONUTF8` | Set to `1` on Windows to prevent `UnicodeEncodeError` | unset |

---

## Code Style

- **Language:** Python 3.11+ with strict type annotations encouraged
- **Formatter:** Ruff (line length 120, double quotes, space indentation, LF line endings)
- **Linter:** Ruff with rules: E, W, F, I, UP, B, C4, PIE, SIM, RUF
- **Type checker:** mypy (permissive mode — `ignore_missing_imports = true`)
- **Import ordering:** isort via Ruff; first-party packages are `waggle` and `rlm`
- **Quote style:** Double quotes
- **Semicolons:** Not used
- **Functional patterns:** Preferred where they improve readability

### File Conventions

- All Python source lives under `src/waggle/` or `src/rlm/`
- Test files mirror source structure under `tests/`
- Use `py.typed` marker for PEP 561 compliance
- Static assets for Graph Studio are bundled under `src/waggle/static/graph/`

---

## Testing Instructions

```bash
# Run the full test suite (deterministic mode, no ML model needed)
WAGGLE_MODEL=deterministic pytest -v --tb=long

# Run a specific test file
WAGGLE_MODEL=deterministic pytest tests/test_graph.py -v

# Run a specific test by name
WAGGLE_MODEL=deterministic pytest -k "test_query_graph" -v

# Lint before committing
ruff check src/ tests/
ruff format --check src/ tests/
```

### CI Pipeline

The GitHub Actions CI (`ci.yml`) runs:
1. **Lint** — `ruff check` and `ruff format --check` on `src/` and `tests/`
2. **Test** — pytest across Python 3.11, 3.12, 3.13 on Ubuntu + Windows smoke test
3. **Package** — build sdist/wheel and verify with `twine check`
4. **VS Code extension** — `npm ci && npm run compile` in `apps/vscode-extension/`
5. **Graph Studio** — `npm ci && npm run build` in `apps/mcp/graph-ui/`

All CI checks must pass before a PR can be merged.

---

## PR and Commit Conventions

### Branch Naming

```text
fix/<short-description>      # Bug fixes
feat/<short-description>     # New features
docs/<short-description>     # Documentation changes
test/<short-description>     # Test additions/changes
refactor/<short-description> # Code refactoring
```

### Commit Message Format

```text
<type>(<scope>): <short description>

<optional body explaining why>
```

**Types:** `fix`, `feat`, `docs`, `test`, `refactor`, `ci`, `chore`

### Pull Request Checklist

- Link the issue with `Fixes #<number>` in the PR description
- Run `WAGGLE_MODEL=deterministic pytest -q` and confirm all tests pass
- Run `ruff check src/ tests/` and `ruff format --check src/ tests/`
- Keep PRs focused — one logical change per PR
- Include implementation notes for non-trivial changes
- Add or update tests for code changes

---

## Architecture Notes

### Graph Engine

The core graph is in `src/waggle/graph.py` (SQLite) with an alternative Neo4j backend in `src/waggle/neo4j_graph.py`. Nodes have types (`fact`, `entity`, `concept`, `preference`, `decision`, `question`, `note`) and edges have types (`relates_to`, `contradicts`, `depends_on`, `part_of`, `updates`, `derived_from`, `similar_to`).

### Temporal Validity

Every node supports optional `valid_from`/`valid_to` fields. The `query_graph` tool excludes expired nodes by default. Use `include_invalidated=True` or `as_of=<ISO-8601>` for historical queries.

### Memory Orchestration

- `src/waggle/orchestrator.py` — Automatic memory retrieval and ingestion flow
- `src/waggle/chat_runtime.py` — Runtime turn handling and orchestration wiring
- `src/waggle/recursive_context.py` — Context assembly pipeline

### The `.abhi` Format

Portable memory snapshots using JSON with optional AES-256-GCM encryption, content hash, and magic-bytes header (`WGL\x01`). Validate with `waggle-mcp fsck <file.abhi>`.

---

## Security Considerations

- Never commit credentials, API keys, or personal data
- Do not include local machine paths in committed files
- The `.abhi` format supports encryption — use it for sensitive memory exports
- See `SECURITY.md` for vulnerability reporting procedures
- See `docs/security/` for detailed security documentation

---

## Common Tasks for Agents

### Adding or Changing an MCP Tool

1. Edit `src/waggle/server.py` to register the new tool
2. Update `tests/test_server.py` with coverage
3. Update `docs/reference.md` with the tool's documentation

### Improving Memory/Retrieval Quality

1. Work in `src/waggle/graph.py`, `src/waggle/retrieval/hybrid.py`, or `src/waggle/recursive_context.py`
2. Add tests in `tests/test_graph.py` or `tests/test_hybrid_retrieval.py`
3. Run benchmarks if retrieval quality is affected

### Fixing Automatic Memory Issues

1. Check `src/waggle/orchestrator.py` and `src/waggle/chat_runtime.py`
2. Reference `docs/automatic-memory-rules.md` and `docs/memory-orchestration.md`
3. Update `tests/test_chat_runtime.py`

### Documentation Contributions

1. Edit files under `docs/`, `README.md`, or `CONTRIBUTING.md`
2. For install guides, update the relevant file in `docs/install/`
3. Keep `docs/reference.md` in sync with any tool changes

---

## Dependency Change Rules

- If you change `server.py`, also check `tests/test_server.py`, `tests/test_stdio_integration.py`, and `docs/reference.md`
- If you change `graph.py`, also check `tests/test_graph.py`, `tests/test_edges.py`, and temporal-validity tests
- If you change `orchestrator.py` or `chat_runtime.py`, also check `docs/automatic-memory-rules.md`, `docs/memory-orchestration.md`, and runtime tests
- If you change `.abhi` behavior, also check import/export tests and format documentation
- If you change install or hook behavior, also check `docs/install/**`, `docs/hooks.md`, and integration tests

---

## Waggle Automatic Memory

<!-- waggle:auto-memory:start -->
Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call `prime_context`.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call `query_graph` with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call `observe_conversation` automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call `build_context` before answers and `on_assistant_turn` after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.
<!-- waggle:auto-memory:end -->

---

## Agent Compatibility

This `AGENTS.md` file is compatible with the following AI coding agents and tools:

- [OpenAI Codex](https://github.com/openai/codex)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [Cursor](https://cursor.sh/)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli)
- [Aider](https://aider.chat/)
- [Amp](https://amp.dev/)

### Aider Configuration

To configure Aider to use this file, add to `.aider.conf.yml`:

```yaml
read: AGENTS.md
```

### Gemini CLI Configuration

To configure Gemini CLI, add to `.gemini/settings.json`:

```json
{
  "contextFileName": "AGENTS.md"
}
```

---

## References

- [agents.md specification](https://agents.md/)
- [skills.sh](https://skills.sh/)
- [Waggle-mcp README](./README.md)
- [Contributing Guide](./CONTRIBUTING.md)
- [Repository Map](./docs/repository-map.md)
- [API Reference](./docs/reference.md)
