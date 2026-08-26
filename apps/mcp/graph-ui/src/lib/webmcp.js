import { apiRequest } from "./api";

const registrations = new WeakMap();

function registerOnce(modelContext, tool) {
  let tools = registrations.get(modelContext);
  if (!tools) {
    tools = new Map();
    registrations.set(modelContext, tools);
  }
  const existing = tools.get(tool.name);
  if (existing) {
    return existing;
  }
  const registration = Promise.resolve(modelContext.registerTool(tool))
    .then(() => true)
    .catch((error) => {
      tools.delete(tool.name);
      throw error;
    });
  tools.set(tool.name, registration);
  return registration;
}

function assertWorkspaceProject(projectId, getScope) {
  const workspaceProject = String(getScope()?.project || "").trim();
  if (workspaceProject && projectId !== workspaceProject) {
    throw new Error(
      "PROJECT_NOT_IN_WORKSPACE: open that project in Waggle before requesting its memory.",
    );
  }
}

export function registerGetProjectBriefTool({
  modelContext = document.modelContext,
  getScope = () => ({}),
  onActivity = () => {},
} = {}) {
  if (typeof modelContext?.registerTool !== "function") {
    return Promise.resolve(false);
  }

  return registerOnce(modelContext, {
    name: "get_project_brief",
    description:
      "Get a compact, authoritative project briefing from the Waggle memory visible in this workspace.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: {
          type: "string",
          minLength: 1,
          maxLength: 512,
          description: "The exact Waggle project identifier to brief.",
        },
      },
      required: ["project_id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    execute: async (input) => {
      const projectId = input?.project_id;
      if (typeof projectId !== "string" || projectId.trim() === "") {
        throw new Error("INVALID_INPUT: project_id is required.");
      }

      assertWorkspaceProject(projectId.trim(), getScope);

      const result = await apiRequest("/api/webmcp/project-brief", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId.trim() }),
      });
      onActivity({
        tool: "get_project_brief",
        project_id: projectId.trim(),
        supporting_memory_count: result.supporting_memory_ids?.length || 0,
      });
      return result;
    },
  });
}

export function registerRecallMemoryTool({
  modelContext = document.modelContext,
  getScope = () => ({}),
  onActivity = () => {},
} = {}) {
  if (typeof modelContext?.registerTool !== "function") {
    return Promise.resolve(false);
  }

  return registerOnce(modelContext, {
    name: "recall_memory",
    description:
      "Recall current authoritative Waggle memories for a project. Superseded and expired memories are excluded.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: {
          type: "string",
          minLength: 1,
          maxLength: 512,
          description: "The exact Waggle project identifier to search.",
        },
        query: {
          type: "string",
          minLength: 1,
          maxLength: 4000,
          description: "The question or memory topic to recall.",
        },
        limit: {
          type: "integer",
          minimum: 1,
          maximum: 10,
          default: 5,
          description: "Maximum number of authoritative memories to return.",
        },
      },
      required: ["project_id", "query"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    execute: async (input) => {
      const projectId = input?.project_id;
      const query = input?.query;
      const limit = input?.limit ?? 5;
      if (typeof projectId !== "string" || projectId.trim() === "") {
        throw new Error("INVALID_INPUT: project_id is required.");
      }
      if (typeof query !== "string" || query.trim() === "") {
        throw new Error("INVALID_INPUT: query is required.");
      }
      if (!Number.isInteger(limit) || limit < 1 || limit > 10) {
        throw new Error("INVALID_INPUT: limit must be an integer between 1 and 10.");
      }

      assertWorkspaceProject(projectId.trim(), getScope);
      const result = await apiRequest("/api/webmcp/recall-memory", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId.trim(),
          query: query.trim(),
          limit,
        }),
      });
      onActivity({
        tool: "recall_memory",
        project_id: projectId.trim(),
        result_count: result.memories?.length || 0,
      });
      return result;
    },
  });
}

export function registerProposeMemoryChangeTool({
  modelContext = document.modelContext,
  getScope = () => ({}),
  onActivity = () => {},
} = {}) {
  if (typeof modelContext?.registerTool !== "function") {
    return Promise.resolve(false);
  }

  return registerOnce(modelContext, {
    name: "propose_memory_change",
    description:
      "Propose a change to an existing authoritative Waggle memory for human review. This does not modify the authoritative memory.",
    inputSchema: {
      type: "object",
      properties: {
        project_id: {
          type: "string",
          minLength: 1,
          maxLength: 512,
          description: "The exact Waggle project identifier containing the memory.",
        },
        memory_id: {
          type: "string",
          minLength: 1,
          maxLength: 512,
          description: "The current authoritative memory to propose changing.",
        },
        proposed_content: {
          type: "string",
          minLength: 1,
          maxLength: 20000,
          description: "The replacement content proposed for human review.",
        },
        reason: {
          type: "string",
          maxLength: 4000,
          description: "Why the memory should change.",
        },
        evidence_ids: {
          type: "array",
          maxItems: 20,
          uniqueItems: true,
          items: { type: "string", minLength: 1, maxLength: 512 },
          description: "Optional same-project memories supporting the proposal.",
        },
      },
      required: ["project_id", "memory_id", "proposed_content"],
      additionalProperties: false,
    },
    annotations: {
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
    },
    execute: async (input) => {
      const projectId = input?.project_id;
      const memoryId = input?.memory_id;
      const proposedContent = input?.proposed_content;
      const reason = input?.reason ?? "";
      const evidenceIds = input?.evidence_ids ?? [];
      if (typeof projectId !== "string" || projectId.trim() === "") {
        throw new Error("INVALID_INPUT: project_id is required.");
      }
      if (typeof memoryId !== "string" || memoryId.trim() === "") {
        throw new Error("INVALID_INPUT: memory_id is required.");
      }
      if (typeof proposedContent !== "string" || proposedContent.trim() === "") {
        throw new Error("INVALID_INPUT: proposed_content is required.");
      }
      if (typeof reason !== "string") {
        throw new Error("INVALID_INPUT: reason must be a string.");
      }
      if (!Array.isArray(evidenceIds) || evidenceIds.some((item) => typeof item !== "string")) {
        throw new Error("INVALID_INPUT: evidence_ids must be an array of strings.");
      }

      assertWorkspaceProject(projectId.trim(), getScope);
      const result = await apiRequest("/api/webmcp/proposals", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId.trim(),
          memory_id: memoryId.trim(),
          proposed_content: proposedContent.trim(),
          reason: reason.trim(),
          evidence_ids: evidenceIds.map((item) => item.trim()),
        }),
      });
      onActivity({
        tool: "propose_memory_change",
        project_id: projectId.trim(),
        proposal: result,
      });
      return result;
    },
  });
}
