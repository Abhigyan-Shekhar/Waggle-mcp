# server.json Registry Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Waggle's `server.json` accurate for local-first stdio installs and add CI that proves the registry manifest, Python distribution, installed CLI, and MCP stdio startup are release-ready without publishing.

**Architecture:** Keep GitHub Actions orchestration declarative and put repository-specific validation in a focused Python script with separate schema, project metadata, combined manifest, built-wheel README, and installed-command stdio subcommands. The workflow fetches the released schema named by `server.json`, calls Track 1's metadata checker, builds into runner-temporary storage, installs into a fresh temporary virtual environment, and runs all installed-artifact checks there.

**Tech Stack:** Python 3.11, `jsonschema`, `tomllib`, `zipfile`, MCP Python SDK, PyPA `build`, GitHub Actions.

## Global Constraints

- Branch from local `codex/sync-release-metadata`; do not push or open a PR before Track 1 merges.
- Preserve `server.json` `.name`, `.version`, and `packages[].version` exactly.
- Registry transport remains stdio only; no remote/HTTP manifest entry.
- The job has `contents: read` and never publishes or writes to the repository tree.
- Every database, build output, and virtual environment used by smoke tests lives under a temporary directory.
- Before eventual publication, rebase onto merged `main` and rerun every verification command from scratch.

---

### Task 1: Registry-readiness validation helpers

**Files:**
- Create: `scripts/check_registry_readiness.py`
- Create: `tests/test_registry_readiness.py`

**Interfaces:**
- Produces: `check_schema(manifest_path: Path, schema_path: Path) -> list[str]`, `check_project_metadata(root: Path) -> list[str]`, `check_manifest(root: Path, schema_path: Path) -> list[str]`, `check_wheel(wheel_path: Path, readme_path: Path) -> list[str]`, async `smoke_stdio(command: Path, work_dir: Path) -> list[str]`, and matching CLI subcommands.
- Consumes: `server.json`, `pyproject.toml`, `README.md`, an explicitly downloaded JSON schema, one built wheel, and one installed `waggle-mcp` executable.

- [ ] Write tests that fail because the helper module does not exist. Cover valid metadata; schema errors; package-name drift; missing `waggle-mcp` entry point; wheel description/marker absence; exact README embedding; and stdio initialization using a real temporary executable fixture.
- [ ] Run `WAGGLE_MODEL=deterministic .venv/bin/python -m pytest tests/test_registry_readiness.py -q` and confirm the missing-module failure.
- [ ] Implement only the helper functions and CLI needed by the tests. Aggregate actionable errors and return exit status 1 on validation failure.
- [ ] Re-run the focused tests and confirm they pass.
- [ ] Run Ruff and mypy over the new helper and tests.

### Task 2: Correct the registry manifest

**Files:**
- Modify: `server.json`

**Interfaces:**
- Consumes: released schema `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json` confirmed from the official Registry's `CurrentSchemaVersion` on 2026-08-10.
- Produces: a schema-valid local-first manifest whose PyPI package matches `[project].name` and whose command exists in `[project.scripts]`.

- [ ] Add title `Waggle` and refine the description to persistent, local-first, graph-backed conversational memory.
- [ ] Remove only `WAGGLE_API_KEY_ENVIRONMENT` from `environmentVariables`; retain transport, backend, DB path, default tenant, and model settings.
- [ ] Run the manifest helper against the released schema copy and confirm it passes.
- [ ] Run `python scripts/sync_release_metadata.py --check` and confirm Track 1 metadata still passes.

### Task 3: Least-privilege registry-readiness workflow

**Files:**
- Create: `.github/workflows/registry-readiness.yml`

**Interfaces:**
- Consumes: the Task 1 CLI and Track 1 `scripts/sync_release_metadata.py --check`.
- Produces: a pull-request-only job filtered to release metadata paths, with `contents: read` and no publishing capability.

- [ ] Add triggers for `server.json`, `pyproject.toml`, `README.md`, `scripts/sync_release_metadata.py`, `scripts/check_registry_readiness.py`, and the workflow itself.
- [ ] Set up Python 3.11, install `build` plus `jsonschema`, fetch the exact schema URI declared by the manifest into `${RUNNER_TEMP}`, and run the manifest helper.
- [ ] Run Track 1 drift checking, build wheel/sdist under `${RUNNER_TEMP}`, and run wheel README inspection.
- [ ] Create `${RUNNER_TEMP}/registry-venv`, install the built wheel, run deterministic/offline `waggle-mcp doctor` with a temporary SQLite path, and run the stdio helper against that installed command.
- [ ] Validate YAML syntax locally and audit the workflow for `contents: read`, temporary-only paths, bounded network use, and absence of publish commands.

### Task 4: Local dry run and branch handoff

**Files:**
- Modify only if verification exposes a Track 2 defect.

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: fresh command output, clean Track 2 commits, and a PR description that includes the known broken live PyPI listing plus the rebase/reverification gate.

- [ ] Run focused tests, the complete offline deterministic suite, Ruff, formatting, mypy, and Track 1 drift checking.
- [ ] Download the released schema anew and run manifest validation.
- [ ] Build wheel/sdist into a fresh temporary directory; inspect the wheel; create a fresh virtual environment; install the wheel; run offline doctor; and perform the stdio smoke test.
- [ ] Review `git diff` against `codex/sync-release-metadata`, confirm no Track 1 files were unintentionally changed, and commit Track 2 changes only.
- [ ] Prepare—but do not publish—the branch name, PR title, and PR description. Record that after Track 1 merges this branch must be rebased onto `main` and the entire verification sequence rerun before push/PR.
