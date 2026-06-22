# Automatic Memory Rules

Use this rule text in MCP clients that support agent instructions, user rules, or custom instructions.

This is the recommended setup for:
- Codex global/project instructions
- Antigravity User Rules

`waggle-mcp setup --yes` and `waggle-mcp init` write this rule into a managed `AGENTS.md` block automatically when run from a Codex workspace.

## Managed Block in AGENTS.md

The automatic memory rules are placed inside `AGENTS.md` using a dedicated block structured as follows:

```markdown
<!-- waggle:auto-memory:start -->
## Waggle Automatic Memory

Use Waggle automatically for conversational memory.
...
<!-- waggle:auto-memory:end -->
```

### Purpose of the Block
AI coding assistants such as Codex and Antigravity process instructions specified in `AGENTS.md` automatically. By embedding these rules directly in the repository's configuration files, contributors and team members don't need to manually configure agent memory rules in their personal client configurations.

### Customization Guidelines for Maintainers
* **Do Not Modify Inside the Block**: The content inside the `<!-- waggle:auto-memory:start -->` and `<!-- waggle:auto-memory:end -->` comment markers is strictly managed by `waggle-mcp`. Any edits within these boundaries will be overridden during subsequent setups or initialization calls.
* **Adding Custom Repository Rules**: Maintainers can safely add custom developer guidelines, style guidelines, project architecture overview, or other prompt rules *outside* the managed block. Waggle's automatic management leaves anything above or below the delimiters untouched.

## Rule Text


```text
Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call prime_context.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call query_graph with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call observe_conversation automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call build_context before answers and on_assistant_turn after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.
```

## Why This Exists

Registering Waggle in `mcp_config.json` or `config.toml` only makes the tools available.
It does not force the client to use them automatically.

For generic MCP clients today, portable automatic memory requires both:
- MCP server registration
- agent-level instructions telling the model when to call Waggle

## Important Notes

- Use the same `WAGGLE_DB_PATH` across clients if you want Codex and Antigravity to share one local memory graph.
- Keep scope stable across sessions, especially `project`, or recall will look empty even when memory exists.
- Live memory should be selective and low-overhead: durable turns are ingested automatically, while low-value chatter is skipped.
- `ingest-transcript-handoff` is for rollover/session-handoff ingestion and export.
- For explicit session/app switching, use a scoped `.abhi` checkpoint with `waggle-mcp checkpoint-context` or `waggle-mcp commit`.
- Resume precedence is DB-first, `.abhi` second:
  - same machine / same `WAGGLE_DB_PATH`: recall from SQLite
  - cross-session or cross-app on the same machine: use stable scopes and the shared DB
  - different machine or explicit handoff: `pull` / `import` the `.abhi`
- Normal live conversational memory still depends on automatic `prime_context`, `query_graph`, and `observe_conversation` usage during chats.
