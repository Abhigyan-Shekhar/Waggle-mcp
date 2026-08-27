# Waggle WebMCP Memory Workspace + Guided Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the selected memory-first Waggle homepage and a persistent six-step Guided Demo driven exclusively by real WebMCP and human-review events.

**Architecture:** Extract a pure demo state machine and focused presentation components from the existing `Workspace.jsx`, while keeping that component as the API and tool-registration integration boundary. Derive the homepage and graph preview entirely from the current graph, brief, proposal, and activity responses. Persist only guide progress and identifiers in project-scoped `sessionStorage`.

**Tech Stack:** React 19, Vite 8, Vitest 4, Playwright 1.60, Framer Motion, Cytoscape graph data, CSS, and an installed line-icon React package.

**Spec:** `docs/superpowers/specs/2026-08-26-webmcp-memory-workspace-guided-demo-design.md`

## Global Constraints

- Preserve the warm off-white, forest-green, serif-headline Waggle identity.
- Use live `snapshot.nodes`, `snapshot.edges`, project brief, proposal, and activity data; never mock visible product state in production.
- Guided ChatGPT steps advance only from real registered WebMCP callbacks.
- Human approval advances only after a successful `proposal.edited_and_approved` response.
- Keep Challenge Demo and Human controlled signals visible.
- Hosted copy must describe isolated seeded judge mode without claiming hosted storage is local-first.
- Do not add any item listed in the challenge’s “DO NOT build before submission” section.

---

### Task 1: Guided Demo State Machine

**Files:**
- Create: `apps/mcp/graph-ui/src/lib/demo-state.js`
- Create: `apps/mcp/graph-ui/src/lib/demo-state.spec.js`

**Interfaces:**
- Consumes: normalized events `{ type, result?, proposal?, memoryIds? }` from Workspace callbacks.
- Produces: `DEMO_STEPS`, `createDemoState(project)`, `reduceDemoState(state, event)`, `loadDemoState(storage, project)`, `saveDemoState(storage, state)`, and `clearDemoState(storage, project)`.

- [ ] **Step 1: Write failing state-machine tests**

```js
it("advances only when the current step receives its matching real event", () => {
  const started = reduceDemoState(createDemoState("waggle-webmcp"), { type: "demo.started" });
  expect(reduceDemoState(started, { type: "webmcp.memory.recalled", memoryIds: ["storage"] })).toEqual(started);
  expect(reduceDemoState(started, { type: "webmcp.project_brief.read" }).step).toBe(2);
});

it("records proposal and applied-memory identifiers without storing payload truth", () => {
  const state = { ...createDemoState("waggle-webmcp"), active: true, step: 3 };
  const proposed = reduceDemoState(state, { type: "proposal.created", proposalId: "proposal_123", memoryId: "storage-v1" });
  expect(proposed).toMatchObject({ step: 4, proposalId: "proposal_123", memoryId: "storage-v1" });
  expect(proposed).not.toHaveProperty("approvedContent");
});
```

- [ ] **Step 2: Run `npm run test:unit -- src/lib/demo-state.spec.js` and verify missing-module failure**
- [ ] **Step 3: Implement the six immutable step definitions, reducer guards, and versioned session-storage helpers**
- [ ] **Step 4: Re-run the focused unit test and verify it passes**
- [ ] **Step 5: Commit `test/feat(webmcp): add guided demo state machine`**

### Task 2: Live Memory Map Selection

**Files:**
- Create: `apps/mcp/graph-ui/src/lib/memory-map.js`
- Create: `apps/mcp/graph-ui/src/lib/memory-map.spec.js`

**Interfaces:**
- Consumes: `{ nodes, edges }`, optional `focusMemoryId`, and `limit`.
- Produces: `selectMemoryMapPreview(snapshot, { focusMemoryId, limit })` returning only existing nodes and edges.

- [ ] **Step 1: Write failing tests proving every returned node and edge comes from the supplied graph and that focus lineage wins**

```js
it("returns a focused real subgraph without invented nodes or edges", () => {
  const result = selectMemoryMapPreview(snapshot, { focusMemoryId: "storage-v2", limit: 6 });
  expect(result.nodes[0].id).toBe("storage-v2");
  expect(result.nodes.every((node) => snapshot.nodes.includes(node))).toBe(true);
  expect(result.edges.every((edge) => snapshot.edges.includes(edge))).toBe(true);
});
```

- [ ] **Step 2: Run the focused test and verify missing-module failure**
- [ ] **Step 3: Implement deterministic breadth-first selection over real edges with authoritative fallback nodes**
- [ ] **Step 4: Re-run the focused test and verify it passes**
- [ ] **Step 5: Commit `feat(webmcp): derive focused live memory preview`**

### Task 3: Guided Rail and Memory-First Homepage Components

**Files:**
- Create: `apps/mcp/graph-ui/src/components/GuidedDemo.jsx`
- Create: `apps/mcp/graph-ui/src/components/MemoryMapPreview.jsx`
- Create: `apps/mcp/graph-ui/src/components/WorkspaceOverview.jsx`
- Modify: `apps/mcp/graph-ui/package.json`
- Modify: `apps/mcp/graph-ui/package-lock.json`
- Modify: `apps/mcp/graph-ui/src/styles.css`
- Test: `apps/mcp/graph-ui/tests/webmcp.spec.js`

**Interfaces:**
- `GuidedDemo({ state, onStart, onExit, onRestart, onCopyPrompt })` renders the rail/sheet.
- `MemoryMapPreview({ snapshot, focusMemoryId, graphHref })` renders the selected real subgraph.
- `WorkspaceOverview({ brief, snapshot, proposals, activity, demo, onNavigate, onStartDemo })` renders Option 2 hierarchy.

