import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  // Mock common endpoints for deterministic behavior
  await page.route("**/api/graph**", async (route) => {
    const url = route.request().url();
    if (url.includes("/transcripts")) {
      const isSearch = url.includes("query=");
      const data = isSearch
        ? {
            hits: [
              {
                id: "t-1",
                session_id: "test-session",
                project: "test-project",
                agent_id: "test-agent",
                turn_index: 0,
                role: "user",
                transcript_text: "Help me write a document.",
                observed_at: "2026-06-13T08:00:00Z"
              }
            ],
            pagination: { offset: 0, total_count: 1 }
          }
        : {
            records: [
              {
                id: "t-1",
                session_id: "test-session",
                project: "test-project",
                agent_id: "test-agent",
                turn_index: 0,
                role: "user",
                transcript_text: "Help me write a document.",
                observed_at: "2026-06-13T08:00:00Z"
              }
            ],
            pagination: { offset: 0, total_count: 1 }
          };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(data)
      });
    } else if (url.includes("/export")) {
      await route.fulfill({
        status: 200,
        contentType: "application/octet-stream",
        body: Buffer.from("dummy abhi document content")
      });
    } else if (url.includes("/preview-import")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          validation: { valid: true, errors: [] },
          inspect: { nodes_count: 1, edges_count: 0 },
          snapshot: {
            nodes: [
              {
                id: "node-imp-1",
                label: "Imported Node 1",
                content: "This node was imported.",
                node_type: "fact",
                project: "test-project"
              }
            ],
            edges: []
          }
        })
      });
    } else if (url.includes("/import")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          imported_node_ids: ["node-imp-1"]
        })
      });
    } else if (url.includes("/retrieval-debug")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          token_estimate: 150,
          debug: {
            flat_top_nodes: [
              {
                node_id: "node-1",
                label: "Retrieved Node 1",
                final_score: 0.85,
                similarity_score: 0.90,
                recency_score: 0.75
              }
            ],
            all_windows: [
              {
                window_id: "win-1",
                title: "Test Window Title",
                routing_score: 0.85,
                similarity: 0.90,
                recency: 0.75
              }
            ]
          },
          replay_hits: [
            {
              node_id: "node-1",
              label: "Retrieved Node 1",
              score: 0.85
            }
          ],
          fusion_hits: [
            {
              id: "node-1",
              label: "Retrieved Node 1",
              score: 0.85
            }
          ]
        })
      });
    } else {
      // Default fallback
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tenant_id: "test-tenant",
          nodes: [
            {
              id: "node-1",
              label: "Test Node 1",
              content: "This is a test node content",
              node_type: "decision",
              tags: ["test"],
              project: "test-project",
              agent_id: "test-agent",
              session_id: "test-session"
            }
          ],
          edges: [],
          ui: {}
        })
      });
    }
  });

  // Default configuration in edit mode
  await page.addInitScript(() => {
    window.__WAGGLE_GRAPH_CONFIG__ = {
      mode: "edit",
      sampleMode: false,
      project: "test-project",
      agent_id: "test-agent",
      session_id: "test-session"
    };
  });
  await page.goto("/");
});

test.describe("Graph UI Workflows", () => {
  test("should trigger export when clicking Export button", async ({ page }) => {
    // Intercept/expect the export network request
    const exportRequestPromise = page.waitForRequest(request =>
      request.url().includes("/api/graph/export") && request.url().includes("format=abhi")
    );

    await page.click('button:has-text("Export")');

    const request = await exportRequestPromise;
    expect(request.url()).toContain("format=abhi");
  });

  test("should handle file import and preview", async ({ page }) => {
    // Check that import preview details are not present initially
    await expect(page.locator("text=1 nodes · 0 edges")).not.toBeVisible();

    // Trigger file selection for import preview
    await page.setInputFiles('label:has-text("Import preview") input', {
      name: "waggle-memory.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          nodes: [
            {
              id: "node-imp-1",
              label: "Imported Node 1",
              content: "This node was imported.",
              node_type: "fact"
            }
          ],
          edges: []
        })
      )
    });

    // Verify import preview details are now displayed
    await expect(page.locator("text=1 nodes · 0 edges")).toBeVisible();
    await expect(page.locator("text=Imported Node 1")).toBeVisible();

    // Verify commit button is present and clickable
    const commitButton = page.locator('button:has-text("Commit import")');
    await expect(commitButton).toBeVisible();

    // Mock subsequent loadSnapshot call to contain the newly imported node
    await page.route("**/api/graph", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tenant_id: "test-tenant",
          nodes: [
            {
              id: "node-1",
              label: "Test Node 1",
              content: "This is a test node content",
              node_type: "decision",
              project: "test-project"
            },
            {
              id: "node-imp-1",
              label: "Imported Node 1",
              content: "This node was imported.",
              node_type: "fact",
              project: "test-project",
              imported: true
            }
          ],
          edges: [],
          ui: {}
        })
      });
    });

    // Commit the import
    await commitButton.click();

    // Verify toast confirmation and sidebar preview is closed
    await expect(page.locator("text=Imported graph data.")).toBeVisible();
    await expect(page.locator("text=1 nodes · 0 edges")).not.toBeVisible();
  });

  test("should execute and display retrieval debug flows", async ({ page }) => {
    // Switch to the retrieval debugger tab
    await page.click('button:has-text("Retrieval")');

    // Verify text area query input is pre-populated
    const textarea = page.locator("textarea");
    await expect(textarea).toBeVisible();
    await expect(textarea).toHaveValue("how do transcript provenance and derived nodes interact?");

    // Enter a new query
    await textarea.fill("new custom query for retrieval");

    // Click Run debugger
    await page.click('button:has-text("Run debugger")');

    // Verify retrieval debugger results are rendered
    await expect(page.locator("text=150 tokens")).toBeVisible();
    await expect(page.locator("text=Retrieved Node 1")).toBeVisible();
    await expect(page.locator("text=final 0.85 · vector 0.90 · recency 0.75")).toBeVisible();
    await expect(page.locator("text=Test Window Title")).toBeVisible();
  });

  test("should support search in transcripts tab", async ({ page }) => {
    // Switch to transcripts tab
    await page.click('button:has-text("Transcripts")');

    // Fill the search transcripts input
    const searchInput = page.locator('input[placeholder="Search transcripts (hybrid BM25 + vector)"]');
    await expect(searchInput).toBeVisible();
    await searchInput.fill("Help me");

    // Click Search button
    await page.click('button:has-text("Search")');

    // Verify request url and search result rendering
    await expect(page.locator("text=user")).toBeVisible();
    await expect(page.locator("text=Help me write a document.")).toBeVisible();
  });
});
