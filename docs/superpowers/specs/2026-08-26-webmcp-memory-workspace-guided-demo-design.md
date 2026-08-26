# Waggle WebMCP Memory Workspace + Guided Demo Design

**Date:** 2026-08-26  
**Status:** Approved visual direction — generated Option 2, “Shared Truth Canvas”  
**Scope:** Coordinated P0 milestone covering the memory-first homepage and six-step Guided Demo

## Product outcome

A judge should understand within ten seconds that Waggle is shared project memory that ChatGPT can read and propose changes to, while humans control what becomes authoritative. The workspace remains the product; the guide is a persistent instructor that orchestrates real WebMCP interactions without simulating tool calls.

## Visual target

Preserve the current Waggle identity: warm off-white surfaces, forest-green accents, restrained borders and shadows, serif display headlines, compact sans-serif interface copy, and the existing sidebar shell. Use the selected “Shared Truth Canvas” layout:

- concise thesis and primary **See Waggle in Action** action;
- Current Context and Key Decisions as the dominant first viewport content;
- focused live Memory Map immediately below them;
- Recent Memories and Pending Human Review as lightweight rows;
- counts only as secondary metadata;
- persistent Guided Demo rail on the right when active.

Use a real icon package for navigation and controls. Do not use Unicode symbols, handcrafted SVGs, emoji, fake charts, or mocked graph data.

## Homepage information hierarchy

1. The page thesis is **“Shared project memory, governed by humans.”** Supporting copy states that ChatGPT operates on the same governed memory visible in the workspace.
2. **Current Context** uses the real project brief goal and current-state memories. It is the main content block.
3. **Key Decisions** shows up to five authoritative decision memories, including the current storage architecture.
4. **Live Memory Map** is a compact, readable subgraph derived from the current `snapshot.nodes` and `snapshot.edges`. It prioritizes the storage decision and directly connected nodes, then fills remaining slots with high-value authoritative memories. The preview never fabricates nodes or relationships.
5. **Recent Memories** shows recently updated authoritative memory records.
6. **Pending Human Review** shows pending proposals, or an educational empty state explaining that agents can propose but only humans approve.
7. The footer metadata may show values such as `24 authoritative memories · 5 decisions`, but totals never dominate the page.
8. **Explore full graph →** opens Graph Studio with the current project. When a relevant changed memory is known, the link also carries a focus identifier.

## Guided Demo shell

The guide is a persistent right rail on desktop and a bottom sheet on mobile. Starting the guide:

1. calls the real demo reset endpoint;
2. reloads the deterministic fixture;
3. writes demo progress to project-scoped `sessionStorage`;
4. returns to `/workspace` and opens Step 1.

Normal workspace navigation does not dismiss or reset the guide. **Exit demo** removes active guide state without resetting project data. **Restart demo** resets the fixture and starts again at Step 1.

The guide has no manual “Next” action for ChatGPT steps. It shows a waiting state and advances only when the corresponding real WebMCP callback or human review event occurs. Copy Prompt uses the Clipboard API and provides an accessible success message.

## Six-step state machine

| Step | Required event | Prompt / human action | Focus after success |
|---|---|---|---|
| 1. Project brief | `webmcp.project_brief.read` | “Catch me up on this project using Waggle.” | Current Context |
| 2. Authoritative recall | `webmcp.memory.recalled` with a returned storage memory | “What did we decide about the storage architecture?” | Storage decision |
| 3. Propose correction | `proposal.created` | “That conflicts with our local-first requirement. Propose a better memory, but don't change anything directly.” | New proposal card |
| 4. Human Edit & Approve | `proposal.edited_and_approved` | Edit to “Use SQLite by default; Neo4j remains optional.” | Frozen approved payload |
| 5. Apply approval | `proposal.applied` | “Apply the memory change I approved.” | Previous → Authoritative transformation |
| 6. Confirm recall | `webmcp.memory.recalled` returning the applied memory | “What storage architecture did we decide on?” | Corrected authoritative memory |

The completion state explains that ChatGPT retrieved the exact human-approved truth and offers **Explore lineage in Graph Studio →**.

## Event and persistence model

- `demo-state.js` owns the pure state machine, storage schema, and event matching.
- `GuidedDemo.jsx` renders the rail/sheet and emits start, exit, restart, and copy actions.
- `Workspace.jsx` remains the integration boundary for WebMCP registration, API-backed workspace state, navigation, and human review.
- Each WebMCP callback dispatches a normalized guide event containing real result identifiers. Human review dispatches only after the review API succeeds.
- The persisted state stores the current step, active/completed status, relevant proposal/memory identifiers, and started timestamp. It does not store authoritative memory payloads as a substitute for server state.
- Reload restores guide position, then reconciles it against real proposals and graph data so stale client state cannot claim a completed server transition.

## Highlighting and navigation

- Step 1 highlights Current Context.
- Step 2 highlights the authoritative storage decision without navigating away from the homepage.
- Step 3 navigates to `/workspace/proposals` and scrolls to the real proposal ID.
- Step 4 keeps that proposal visible and highlights the immutable approved value.
- Step 5 highlights the Previous → Authoritative transformation.
- Step 6 returns to the relevant corrected memory and exposes the focused Graph Studio link.
- Highlights are temporary visual emphasis with accessible labels; they do not change the underlying data.

## Responsive behavior

- Desktop at 1280px and wider: 244px sidebar, flexible workspace, 318–340px guide rail.
- Laptop: sidebar may compact, while context and decisions remain two columns where readable.
- Mobile: navigation becomes compact, homepage sections stack, graph preview remains horizontally contained, and the guide becomes a bottom sheet with an explicit expand/collapse control.
- Proposal edit and approval actions remain reachable without horizontal scrolling.

## Loading, connection, and trust copy

- Initial backend wake-up shows **“Connecting to Waggle…”** and does not surface a frightening raw error.
- Challenge Demo and Human controlled remain visible.
- Hosted copy states that this is an isolated, seeded judge workspace. It does not imply that the hosted Render instance itself is local-first storage.
- Approved proposals say **“Ask ChatGPT: Apply the change I approved.”** and include Copy Prompt.

## Acceptance criteria

- Homepage sections and graph preview are derived from the live workspace response.
- Starting or restarting the guide performs a real fixture reset.
- All ChatGPT steps advance only from actual registered WebMCP tool callbacks.
- Human approval advances only after a successful edited approval response.
- Demo state survives normal navigation and reload in the same tab.
- Proposal creation, approval, application, and final recall visibly update the same workspace.
- The selected Option 2 hierarchy is recognizable at 1440×1024 and remains usable at laptop and mobile widths.
- Existing WebMCP, proposal governance, memory, and activity behaviors continue to pass.

## Explicitly out of scope

Authentication, OAuth, billing, multi-tenant SaaS migration, device sync, browser extensions, retrieval redesign, embedding work, Waggle core redesign, mobile applications, RBAC, and deployment replacement.