- [ ] **Step 1: Add Playwright assertions for the thesis, Current Context, Key Decisions, live graph link, secondary counts, and closed/open guide states**
- [ ] **Step 2: Run the browser test and verify the new semantic assertions fail against the old dashboard**
- [ ] **Step 3: Install the closest matching line-icon package and add the three focused components**
- [ ] **Step 4: Implement the Option 2 layout and responsive rail/sheet styles without static graph mocks**
- [ ] **Step 5: Re-run the focused browser test and verify homepage and guide rendering pass**
- [ ] **Step 6: Commit `feat(webmcp): add memory-first workspace and guided rail`**

### Task 4: Real Event Orchestration and Persistence

**Files:**
- Modify: `apps/mcp/graph-ui/src/Workspace.jsx`
- Modify: `apps/mcp/graph-ui/tests/webmcp.spec.js`

**Interfaces:**
- Consumes Task 1 state functions and Task 3 components.
- Produces normalized events from existing WebMCP callbacks and successful human review operations.

- [ ] **Step 1: Extend the browser test to start the demo, assert a real reset, execute each registered tool, and verify automatic route/focus/step transitions**
- [ ] **Step 2: Add reload and normal-navigation assertions proving `sessionStorage` progress survives**
- [ ] **Step 3: Run the test and verify it fails because callbacks do not yet update guide state**
- [ ] **Step 4: Integrate the reducer into `Workspace.jsx`; dispatch normalized events only inside successful callbacks**
- [ ] **Step 5: Add start, restart, exit, copy, scrolling, route focus, and storage reconciliation behavior**
- [ ] **Step 6: Re-run the focused browser test and verify the complete six-step flow passes**
- [ ] **Step 7: Commit `feat(webmcp): orchestrate guided demo with real events`**

### Task 5: Proposal Guidance and Focused Graph Links

**Files:**
- Modify: `apps/mcp/graph-ui/src/Workspace.jsx`
- Modify: `apps/mcp/graph-ui/src/App.jsx`
- Modify: `apps/mcp/graph-ui/src/styles.css`
- Modify: `apps/mcp/graph-ui/tests/webmcp.spec.js`
- Modify: `apps/mcp/graph-ui/tests/studio-flows.spec.js`

**Interfaces:**
- Workspace graph links append `focus=<memory-id>` when the guide has a relevant changed memory.
- Graph Studio reads `focus` on initial load and selects/focuses that existing node.

- [ ] **Step 1: Add failing tests for approved-state Copy Prompt, frozen-payload explanation, proposal focus, and Graph Studio focus query handling**
- [ ] **Step 2: Run focused tests and verify the assertions fail**
- [ ] **Step 3: Replace “Awaiting application by ChatGPT” with the requested actionable copy and Clipboard control**
- [ ] **Step 4: Implement proposal-card data focus and Graph Studio initial focus from a validated existing node ID**
- [ ] **Step 5: Re-run focused tests and verify they pass**
- [ ] **Step 6: Commit `feat(webmcp): connect approved proposals to focused lineage`**

### Task 6: Connection, Accessibility, and Responsive States

**Files:**
- Modify: `apps/mcp/graph-ui/src/Workspace.jsx`
- Modify: `apps/mcp/graph-ui/src/styles.css`
- Modify: `apps/mcp/graph-ui/tests/webmcp.spec.js`

**Interfaces:**
- Workspace distinguishes initial connecting/wake-up state from terminal API errors.
- Guided rail and bottom sheet expose accessible status and focus semantics.

- [ ] **Step 1: Add failing browser tests for “Connecting to Waggle…”, keyboard-accessible controls, 1280px layout, and mobile proposal/demo actions**
- [ ] **Step 2: Run the tests and verify the old loading/error and responsive behavior fail**
- [ ] **Step 3: Implement connection copy, `aria-live` statuses, focus-visible styles, and mobile/laptop breakpoints**
- [ ] **Step 4: Re-run the focused tests at desktop and mobile viewports**
- [ ] **Step 5: Commit `fix(webmcp): harden workspace connection and responsive states`**

### Task 7: Regression and Design QA

**Files:**
- Create: `apps/mcp/graph-ui/design-qa.md`
- Modify: P0/P1/P2 files identified by visual comparison only

**Interfaces:**
- Consumes the selected Option 2 reference and a same-state 1440×1024 local capture.
- Produces a blocking QA report whose final line is `final result: passed`.

- [ ] **Step 1: Run `npm run test:unit` and confirm the complete unit suite passes**
- [ ] **Step 2: Run `npm run test:browser` and confirm the complete Playwright suite passes**
- [ ] **Step 3: Run `npm run build` and confirm production build success**
- [ ] **Step 4: Open the local app in the configured in-app browser and execute the complete real local demo flow**
- [ ] **Step 5: Capture the selected reference and implementation at the same 1440×1024 state, compare them together, and write `design-qa.md`**
- [ ] **Step 6: Fix all P0/P1/P2 visual mismatches using a failing test first for behavioral changes, then repeat capture and comparison**
- [ ] **Step 7: Verify `design-qa.md` ends with `final result: passed`**
- [ ] **Step 8: Commit `test(webmcp): verify memory workspace guided demo`**

## Self-review

- Spec coverage: homepage hierarchy, real graph preview, six demo steps, reset, persistence, real-event detection, human approval, copy prompts, focused lineage, responsive behavior, connection state, and trust copy all map to explicit tasks.
- Placeholder scan: no deferred implementation placeholders remain.
- Type consistency: `proposalId`, `memoryId`, `focusMemoryId`, and normalized event names are consistent across state, component, and integration tasks.
