# Consumer ChatGPT WebMCP Acceptance — 2026-08-28

## Frozen condition

- Public workspace: `https://waggle-webmcp.onrender.com/`
- Implementation commit: `ec05e8b19f8f50d2422a716f0600ca5d41da9b9f`
- Surface: consumer ChatGPT Work mode with the built-in browser
- Project alias: `waggle-webmcp`
- Prompts: exact explicit tool-named prompts shipped at the frozen commit
- Human-approved replacement: `Use SQLite by default; Neo4j remains optional.`

The implementation, tool schemas, prompts, fixture, and reset behavior were not
changed between the two runs. Times below are UTC timestamps at which each
result was recorded in the acceptance task.

## Run 1

| Time (UTC) | Boundary | Sealed result |
|---|---|---|
| `16:05:37` | `get_project_brief` | Consumer ChatGPT invoked the Site tool and returned the governed project brief, including the seeded Neo4j storage decision. |
| `16:07:48` | `recall_memory` | Authoritative result: `Use Neo4j as the primary storage engine.` |
| `16:09:20` | `propose_memory_change` | Created pending `proposal_89ce210835d647e7bdcdd15c01fee6ee`; authoritative memory remained unchanged. |
| `16:10:34` | Human review | The user approved the exact SQLite-default replacement in Waggle. |
| `16:11:56` | `apply_approved_memory_change` | Applied using only the proposal ID; created authoritative memory `abfe6957-7850-4cae-b17f-7b647dd2781f`. |
| `16:12:48` | `recall_memory` | Returned only the SQLite-default decision and confirmed that it superseded the prior Neo4j-primary decision. |

## Reset boundary

At `16:17:33` the user supplied a screenshot of the consumer ChatGPT Work flow
confirming `Reset Demo` had been clicked. The reset removed Run 1 proposal and
applied state and restored the deterministic Neo4j-primary seed before Run 2.

## Run 2

| Time (UTC) | Boundary | Sealed result |
|---|---|---|
| `16:19:56` | `get_project_brief` | Consumer ChatGPT invoked the Site tool and returned the restored Neo4j-primary state. |
| `16:21:13` | `recall_memory` | Authoritative result: `Use Neo4j as the primary storage engine.` |
| `16:21:56` | `propose_memory_change` | Created pending `proposal_44683cc04510454790e3c821980b668a`; authoritative memory remained unchanged. |
| `16:22:41` | Human review | The user approved the exact SQLite-default replacement in Waggle. |
| `16:23:27` | `apply_approved_memory_change` | Applied using only the proposal ID; created authoritative memory `0ef83b75-680d-48cc-b719-fd23b172d2ef`. |
| `16:24:15` | `recall_memory` | Returned only the SQLite-default decision and confirmed that it superseded the prior Neo4j-primary decision. |

## Acceptance decision

**PASS.** Consumer ChatGPT selected and invoked all four page-level Waggle Site
tools across two complete governed-memory flows. Both runs preserved the human
approval boundary, proposal-only application contract, authoritative
supersession, and corrected recall. The clean reset produced an independent
second execution with distinct proposal and memory IDs.

This ledger records manually observed consumer responses and the user-confirmed
reset artifact. Direct Codex browser-runtime calls from earlier validation are
kept separate and are not counted as either consumer run.
