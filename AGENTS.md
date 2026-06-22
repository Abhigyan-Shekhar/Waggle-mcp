# Agent Workflows, Conventions, and Project Guidance

This file establishes the standardized configuration, behavioral rules, and skill setups for all AI coding agents operating within the Waggle-mcp ecosystem.

## 🤖 Core Principles for AI Agents
1. **Context-Driven Implementation:** Always read the corresponding documentation folder (`/docs/`) before writing code patterns to ensure architecture consistency.
2. **Explicit Code Contrast:** Follow the exact patterns shown in the standards. If updating existing logic, preserve multi-tenant context layers and transaction safety flags.
3. **No Blind Code Exploration:** Rely on the active structural references updated by the doc-sync automation rather than attempting to guess hidden repo paths.

## 🛠️ Relevant Agent Skills Setup
To configure your local environment and synchronize skills uniformly across platforms (e.g., Claude Code, Codex, Cursor), initialize the project-specific agent skills.

```bash
# Initialize and link standard project skill boundaries
./setup-agent-skills.sh --project waggle-mcp