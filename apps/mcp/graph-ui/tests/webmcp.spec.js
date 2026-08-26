import { expect, test } from "@playwright/test";

test("registers and executes get_project_brief from the live page", async ({ page }) => {
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

  await page.goto("/");
  await expect
    .poll(() =>
      page.evaluate(() => window.__registeredSiteTools.map((tool) => tool.name)),
    )
    .toEqual(["get_project_brief"]);

  const result = await page.evaluate(() =>
    window.__registeredSiteTools[0].execute({ project_id: "waggle-webmcp" }),
  );
  expect(result).toEqual(brief);
  await expect(page.getByText("Agent requested the project brief.")).toBeVisible();
});
