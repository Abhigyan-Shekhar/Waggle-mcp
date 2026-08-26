import { apiRequest } from "./api";

const registrations = new WeakMap();

export function registerGetProjectBriefTool({
  modelContext = document.modelContext,
  getScope = () => ({}),
  onActivity = () => {},
} = {}) {
  if (typeof modelContext?.registerTool !== "function") {
    return Promise.resolve(false);
  }

  const existing = registrations.get(modelContext);
  if (existing) {
    return existing;
  }

  const registration = Promise.resolve(
    modelContext.registerTool({
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

        const workspaceProject = String(getScope()?.project || "").trim();
        if (workspaceProject && projectId.trim() !== workspaceProject) {
          throw new Error(
            "PROJECT_NOT_IN_WORKSPACE: open that project in Waggle before requesting its brief.",
          );
        }

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
    }),
  )
    .then(() => true)
    .catch((error) => {
      registrations.delete(modelContext);
      throw error;
    });

  registrations.set(modelContext, registration);
  return registration;
}
