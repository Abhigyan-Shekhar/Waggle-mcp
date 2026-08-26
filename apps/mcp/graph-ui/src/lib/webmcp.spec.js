import { afterEach, describe, expect, it, vi } from "vitest";

import { registerGetProjectBriefTool } from "./webmcp";

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
});
