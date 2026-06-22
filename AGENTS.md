# Repository Agent Rules

This file contains custom developer rules and agent instructions for the Waggle MCP repository. 

Maintainers can add custom rules and instructions below. The block labeled "Waggle Automatic Memory" is automatically managed by `waggle-mcp` setup commands and should not be edited manually.

## Custom Rules

<!-- waggle:auto-memory:start -->
## Waggle Automatic Memory

Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call prime_context.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call query_graph with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call observe_conversation automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call build_context before answers and on_assistant_turn after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.
<!-- waggle:auto-memory:end -->
