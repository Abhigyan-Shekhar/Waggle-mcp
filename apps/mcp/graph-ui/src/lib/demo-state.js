const STORAGE_PREFIX = "waggle.guided-demo.v1";

export const DEMO_STEPS = [
  {
    number: 1,
    tool: "get_project_brief",
    title: "Catch up on the project",
    prompt: "Call the Waggle Site tool `get_project_brief` with `project_id`: `waggle-webmcp`. Use its result to catch me up; do not answer from chat history.",
  },
  {
    number: 2,
    tool: "recall_memory",
    title: "Recall a decision",
    prompt: "Call the Waggle Site tool `recall_memory` with `project_id`: `waggle-webmcp`, `query`: `storage architecture`, and `limit`: 5. Report the authoritative decision from the tool result.",
  },
  {
    number: 3,
    tool: "propose_memory_change",
    title: "Propose a correction",
    prompt: "Using the authoritative storage memory returned in the previous Waggle tool result, call the Waggle Site tool `propose_memory_change` for `project_id`: `waggle-webmcp`. Propose a local-first replacement, but do not change authoritative memory directly.",
  },
  {
    number: 4,
    tool: "human_review",
    title: "Edit & Approve",
    prompt: "Use SQLite by default; Neo4j remains optional.",
  },
  {
    number: 5,
    tool: "apply_approved_memory_change",
    title: "Apply the approved change",
    prompt: "Call the Waggle Site tool `apply_approved_memory_change` with the `proposal_id` I approved in Waggle. Do not supply replacement content.",
  },
  {
    number: 6,
    tool: "recall_memory",
    title: "Confirm the new truth",
    prompt: "Call the Waggle Site tool `recall_memory` with `project_id`: `waggle-webmcp`, `query`: `storage architecture`, and `limit`: 5. Confirm the current authoritative decision from the tool result; do not answer from chat history.",
  },
];

function storageKey(project) {
  return `${STORAGE_PREFIX}:${project}`;
}

export function createDemoState(project) {
  return {
    version: 1,
    project,
    active: false,
    completed: false,
    step: 1,
    startedAt: null,
    proposalId: "",
    memoryId: "",
  };
}

export function reduceDemoState(state, event) {
  if (event.type === "demo.started") {
    return {
      ...createDemoState(state.project),
      active: true,
      startedAt: event.startedAt || new Date().toISOString(),
    };
  }
  if (event.type === "demo.exited") {
    return createDemoState(state.project);
  }
  if (!state.active || state.completed) return state;

  if (state.step === 1 && event.type === "webmcp.project_brief.read") {
    return { ...state, step: 2 };
  }
  if (
    state.step === 2
    && event.type === "webmcp.memory.recalled"
    && event.storageMemoryId
    && event.memoryIds?.includes(event.storageMemoryId)
  ) {
    return { ...state, step: 3, memoryId: event.storageMemoryId };
  }
  if (state.step === 3 && event.type === "proposal.created" && event.proposalId) {
    return {
      ...state,
      step: 4,
      proposalId: event.proposalId,
      memoryId: event.memoryId || state.memoryId,
    };
  }
  if (
    state.step === 4
    && event.type === "proposal.edited_and_approved"
    && event.proposalId === state.proposalId
  ) {
    return { ...state, step: 5 };
  }
  if (
    state.step === 5
    && event.type === "proposal.applied"
    && event.proposalId === state.proposalId
    && event.memoryId
  ) {
    return { ...state, step: 6, memoryId: event.memoryId };
  }
  if (
    state.step === 6
    && event.type === "webmcp.memory.recalled"
    && state.memoryId
    && event.memoryIds?.includes(state.memoryId)
  ) {
    return { ...state, completed: true };
  }
  return state;
}

export function loadDemoState(storage, project) {
  const fallback = createDemoState(project);
  try {
    const stored = JSON.parse(storage.getItem(storageKey(project)) || "null");
    if (
      stored?.version !== 1
      || stored.project !== project
      || typeof stored.active !== "boolean"
      || typeof stored.completed !== "boolean"
      || !Number.isInteger(stored.step)
      || stored.step < 1
      || stored.step > DEMO_STEPS.length
    ) {
      return fallback;
    }
    return { ...fallback, ...stored };
  } catch {
    return fallback;
  }
}

export function saveDemoState(storage, state) {
  try {
    storage.setItem(storageKey(state.project), JSON.stringify(state));
  } catch {
    // Guided progress remains usable in memory when storage is unavailable/full.
  }
}

export function clearDemoState(storage, project) {
  try {
    storage.removeItem(storageKey(project));
  } catch {
    // Storage access/removal can be denied by an embedded browser.
  }
}

export function getSessionStorage() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
