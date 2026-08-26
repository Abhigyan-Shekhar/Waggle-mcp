import { expect, test } from "@playwright/test";

test("registers and executes read-only Waggle tools from the live page", async ({ page }) => {
  const brief = {
    project: { id: "waggle-webmcp", name: "Waggle WebMCP" },
    goal: "Build governed shared memory.",
    current_state: [],
    decisions: [],
    constraints: [],
    open_questions: [],
    recent_changes: [],
    supporting_memory_ids: ["memory-1"],
  };
  const recall = {
    query: "storage architecture",
    project_id: "waggle-webmcp",
    memories: [{ memory_id: "memory-v3", status: "authoritative" }],
  };

  await page.addInitScript(() => {
    window.__WAGGLE_GRAPH_CONFIG__ = {
      schemaVersion: 1,
      mode: "edit",
      sampleMode: false,
      scope: {
        project: "waggle-webmcp",
        agent_id: "",
        session_id: "",
      },
    };
    window.__registeredSiteTools = [];
    Object.defineProperty(document, "modelContext", {
      configurable: true,
      value: {
        registerTool(tool) {
          window.__registeredSiteTools.push(tool);
        },
      },
    });
  });

  await page.route("**/api/graph**", async (route) => {
    const url = route.request().url();
    const payload = url.includes("/transcripts")
      ? { records: [], pagination: { offset: 0, total_count: 0 } }
      : { tenant_id: "demo", nodes: [], edges: [], ui: {} };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
  });
  await page.route("**/api/webmcp/project-brief", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ project_id: "waggle-webmcp" });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(brief),
    });
  });
  await page.route("**/api/webmcp/recall-memory", async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      project_id: "waggle-webmcp",
      query: "storage architecture",
      limit: 5,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(recall),
    });
  });

  await page.goto("/");
  await expect
    .poll(() =>
      page.evaluate(() => window.__registeredSiteTools.map((tool) => tool.name)),
    )
    .toEqual(["get_project_brief", "recall_memory"]);

  const briefResult = await page.evaluate(() =>
    window.__registeredSiteTools
      .find((tool) => tool.name === "get_project_brief")
      .execute({ project_id: "waggle-webmcp" }),
  );
  expect(briefResult).toEqual(brief);
  await expect(page.getByText("Agent requested the project brief.")).toBeVisible();

  const recallResult = await page.evaluate(() =>
    window.__registeredSiteTools
      .find((tool) => tool.name === "recall_memory")
      .execute({
        project_id: "waggle-webmcp",
        query: "storage architecture",
        limit: 5,
      }),
  );
  expect(recallResult).toEqual(recall);
  await expect(
    page.getByText("Agent recalled 1 authoritative memories."),
  ).toBeVisible();
});
