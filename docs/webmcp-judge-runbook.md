# Waggle WebMCP Judge Runbook

## Deployment contract

Deploy the repository's `render.yaml` Blueprint from the challenge branch. It
runs the existing Docker image as one public ASGI service, stores SQLite state
on `/data`, and enables `WAGGLE_DEMO_MODE=true` with secure cookies.

Do not add a second frontend origin, a shared Neo4j demo database, an API key,
or a login step. Browser isolation is performed server-side: the opaque cookie
selects a private tenant and physical project while every public WebMCP payload
continues to use the alias `waggle-webmcp`.

## Public URL acceptance

Run these checks after Render reports the deploy healthy:

1. Open the HTTPS root URL in a new private browser window.
2. Confirm the workspace loads without setup and shows `Challenge Demo`.
3. Confirm the browser receives an HttpOnly, Secure, SameSite=Lax
   `waggle_demo_session` cookie.
4. Refresh and confirm the same 25-memory workspace remains.
5. Open another private browser window and confirm it receives a different
   session cookie and workspace IDs.
6. Create or approve a change in window A and confirm it does not appear in B.
7. Select `Reset Demo` in A and confirm A returns to the original fixture while
   B is unchanged.
8. Open `/workspace`, `/graph`, and a built asset directly; each must return
   successfully from the same origin.
9. Check `/health/live` and `/health/ready` return healthy responses.
10. Restart the service and confirm the persistent disk preserves existing
    browser workspaces. A session whose data is absent must reseed automatically.

## Real ChatGPT acceptance

From the public workspace URL, run the exact judge story in ChatGPT:

1. Discover `get_project_brief`, `recall_memory`,
   `propose_memory_change`, and `apply_approved_memory_change`.
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
