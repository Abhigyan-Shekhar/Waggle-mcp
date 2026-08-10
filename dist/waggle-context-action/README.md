# Waggle Context Handoff

*convert a GitHub issue, PR, discussion, release, or manually supplied workflow context into a portable Waggle memory checkpoint and a compact Markdown context handoff for downstream AI workflows.*

The Action runs entirely inside the GitHub Actions runner. It uses deterministic local embeddings, does not call an external LLM, does not contact a hosted Waggle service, and does not require a Waggle-hosted account. It does not commit to the consuming repository, does not comment on issues or pull requests, and does not execute code from event payloads.

## Quick start

This directory is a complete Action repository distribution. Until it is extracted and published, its examples use `./` to invoke the local Action. In a consuming repository, replace that with the public repository plus an immutable commit SHA:

```yaml
name: Build repository context
on:
  issues:
    types: [opened, edited]

permissions:
  contents: read

jobs:
  context:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
      - id: waggle
        uses: YOUR_ORG/waggle-context-handoff@REPLACE_WITH_40_CHARACTER_COMMIT_SHA
      - name: Consume the handoff by file path
        env:
          WAGGLE_CONTEXT_FILE: ${{ steps.waggle.outputs.context-file }}
        run: your-agent --context-file "$WAGGLE_CONTEXT_FILE"
```

The event body is read from `GITHUB_EVENT_PATH` by Python. It is never interpolated into a command. The `scope` input is a Waggle project namespace: its effective default is `GITHUB_REPOSITORY`, and the runner invokes `waggle-mcp` with `--project <scope> --scope project`. It is never mapped to a session ID.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `event-path` | effective `GITHUB_EVENT_PATH` | JSON event file; set explicitly for manual or standalone use. |
| `checkpoint` | empty | Existing `.abhi` checkpoint to import before the new event. |
| `scope` | effective `GITHUB_REPOSITORY` | Accumulating Waggle project namespace. |
| `output-directory` | `.waggle-output` | Destination inside `GITHUB_WORKSPACE`. |
| `waggle-version` | `0.1.25` | Exact normalized `waggle-mcp` package version. Confirm this release contains `ingest-github-event` before publishing. |
| `upload-artifact` | `true` | Upload both generated files for seven days. Accepts only `true` or `false`, case-insensitively. |
| `write-step-summary` | `true` | Write safe metadata and counts to the job summary. Accepts only `true` or `false`, case-insensitively. |

## Outputs

| Output | Meaning |
| --- | --- |
| `context-file` | Absolute path to the non-empty Markdown handoff. |
| `checkpoint-file` | Absolute path to the portable `.abhi` memory checkpoint. |
| `nodes-added` | Graph nodes added by this event. |
| `edges-added` | Graph edges added by this event. |

## Supported context

GitHub issue, pull request, discussion, release, and push payloads are normalized with source URL, repository, event type, actor, timestamps, and identifiers retained as provenance. `workflow_dispatch` maps to bounded generic JSON. Unsupported events return a valid scoped checkpoint and a clear `unsupported` status with zero additions. Malformed supported payloads fail without replacing existing outputs.

Supplying `checkpoint` imports earlier memory before ingestion, so repeated workflows can accumulate repository context. IDs are derived deterministically from stable GitHub provenance; reprocessing the same event updates the same graph objects instead of duplicating them.

## Standalone CLI

The main Waggle package exposes the same operation without GitHub Actions:

```bash
waggle-mcp ingest-github-event \
  --event-path event.json \
  --event-type issue \
  --repository octo/demo \
  --project octo/demo \
  --scope project \
  --output-context context.md \
  --output-checkpoint memory.abhi
```

The CLI supports the existing export modes `all`, `project`, `session`, and `since-date`. Project and session modes require `--project`; session mode additionally requires `--session-id`.

## Security and permissions

The default and recommended workflow permission is:

```yaml
permissions:
  contents: read
```

No example needs additional GitHub permissions. If a downstream AI provider needs a token, give it only to that later step; the Waggle step needs none. Event text is untrusted data. The runner uses argument arrays with `shell=False`, bounded JSON parsing, unique private temporary storage, exact package-version validation, and output containment checks. It suppresses captured child output on failure to avoid reflecting secrets.

See [SECURITY.md](SECURITY.md) for the threat model and reporting process, [examples](examples/) for complete workflows, and [MARKETPLACE_RELEASE_CHECKLIST.md](MARKETPLACE_RELEASE_CHECKLIST.md) for human-only publication steps.

## Extract into its own public repository

From a clean checkout of the Waggle main repository:

```bash
mkdir ../waggle-context-handoff
cp -R dist/waggle-context-action/. ../waggle-context-handoff/
cd ../waggle-context-handoff
git init
git add .
git commit -m "feat: publish Waggle Context Handoff action"
```

The owner must then review every file, confirm `action.yml` is at the new repository root, confirm the pinned Waggle version exists and contains this CLI, follow the release checklist, and choose where to push. Nothing in this distribution creates a repository, publishes Marketplace metadata, accepts agreements, or pushes commits.

## Development

Use Python 3.11 or newer with Waggle installed editable, then run:

```bash
python -m pytest tests -q
ruff check scripts tests
mypy scripts/run_action.py
bash -n tests/assert_no_secret.sh
```

Licensed under Apache-2.0. See [LICENSE](LICENSE).
