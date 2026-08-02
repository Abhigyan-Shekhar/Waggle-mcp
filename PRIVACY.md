# Privacy Policy

Waggle is a local-first MCP memory layer for AI coding agents.

## What Waggle Stores

By default, Waggle stores memory on the user's machine in SQLite. Stored data can
include conversation transcript text, extracted decisions, preferences,
constraints, graph nodes and edges, timestamps, and exported `.abhi` memory
archives.

## Network Use

The default local Waggle runtime does not require a cloud account, hosted
backend, or API key. Memory does not leave the user's machine unless the user
explicitly exports, shares, syncs, or uploads it.

Anonymous telemetry is disabled by default. If a user explicitly enables it,
Waggle may send a small allowlisted set of product events such as setup
completion, server startup, successful memory storage, successful memory
retrieval, demo completion, export completion, and safe failure categories. This
telemetry uses a random installation UUID and does not include conversations,
prompts, memory text, source code, file paths, repository names, project names,
tenant names, raw exception messages, or stack traces.

Users can inspect, enable, or disable telemetry with:

```bash
waggle-mcp telemetry status
waggle-mcp telemetry show
waggle-mcp telemetry enable
waggle-mcp telemetry disable
```

See `docs/telemetry.md` for the exact event schema and privacy controls.

If a user configures optional integrations or sharing workflows, those
integrations may transmit the data the user chooses to export or sync.

Self-hosted remote Waggle connector deployments process data sent to the remote
MCP server by the connected client. Self-hosted deployments may store
conversation text, extracted memory nodes and edges, scope metadata, evidence
records, timestamps, and imported or exported memory artifacts in the backend
configured by the user or organization operating that server.

## User Control

Users control the local database path, exported `.abhi` archives, and any files
they choose to share. Users can delete local Waggle data by removing the
configured Waggle database and export files from their machine.

For self-hosted remote deployments, users or organization administrators control
the database, backups, tunnel, and authorized destructive memory tools. Any
centrally hosted Waggle service would need a separate published retention,
backup, and account deletion policy before public launch.

## Third-Party Sharing

Waggle does not sell user memory data. Self-hosted connector operators control
their own deployment and should not share memory with third parties except as
required to operate their deployment, comply with law, or fulfill user-directed
export or sync workflows.

## Security Notes

Waggle includes export-time checks for likely secrets, but these checks are
heuristic and are not a complete data-loss-prevention system. Users should
review exported memory archives before sharing them.

Security reports should follow the process in `SECURITY.md`.

## Contact

For privacy or support questions, open an issue at:
https://github.com/Abhigyan-Shekhar/Waggle-mcp/issues
