import { afterEach, describe, expect, it, vi } from "vitest";

import {
  registerApplyApprovedMemoryChangeTool,
  registerGetProjectBriefTool,
  registerProposeMemoryChangeTool,
  registerRecallMemoryTool,
} from "./webmcp";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("get_project_brief WebMCP registration", () => {
  it("does nothing in browsers without WebMCP", async () => {
    await expect(
      registerGetProjectBriefTool({ modelContext: {} }),
    ).resolves.toBe(false);
  });

  it("registers the official tool shape and executes against Waggle", async () => {
    let definition;
    const modelContext = {
      registerTool: vi.fn((tool) => {
        definition = tool;
      }),
    };
    const activity = vi.fn();
    const payload = {
      project: { id: "waggle-webmcp", name: "Waggle WebMCP" },
      decisions: [],
      constraints: [],
      current_state: [],
      open_questions: [],
      recent_changes: [],
      supporting_memory_ids: ["memory-1"],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => payload,
      })),
    );

    await registerGetProjectBriefTool({
      modelContext,
      getScope: () => ({ project: "waggle-webmcp" }),
      onActivity: activity,
    });

    expect(modelContext.registerTool).toHaveBeenCalledOnce();
    expect(definition.name).toBe("get_project_brief");
    expect(definition.description).toContain("when the user asks to catch up");
    expect(definition.description).toContain("do not substitute chat history");
    expect(definition.annotations).toEqual({ readOnlyHint: true });
    expect(definition.inputSchema.required).toEqual(["project_id"]);

    await expect(
      definition.execute({ project_id: "waggle-webmcp" }),
    ).resolves.toEqual(payload);
    expect(fetch).toHaveBeenCalledWith(
      "/api/webmcp/project-brief",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ project_id: "waggle-webmcp" }),
      }),
    );
    expect(activity).toHaveBeenCalledWith(
      expect.objectContaining({
        tool: "get_project_brief",
        supporting_memory_count: 1,
      }),
    );
  });

  it("prevents the page tool from reading another open workspace", async () => {
    let definition;
    const modelContext = {
      registerTool: (tool) => {
        definition = tool;
      },
    };

    await registerGetProjectBriefTool({
      modelContext,
      getScope: () => ({ project: "project-a" }),
    });

    await expect(
      definition.execute({ project_id: "project-b" }),
    ).rejects.toThrow("PROJECT_NOT_IN_WORKSPACE");
  });

  it("constrains project-scoped Site tools to the project open in Waggle", async () => {
    const definitions = [];
    const modelContext = {
      registerTool: (tool) => definitions.push(tool),
    };
    const options = {
      modelContext,
      getScope: () => ({ project: "waggle-webmcp" }),
    };

    await Promise.all([
      registerGetProjectBriefTool(options),
      registerRecallMemoryTool(options),
      registerProposeMemoryChangeTool(options),
    ]);

    for (const definition of definitions) {
      expect(definition.inputSchema.properties.project_id.enum).toEqual(["waggle-webmcp"]);
    }
  });
});

describe("recall_memory WebMCP registration", () => {
  it("registers once, stays read-only, and returns the HTTP result unchanged", async () => {
    const definitions = [];
    const modelContext = {
      registerTool: vi.fn((tool) => definitions.push(tool)),
    };
    const activity = vi.fn();
    const payload = {
      query: "storage architecture",
      project_id: "waggle-webmcp",
      memories: [{ memory_id: "memory-v3", status: "authoritative" }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => payload,
      })),
    );

    const options = {
      modelContext,
      getScope: () => ({ project: "waggle-webmcp" }),
      onActivity: activity,
    };
    await Promise.all([
      registerGetProjectBriefTool(options),
      registerRecallMemoryTool(options),
      registerRecallMemoryTool(options),
    ]);

    expect(modelContext.registerTool).toHaveBeenCalledTimes(2);
    const definition = definitions.find((tool) => tool.name === "recall_memory");
    expect(definition.description).toContain("what was decided");
    expect(definition.annotations).toEqual({ readOnlyHint: true });
    await expect(
      definition.execute({
        project_id: "waggle-webmcp",
        query: "storage architecture",
        limit: 5,
      }),
    ).resolves.toEqual(payload);
    expect(fetch).toHaveBeenCalledWith(
      "/api/webmcp/recall-memory",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          project_id: "waggle-webmcp",
          query: "storage architecture",
          limit: 5,
        }),
      }),
    );
    expect(activity).toHaveBeenCalledWith({
      tool: "recall_memory",
      project_id: "waggle-webmcp",
      result_count: 1,
      result: payload,
    });
  });

  it("rejects cross-workspace recall before making a request", async () => {
    let definition;
    await registerRecallMemoryTool({
      modelContext: { registerTool: (tool) => (definition = tool) },
      getScope: () => ({ project: "project-a" }),
    });

    await expect(
      definition.execute({ project_id: "project-b", query: "storage" }),
    ).rejects.toThrow("PROJECT_NOT_IN_WORKSPACE");
  });
});

