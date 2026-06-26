import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  // Common mocks for page load
  await page.route("**/api/graph**", async (route) => {
    const url = route.request().url();
    const method = route.request().method();

    if (url.includes("/transcripts") && method === "GET") {
      // By default, return a basic list of transcripts
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
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
            },
            {
              id: "t-2",
              session_id: "test-session",
              project: "test-project",
              agent_id: "test-agent",
              turn_index: 1,
              role: "assistant",
              transcript_text: "Document created successfully.",
              observed_at: "2026-06-13T08:01:00Z"
            }
          ],
          pagination: {
            offset: 0,
            total_count: 2
          }
        })
      });
    } else if (method === "GET") {
      // Return a basic initial graph snapshot
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tenant_id: "test-tenant",
          nodes: [
            {
              id: "node-1",
              label: "Initial Node 1",
              content: "Initial content",
              node_type: "decision",
              tags: ["initial"],
              source_prompt: "user: Help me write a document.",
              evidence_records: [],
              project: "test-project",
              agent_id: "test-agent",
              session_id: "test-session",
              updated_at: "2026-06-13T08:01:00Z",
              created_at: "2026-06-13T08:01:00Z"
            }
          ],
          edges: [],
          ui: {}
        })
      });
    } else {
      // Fallback for other/unhandled API calls
      await route.continue();
    }
  });
});

test.describe("Graph Studio - Workflow Flows", () => {
  test.beforeEach(async ({ page }) => {
    // Configure boot configs to edit mode
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

  test("should import .abhi file successfully", async ({ page }) => {
    // Intercept preview-import and import APIs
    await page.route("**/api/graph/abhi/preview-import", async (route) => {
      expect(route.request().method()).toBe("POST");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          snapshot: {
            nodes: [
              { id: "imported-1", label: "Imported Node 1" },
              { id: "imported-2", label: "Imported Node 2" }
            ],
            edges: []
          },
          validation: { valid: true, errors: [] }
        })
      });
    });

    await page.route("**/api/graph/import", async (route) => {
      expect(route.request().method()).toBe("POST");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          imported_node_ids: ["imported-1", "imported-2"]
        })
      });
    });

    // Upload mock file using input files API on the hidden file input
    const fileInputLocator = page.locator('label:has-text("Import preview") input');
    await fileInputLocator.setInputFiles({
      name: "mock-memory.abhi",
      mimeType: "application/octet-stream",
      buffer: Buffer.from("mock binary contents")
    });

    // Assert that the preview area shows correct info
    await expect(page.locator("text=Import preview")).toBeVisible();
    await expect(page.locator("text=2 nodes · 0 edges")).toBeVisible();
    await expect(page.locator("text=Imported Node 1")).toBeVisible();

    // Click "Commit import" button
    await page.click('button:has-text("Commit import")');

    // Verify success toast/status message
    await expect(page.locator("text=Imported graph data.")).toBeVisible();
  });

  test("should export graph successfully", async ({ page }) => {
    // Mock export API response
    await page.route("**/api/graph/export**", async (route) => {
      expect(route.request().method()).toBe("GET");
      await route.fulfill({
        status: 200,
        contentType: "application/octet-stream",
        body: Buffer.from("mock export contents")
      });
    });

    // Trigger export and wait for download event
    const downloadPromise = page.waitForEvent("download");
    await page.click('button:has-text("Export")');
    const download = await downloadPromise;

    // Verify filename
    expect(download.suggestedFilename()).toBe("waggle-memory.abhi");
  });

  test("should search and display retrieval debug details", async ({ page }) => {
    // Navigate to Retrieval tab
    await page.click('button:has-text("Retrieval")');

    // Fill query
    const textarea = page.locator("textarea");
    await textarea.fill("find relevant documents");

    // Mock retrieval-debug API response
    await page.route("**/api/graph/retrieval-debug", async (route) => {
      expect(route.request().method()).toBe("POST");
      const body = JSON.parse(route.request().postData() || "{}");
      expect(body.query).toBe("find relevant documents");

      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          debug: {
            flat_top_nodes: [
              {
                node_id: "n-1",
                label: "Debugger Node 1",
                final_score: 0.95,
                similarity_score: 0.85,
                recency_score: 0.90
              }
            ],
            all_windows: [
              {
                window_id: "w-1",
                title: "Debugger Window 1",
                routing_score: 0.80,
                similarity: 0.70,
                recency: 0.90
              }
            ]
          },
          replay_hits: [
            {
              role: "user",
              score: 0.75,
              session_id: "session-1",
              turn_index: 2,
              transcript_snippet: "This is a transcript snippet"
            }
          ],
          fusion_hits: [
            {
              fused_rank: 1,
              content: "Fused result 1",
              source_lane: "hybrid",
              graph_rank: 2,
              replay_rank: 1,
              score: 0.88,
              reasoning: "Reasoning for fusion 1"
            }
          ],
          token_estimate: 120
        })
      });
    });

    // Run debugger
    await page.click('button:has-text("Run debugger")');

    // Assert debugger output fields are rendered
    await expect(page.locator("text=Debugger Node 1")).toBeVisible();
    await expect(page.locator("text=Debugger Window 1")).toBeVisible();
    await expect(page.locator("text=This is a transcript snippet")).toBeVisible();
    await expect(page.locator("text=Fused result 1")).toBeVisible();
    await expect(page.locator("text=120 tokens")).toBeVisible();
  });

  test("should search transcripts and filter them correctly", async ({ page }) => {
    // Navigate to Transcripts tab
    await page.click('button:has-text("Transcripts")');

    // Fill transcript search input
    const searchInput = page.locator('input[placeholder="Search transcripts (hybrid BM25 + vector)"]');
    await searchInput.fill("filter query");

    // Mock search API response
    await page.route("**/api/graph/transcripts**", async (route) => {
      const url = route.request().url();
      expect(url).toContain("query=filter+query");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          hits: [
            {
              id: "t-hit",
              session_id: "test-session",
              project: "test-project",
              agent_id: "test-agent",
              turn_index: 5,
              role: "assistant",
              transcript_text: "Filtered transcript result",
              observed_at: "2026-06-13T08:05:00Z"
            }
          ]
        })
      });
    });

    // Click Search button
    await page.click('button:has-text("Search")');

    // Verify search results are displayed
    await expect(page.locator("text=Filtered transcript result")).toBeVisible();
  });
});
