<!-- waggle:auto-memory:start -->

## Waggle Automatic Memory

Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call prime_context.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call query_graph with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call observe_conversation automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call build_context before answers and on_assistant_turn after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.

<!-- waggle:auto-memory:end -->

## Repository

This is the **Waggle** monorepo — a Python 3.11+ MCP server for persistent graph-backed conversational memory.

### Quick setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### Commands

| Scope         | Command                                                  | Notes                                                   |
| ------------- | -------------------------------------------------------- | ------------------------------------------------------- |
| Lint          | `ruff check src/ tests/`                                 |                                                         |
| Format check  | `ruff format --check src/ tests/`                        |                                                         |
| Tests (PowerShell) | `$env:WAGGLE_MODEL="deterministic"; pytest -q`      | **Required** env var avoids 420 MB model download       |
| Tests (Bash/Linux) | `WAGGLE_MODEL=deterministic pytest -q`              | POSIX equivalent for CI/Linux environments              |
| Verbose tests | `$env:WAGGLE_MODEL="deterministic"; pytest -v --tb=long`      |                                                         |
| Typecheck     | `mypy src/`                                              |                                                         |
| Pre-commit    | `pre-commit run --all-files`                             | Runs ruff lint → ruff-format, trailing-whitespace, etc. |

**Pre-commit order matters:** ruff lint runs before ruff-format.

### Project structure

- `src/waggle/` — main package; entrypoint: `waggle.server:main` → CLI commands `waggle-mcp` / `waggle`
- `src/rlm/` — vendored support code; avoid editing unless specifically needed
- `apps/vscode-extension/` — VS Code extension (TypeScript, `npm run compile`)
- `apps/mcp/graph-ui/` — Graph Studio frontend (React/Vite, `npm run build`)
- `apps/cli/` — CLI tooling
- `tests/` — pytest test suite; conftest.py inserts `src/` into sys.path
- `docs/` — narrative docs; `docs/repository-map.md` has full file-level map

### Environment variables (all `WAGGLE_*`)

Parsed by `waggle.config.AppConfig.from_env()`. Booleans enabled only by lowercase `"true"`.

| Variable           | Default               | Notes                                        |
| ------------------ | --------------------- | -------------------------------------------- |
| `WAGGLE_MODEL`     | `all-MiniLM-L6-v2`    | Use `deterministic` for offline/fast testing |
| `WAGGLE_BACKEND`   | `sqlite`              | Or `neo4j` for remote deployment             |
| `WAGGLE_TRANSPORT` | `stdio`               | Or `http` (requires `WAGGLE_BACKEND=neo4j`)  |
| `WAGGLE_DB_PATH`   | `~/.waggle/waggle.db` | Also auto-discovers Codex config             |
| `WAGGLE_LOG_LEVEL` | `INFO`                |                                              |

Full reference: `docs/environment-variables.md`

### Testing quirks

- **Always** set `WAGGLE_MODEL=deterministic` or tests will download ~420 MB model
- On Windows, set `PYTHONUTF8=1` to avoid `UnicodeEncodeError` from emoji
- Tests use SQLite in-memory by default; no external services needed

### Key gotchas

- `src/waggle/server.py` (~6400 lines) is high-blast-radius; changes there can break many tools
- Edges are what make graph memory work — `store_node` without `store_edge` produces disconnected nodes
- `.abhi` is the portable memory format (JSON with magic header `WGL\x01` and optional AES-256-GCM)
- Root directory is reserved for packaging, deployment, and registry manifests; feature code goes under `src/`, `apps/`, `docs/`, or `tests/`
- Extra dependency groups: `[dev]`, `[neo4j]`, `[rlm-ipython]`, `[rlm-modal]`, `[rlm-e2b]`, `[rlm-daytona]`, `[rlm-prime]`

### Installed Skills

Skills live in `.agents/skills/`. Invoke via `/skill-name` in Claude Code.

| Skill                             | How used in this project                                                                                                      |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `conventional-commit`             | Enforce Conventional Commits spec on all commits to `src/waggle/`, `src/rlm/`, `apps/`                                        |
| `python-mcp-server-generator`     | Scaffold new MCP tools/resources inside `src/waggle/server.py`; bootstrap sibling MCP servers                                 |
| `python-design-patterns`          | Guide KISS / SRP / composition choices when extending `server.py` (6400-line high-blast-radius file) or splitting God classes |
| `python-testing-patterns`         | Extend `tests/` with pytest fixtures, parameterization, and mocking patterns; keep `WAGGLE_MODEL=deterministic` in mind       |
| `python-performance-optimization` | Profile SQLite graph queries, embedding lookups, and in-memory operations in the hot path                                     |
| `rate-limiting-implementation`    | Add per-client / per-tool rate limits to MCP tool handlers; protect against runaway agent loops                               |
| `prometheus-configuration`        | Expose `/metrics` endpoint for MCP server process; define scrape config for self-hosted deployments                           |
| `grafana-dashboards`              | Build dashboards for Waggle memory health (node/edge counts, query latency, tool call rates) using Prometheus data            |
| `docker-expert`                   | Harden `Dockerfile` for the MCP server image; multi-stage build to keep image slim                                            |
| `k8s-security-policies`           | Apply NetworkPolicy + RBAC if Waggle runs in a cluster alongside Neo4j (`WAGGLE_BACKEND=neo4j`)                               |
| `vercel-react-best-practices`     | Optimize `apps/mcp/graph-ui/` (React/Vite Graph Studio); applies if bundle size or render perf becomes an issue               |
| `vercel-composition-patterns`     | Refactor Graph Studio components if boolean-prop proliferation appears in the React UI layer                                  |