describe("propose_memory_change WebMCP registration", () => {
  it("registers as an idempotent workflow mutation and returns the persisted proposal", async () => {
    let definition;
    const activity = vi.fn();
    const modelContext = {
      registerTool: vi.fn((tool) => (definition = tool)),
    };
    const proposal = {
      proposal_id: "proposal_123",
      status: "pending",
      project_id: "waggle-webmcp",
      target: {
        memory_id: "memory-v3",
        current_content: "Use Neo4j.",
        version: "fingerprint",
      },
      proposed_content: "Use SQLite by default.",
      reason: "Preserve local-first architecture.",
      evidence_ids: [],
      proposed_by: { type: "agent", id: "webmcp" },
      created_at: "2026-08-26T00:00:00+00:00",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => proposal,
      })),
    );

    const options = {
      modelContext,
      getScope: () => ({ project: "waggle-webmcp" }),
      onActivity: activity,
    };
    await Promise.all([
      registerProposeMemoryChangeTool(options),
      registerProposeMemoryChangeTool(options),
    ]);

    expect(modelContext.registerTool).toHaveBeenCalledOnce();
    expect(definition.name).toBe("propose_memory_change");
    expect(definition.description).toContain("does not modify the authoritative memory");
    expect(definition.annotations).toEqual({
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
    });
    await expect(
      definition.execute({
        project_id: "waggle-webmcp",
        memory_id: "memory-v3",
        proposed_content: "Use SQLite by default.",
        reason: "Preserve local-first architecture.",
      }),
    ).resolves.toEqual(proposal);
    expect(fetch).toHaveBeenCalledWith(
      "/api/webmcp/proposals",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          project_id: "waggle-webmcp",
          memory_id: "memory-v3",
          proposed_content: "Use SQLite by default.",
          reason: "Preserve local-first architecture.",
          evidence_ids: [],
        }),
      }),
    );
    expect(activity).toHaveBeenCalledWith({
      tool: "propose_memory_change",
      project_id: "waggle-webmcp",
      proposal,
    });
  });
});

describe("apply_approved_memory_change WebMCP registration", () => {
  it("accepts only proposal_id and returns the immutable applied result", async () => {
    let definition;
    const activity = vi.fn();
    const result = {
      proposal_id: "proposal_123",
      status: "applied",
      authoritative_memory: { memory_id: "memory-v4", content: "Human-approved value." },
      already_applied: false,
      proposal: { proposal_id: "proposal_123", status: "applied" },
    };
    const modelContext = {
      registerTool: vi.fn((tool) => (definition = tool)),
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => result,
      })),
    );

    const options = {
      modelContext,
      getScope: () => ({ project: "waggle-webmcp" }),
      onActivity: activity,
    };
    await Promise.all([
      registerApplyApprovedMemoryChangeTool(options),
      registerApplyApprovedMemoryChangeTool(options),
    ]);

    expect(modelContext.registerTool).toHaveBeenCalledOnce();
    expect(definition.description).toContain("cannot alter the approved content or bypass human review");
    expect(definition.inputSchema.required).toEqual(["proposal_id"]);
    expect(definition.inputSchema.properties).toEqual({
      proposal_id: expect.any(Object),
    });
    await expect(definition.execute({ proposal_id: "proposal_123" })).resolves.toEqual(result);
    expect(fetch).toHaveBeenCalledWith(
      "/api/webmcp/proposals/proposal_123/apply",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ project_id: "waggle-webmcp" }),
      }),
    );
    expect(activity).toHaveBeenCalledWith({
      tool: "apply_approved_memory_change",
      project_id: "waggle-webmcp",
      result,
    });
  });
});
