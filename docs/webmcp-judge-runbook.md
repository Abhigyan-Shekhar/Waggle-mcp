# Waggle WebMCP Judge Runbook

## Deployment contract

Deploy the repository's `render.yaml` Blueprint from the challenge branch. It
runs the built workspace as a free static site and the existing Docker image as
a separate free ASGI backend. The backend enables `WAGGLE_DEMO_MODE=true`, uses
the deterministic embedding model, and permits credentialed requests only from
the exact frontend origin.

Do not add another frontend origin, a shared Neo4j demo database, an API key, or
a login step. Browser isolation is performed server-side: the opaque
SameSite=None cookie selects a private tenant and physical project while every
public WebMCP payload continues to use the alias `waggle-webmcp`.

The free backend has ephemeral storage. State survives ordinary refreshes while
the instance is running, but may be lost on restart or redeploy; a missing
session workspace reseeds deterministically on its next request.

## Public URL acceptance

Run these checks after Render reports the deploy healthy:

1. Open the HTTPS root URL in a new private browser window.
2. Confirm the workspace loads without setup and shows `Challenge Demo`.
3. Confirm the API response issues an HttpOnly, Secure, SameSite=None
   `waggle_demo_session` cookie.
4. Refresh and confirm the same 25-memory workspace remains.
5. Open another private browser window and confirm it receives a different
   session cookie and workspace IDs.
6. Create or approve a change in window A and confirm it does not appear in B.
7. Select `Reset Demo` in A and confirm A returns to the original fixture while
   B is unchanged.
8. Open `/workspace`, `/graph`, and a built asset on the frontend; each must
   return successfully through the static-site rewrite.
9. Check `/health/live` and `/health/ready` on the API origin.
10. After a backend restart, confirm an absent session workspace reseeds to the
    same deterministic fixture.

## Real ChatGPT acceptance

Use ChatGPT's built-in browser with a model and account configuration that
supports Site tools. Open the public workspace, then select
**Site tools → Available site tools**
in its address bar. If the control is disabled, enable **Settings → Browser →
Permissions → Enable site tools** and reload the page. Do not look for Waggle in
the plugin catalog: WebMCP tools belong to the open page.

After the browser lists the tools, run the exact judge story in ChatGPT:

1. Discover `get_project_brief`, `recall_memory`,
   `propose_memory_change`, `apply_approved_memory_change`, and
   `load_abhi_for_session`. The first four remain the frozen governance flow;
   the fifth accepts an attached `.abhi` payload for browser-session-only use.
2. Call `get_project_brief` with `project_id: waggle-webmcp`.
3. Recall `storage architecture` and confirm the authoritative answer is
   `Use Neo4j as the primary storage engine.`
4. Propose a replacement that makes SQLite the default and keeps Neo4j
   optional; confirm authoritative memory has not changed.
5. Approve or edit-and-approve the proposal in the Waggle workspace.
6. Apply it from ChatGPT using only the proposal ID and public project alias.
7. Recall storage architecture again and confirm only the corrected memory is
   authoritative, with its `updates` lineage preserved.
8. Reset the demo and rerun the opening recall to confirm the exact original
   state is restored.

Record the tested public URL, UTC timestamp, browser, ChatGPT surface, and any
discovery error in `WEBMCP_STATUS.md`. Once the challenge submission closes,
preserve the submitted deployment unchanged and make further work on a separate
branch or service.
