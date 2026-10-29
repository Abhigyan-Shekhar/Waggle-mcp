# WebMCP with a real repository

Waggle's browser workspace exposes the persistent graph belonging to the
repository configured on the Waggle server. It does not upload or index your
entire repository, and opening the public demo does not grant access to your
local filesystem.

## Connect

Start the installed Waggle build with:

```bash
WAGGLE_WORKSPACE_PATH=/absolute/path/to/repository \
WAGGLE_TRANSPORT=http \
WAGGLE_HTTP_HOST=127.0.0.1 \
waggle-mcp serve
```

Open `http://127.0.0.1:8080/workspace`. The header should show your repository
name and remote, not the seeded sample. The page supplies the active project
scope to brief, recall, propose, and apply. A project selector appears when
multiple registered projects are available. Graph Studio navigation preserves
the selected project.

SQLite HTTP use is loopback-only outside demo mode. Keep `WAGGLE_DB_PATH`
unchanged across restarts to retain the graph and proposals. The project registry
and repository observations live in the existing graph, not a separate catalogue.
Registration uses the tenant's graph and is unavailable to other tenants through
the operator-configured filesystem endpoint.

Identity uses normalized Git `origin` (otherwise the first remote), then Git
root, then canonical workspace path. Equivalent SSH/HTTPS remotes share an ID;
path-based identities change if the directory moves. Registered metadata retains
the current checkout path and a readable project name. Legacy manually named
scopes are not automatically merged with repository-derived IDs.

## Catch up

Ask your browser agent:

```text
Call Waggle's get_project_brief with no arguments. Summarize the connected
repository, distinguishing approved decisions from repository observations.
```

The brief includes purpose, architecture, stack, current authoritative decisions,
constraints, questions, recent changes, and source provenance. README and manifest
claims remain `source_observation`, not human-approved memory. The purpose field
has an explicit authority label and provenance. Recall returns current governed
memory and excludes superseded decisions and unapproved source observations.

Repository input is bounded: small README/manifests, a limited selection of docs,
top-level components, deployment fingerprints, example environment-variable names
(not values), branch, and recent commits. No source code is executed. Refresh
rereads these bounded inputs and hashes observations; unchanged observations are
not inserted again. It is not a full-codebase index or a Git-diff-only scanner.
Possible conflict warnings are heuristic review cues, not verified contradictions.

## Verify the governed flow

Use a disposable local repository/database for this test; do not change a real
project's architecture decision just for a demonstration.

1. Register the repository and create a human-owned **Storage architecture**
   decision using Graph Studio or the existing local Waggle tools. An example
   starting value is **SQLite is the default; Neo4j remains optional.** Use the
   registered project's scope. Registration itself does not invent this decision.
2. Call `get_project_brief` with `{}`. Check repository name and decision.
3. Call `recall_memory` with `{"query":"storage architecture","limit":5}`.
   Save the actual returned memory ID.
4. Call `propose_memory_change` using that memory ID, proposed content
   **Use Neo4j as the default persistence backend.**, and a reason identifying
   this as an explicit test. Omit `project_id` to use the open project.
5. Confirm recall still returns SQLite while the proposal is pending. In
   **Proposals**, read the exact target and replacement before approving.
6. Call `apply_approved_memory_change` with only the actual approved
   `proposal_id`. Do not send replacement text. A second identical application
   is an idempotent result, not another mutation. An unapproved, stale, or
   different-project proposal must not apply.
7. Recall again: Neo4j is now authoritative; SQLite remains in history through
   the native `updates` relationship.
8. Stop and restart Waggle with the same repository and database path. Open a
   new browser/agent session and call `get_project_brief` with `{}`. The new
   authoritative decision must remain, without replaying the old conversation.
9. Change a README claim, then call `refresh_project_context` with `{}`. The
   new observation and source lineage appear without changing governed memory.

Browser permission/security decisions remain the browser's responsibility. A
registration or local HTTP test is not evidence of consumer ChatGPT invocation.
If a browser blocks application, use the existing human apply control or report
the platform issue; do not disguise or bypass the blocked action.

## Validation

`tests/test_webmcp_projects.py` covers identity, registration/reconnection,
refresh/reverts/deletion, tenant/project isolation, authority separation,
filesystem access boundaries, and the complete HTTP correction flow across a
reopened database. Existing governance tests cover review, stale proposals,
idempotency, and immutable application. Frontend unit tests verify default scope
resolution and refresh registration. Neo4j contract/stub tests cover the graph
interface, but are not a substitute for a live Neo4j integration run.

The hosted demo and browser-only `.abhi` session mode keep their existing
behavior. They are separate from persistent repository mode; demo reset never
clears a registered local repository graph.
