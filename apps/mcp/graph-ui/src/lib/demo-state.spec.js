import { describe, expect, it } from "vitest";
import {
  clearDemoState,
  createDemoState,
  DEMO_STEPS,
  loadDemoState,
  reduceDemoState,
  saveDemoState,
} from "./demo-state.js";

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  };
}

describe("guided demo state", () => {
  it("uses explicit Site-tool prompts for every ChatGPT step", () => {
    for (const step of DEMO_STEPS.filter((item) => item.tool !== "human_review")) {
      expect(step.prompt).toContain(`\`${step.tool}\``);
    }
    expect(DEMO_STEPS[0].prompt).toContain("`project_id`: `waggle-webmcp`");
    expect(DEMO_STEPS[1].prompt).toContain("`query`: `storage architecture`");
    expect(DEMO_STEPS[4].prompt).toContain("`proposal_id`");
    expect(DEMO_STEPS[5].prompt).toContain("do not answer from chat history");
  });

  it("does not advance when an event belongs to a later ChatGPT step", () => {
    const started = reduceDemoState(createDemoState("waggle-webmcp"), {
      type: "demo.started",
      startedAt: "2026-08-26T10:00:00.000Z",
    });

    const ignored = reduceDemoState(started, {
      type: "webmcp.memory.recalled",
      memoryIds: ["storage-v1"],
    });

    expect(ignored).toEqual(started);
    expect(reduceDemoState(started, { type: "webmcp.project_brief.read" }).step).toBe(2);
  });

  it("requires storage recall before advancing recall steps", () => {
    const state = { ...createDemoState("waggle-webmcp"), active: true, step: 2 };

    expect(reduceDemoState(state, {
      type: "webmcp.memory.recalled",
      memoryIds: ["unrelated-memory"],
      storageMemoryId: "storage-v1",
    })).toEqual(state);
    expect(reduceDemoState(state, {
      type: "webmcp.memory.recalled",
      memoryIds: ["storage-v1"],
      storageMemoryId: "storage-v1",
    })).toMatchObject({ step: 3, memoryId: "storage-v1" });
  });

  it("records proposal and applied memory identifiers without copying payload truth", () => {
    const proposed = reduceDemoState(
      { ...createDemoState("waggle-webmcp"), active: true, step: 3 },
      {
        type: "proposal.created",
        proposalId: "proposal_123",
        memoryId: "storage-v1",
        approvedContent: "must not be persisted",
      },
    );
    const approved = reduceDemoState(proposed, {
      type: "proposal.edited_and_approved",
      proposalId: "proposal_123",
    });
    const applied = reduceDemoState(approved, {
      type: "proposal.applied",
      proposalId: "proposal_123",
      memoryId: "storage-v2",
    });

    expect(proposed).toMatchObject({ step: 4, proposalId: "proposal_123", memoryId: "storage-v1" });
    expect(proposed).not.toHaveProperty("approvedContent");
    expect(approved.step).toBe(5);
    expect(applied).toMatchObject({ step: 6, memoryId: "storage-v2" });
  });

  it("completes only when final recall includes the applied memory", () => {
    const state = {
      ...createDemoState("waggle-webmcp"),
      active: true,
      step: 6,
      memoryId: "storage-v2",
      proposalId: "proposal_123",
    };

    expect(reduceDemoState(state, {
      type: "webmcp.memory.recalled",
      memoryIds: ["storage-v1"],
    })).toEqual(state);
    expect(reduceDemoState(state, {
      type: "webmcp.memory.recalled",
      memoryIds: ["storage-v2"],
    })).toMatchObject({ active: true, completed: true, step: 6 });
  });

  it("persists progress by project and rejects malformed stored state", () => {
    const storage = memoryStorage();
    const state = {
      ...createDemoState("waggle-webmcp"),
      active: true,
      step: 4,
      proposalId: "proposal_123",
    };

    saveDemoState(storage, state);
    expect(loadDemoState(storage, "waggle-webmcp")).toEqual(state);
    expect(loadDemoState(storage, "another-project")).toEqual(createDemoState("another-project"));

    storage.setItem("waggle.guided-demo.v1:broken-project", "not-json");
    expect(loadDemoState(storage, "broken-project")).toEqual(createDemoState("broken-project"));

    clearDemoState(storage, "waggle-webmcp");
    expect(loadDemoState(storage, "waggle-webmcp")).toEqual(createDemoState("waggle-webmcp"));
  });
});
