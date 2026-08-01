# Claude Self-Hosted Connector Submission

This is the Claude web/mobile submission and readiness dossier for Waggle as a self-hosted remote MCP connector.

Status: self-hosting documentation is ready in-repo. Public Claude directory submission depends on Anthropic accepting a connector where each user supplies their own HTTPS MCP URL, or on a future centrally hosted Waggle service.

## Official Submission Path

Remote MCP servers, including MCP Apps, are submitted through the Claude.ai submission portal in an organization's admin settings:

- Portal: https://claude.ai/settings/admin/software/submissions
- Submission docs: https://claude.com/docs/connectors/building/submission
- Pre-submission checklist: https://claude.com/docs/connectors/building/review-criteria
- Authentication docs: https://claude.com/docs/connectors/building/authentication
- Testing docs: https://claude.com/docs/connectors/building/testing

## Positioning

Server name:

Waggle

Tagline:

Persistent graph memory for AI agents

Description:

Waggle gives Claude persistent, graph-backed memory across sessions, projects, and MCP-compatible agent clients. It stores durable decisions, preferences, requirements, corrections, and project facts as connected memory nodes with evidence, timestamps, and temporal validity.

For Claude Code and Claude Desktop, Waggle runs locally over stdio and keeps memory on the user's machine. For Claude web and mobile, the user can self-host the remote MCP endpoint by running Waggle locally in HTTP mode and exposing `/mcp` through a user-owned HTTPS tunnel or reverse proxy.

Suggested categories:

- Productivity
- Developer tools
- Knowledge management
- AI agents

Documentation URL:

https://github.com/Abhigyan-Shekhar/Waggle-mcp/blob/main/docs/claude-self-hosted-connector.md

Privacy policy URL:

https://github.com/Abhigyan-Shekhar/Waggle-mcp/blob/main/PRIVACY.md

Icon:

Use `assets/waggle-icon.png` if published in the repository release assets, or upload the equivalent square PNG through the portal.

## Connection Model

Connector type:

Remote MCP server, self-hosted by each user.

Transport:

Streamable HTTP.

Server URL:

`https://<user-owned-tunnel-domain>/mcp`

URL mode:

Each user provides their own URL. There is no centrally operated Waggle endpoint in the recommended model.

Runtime shape:

- `WAGGLE_TRANSPORT=http`
- `WAGGLE_BACKEND=sqlite` for single-user self-hosting
- `WAGGLE_BACKEND=neo4j` for heavier team or production deployments
- User-owned HTTPS tunnel or reverse proxy
- `/mcp` routed to Waggle's Streamable HTTP MCP app
- `/health/live` and `/health/ready` exposed for health checks
- API keys never passed in URL query parameters

## Authentication

Preferred self-hosted path:

API-key authentication using either `Authorization: Bearer <generated-key>` or `X-API-Key: <generated-key>`.

Why:

Waggle is local-first and authenticates remote HTTP MCP requests with API keys. Bearer auth matches Claude's Messages API MCP connector `authorization_token` field. `X-API-Key` remains available for clients that support custom static headers.

OAuth:

OAuth 2.0 is still the right fit for a future centrally hosted Waggle service. That would require an OAuth issuer, registered redirect URI `https://claude.ai/api/mcp/auth_callback`, token scopes, and protected resource metadata. It is intentionally not required for the self-hosted path.

## User Prerequisites

For Claude Code/Desktop:

- Local Waggle installation.
- Claude MCP stdio config.
- No public HTTPS endpoint.

For Claude web/mobile:

- Local Waggle installation.
- Local Waggle HTTP server.
- User-owned HTTPS tunnel or reverse proxy.
- Generated Waggle API key configured as `Authorization: Bearer` or `X-API-Key`.
- Claude account/org support for custom remote MCP connectors and a compatible auth mode.

## Data Handling

Underlying API:

First-party Waggle MCP server running on the user's own machine or self-managed infrastructure.

Data processed:

- User-provided conversation text sent to Waggle tools.
- Extracted memory nodes and edges.
- Project, agent, tenant, and session scope metadata.
- Timestamps, evidence records, and temporal validity fields.
- Exported/imported `.abhi` bundles when users invoke those tools.

Data storage:

Self-hosted web/mobile deployments store memory in the user's configured local or self-managed backend. SQLite is the simplest single-user path; Neo4j is available for heavier team or production deployments. Claude Code/Desktop deployments store memory in SQLite by default.

Conversation data collection:

Waggle only stores conversation content explicitly passed to memory-ingestion tools such as `observe_conversation` or transcript handoff/import flows. It does not query Claude's private chat history directly.

Retention and deletion:

Retention is controlled by the user through their local database, backups, and Waggle deletion tools. Users can delete scoped memory through explicit clear/delete operations or by removing their local/self-managed database and backups.

## Tool Annotation Status

Claude requires every tool to include:

- `title`
- `readOnlyHint: true` for read-only tools
- `destructiveHint: true` for tools that modify or delete data

Waggle's MCP tool catalog and adapters expose these annotations from `WaggleToolDefinition`. The regression test in `tests/test_mcp_tool_surface.py` verifies every tool has Claude directory annotations and that tool names are 64 characters or fewer.

## Test Instructions

Reviewer self-hosted setup:

1. Follow `docs/claude-self-hosted-connector.md`.
2. Start Waggle locally in HTTP mode.
3. Expose `/mcp` through a user-owned HTTPS tunnel.
4. Create a Waggle API key.
5. Add the tunnel URL as a Claude custom remote MCP connector.
6. Configure auth using bearer token or static header support available in that Claude surface.

Test prompts:

1. "Ask Waggle what decisions are remembered for this project."
2. "Prime this conversation from Waggle memory."
3. "Remember that the reviewer prefers concise implementation notes."
4. "Show what changed recently in Waggle memory."
5. "List any unresolved memory conflicts."
6. "Export a read-only context bundle for this project."

Expected behavior:

- Read-only tools complete without destructive confirmation.
- Write/destructive tools are clearly labeled by Claude.
- Invalid inputs return actionable validation errors.
- Responses are bounded and do not dump the full graph unless explicitly requested.
- Invalid or revoked API keys return a clear auth error.

## Readiness Checklist

- [x] Document user-owned self-hosting model.
- [x] Add MCP tool annotations: `title`, `readOnlyHint`, `destructiveHint`.
- [x] Verify all tool names are 64 characters or fewer in tests.
- [x] Draft self-hosting documentation.
- [x] Use self-hosted auth mode: Waggle API key via `Authorization: Bearer` or `X-API-Key`.
- [ ] Confirm Claude directory accepts per-user remote MCP URLs for a listed connector.
- [ ] Confirm static-header custom connector availability for target Claude plans/orgs.
- [ ] Publish self-hosting documentation at a public URL.
- [ ] Run MCP Inspector against every tool.
- [ ] Test as a custom connector in Claude web/mobile.
- [ ] Prepare reviewer self-hosted endpoint/API key or reproducible self-hosting test instructions.

## Current Blockers

1. Anthropic may not accept a public directory listing that requires each user to provide their own HTTPS MCP URL.
2. Claude web/mobile custom connector auth mode availability depends on the user's plan or organization; Claude's Messages API MCP connector supports bearer tokens through `authorization_token`.
3. Reviewer endpoint/API key or reproducible self-hosting test instructions still need to be prepared.
