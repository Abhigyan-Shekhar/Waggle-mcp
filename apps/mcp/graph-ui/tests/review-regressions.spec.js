import { expect, test } from "@playwright/test";

async function mockWorkspace(page, proposals = []) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const payload = path === "/api/graph"
      ? { nodes: [], edges: [], ui: {} }
      : path === "/api/webmcp/proposals"
        ? { proposals }
        : path === "/api/webmcp/project-brief"
          ? { project: { id: "waggle-webmcp", name: "Waggle" }, goal: "Test workspace", decisions: [], constraints: [], open_questions: [] }
          : path === "/api/admin/audit-events" ? [] : {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
  });
}

test("Graph Studio renders pending and applied proposals without a target", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockWorkspace(page, [
    { proposal_id: "pending", status: "pending", proposed_content: "Pending replacement" },
    { proposal_id: "applied", status: "applied", proposed_content: "Applied replacement", approved_content: "Approved replacement" },
  ]);
  await page.addInitScript(() => {
    window.__WAGGLE_GRAPH_CONFIG__ = { mode: "edit", sampleMode: false, project: "waggle-webmcp" };
  });
  await page.goto("/graph");
  await expect(page.locator('[data-proposal-id="pending"]')).toContainText("Pending replacement");
  await expect(page.locator('[data-proposal-id="applied"]')).toContainText("Approved replacement");
  expect(errors).toEqual([]);
});

test("Workspace and guided demo remain usable when sessionStorage access throws", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockWorkspace(page);
  await page.addInitScript(() => {
    window.__WAGGLE_GRAPH_CONFIG__ = { mode: "edit", sampleMode: false, demoMode: true, project: "waggle-webmcp" };
    Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("Denied", "SecurityError"); } });
  });
  await page.goto("/workspace");
  await page.getByRole("button", { name: "See Waggle in Action" }).click();
  await expect(page.getByText("Step 1 of 6", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
