# Waggle

Portable, local project memory for coding agents, packaged for Codex.

This bundle installs the Waggle MCP server with a local-first memory graph for
recalling project decisions, constraints, preferences, and meaningful outcomes
across sessions.

The bundled runtime runs locally through Codex stdio MCP with SQLite storage by
default. The bundled skills teach Codex to retrieve relevant project history
and checkpoint only durable outcomes. Explicit workflows are available as
`$waggle-prime`, `$waggle-recall`, `$waggle-checkpoint`, and `$waggle-memory`.
The compact plugin runtime uses Waggle's deterministic offline embedding mode
so startup does not download a model.

The plugin does not require a hosted Waggle backend, Python installation,
Waggle account, API key, Apple Developer ID notarization, or Windows
Authenticode signing. macOS Gatekeeper or Windows SmartScreen may show a
first-run warning because the current release is intentionally unsigned.

Repository: https://github.com/Abhigyan-Shekhar/Waggle-mcp
