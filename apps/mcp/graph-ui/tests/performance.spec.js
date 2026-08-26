import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  // Mock the API calls so the tests are 100% deterministic and do not depend on a running server.
  await page.route("**/api/graph**", async (route) => {
    const url = route.request().url();
    if (url.includes("/transcripts")) {
      const records = [];
      for (let i = 0; i < 1000; i++) {
        records.push({
          id: `t-${i}`,
          session_id: "perf-session",
          project: "perf-project",
          agent_id: "perf-agent",
          turn_index: i,
          role: i % 2 === 0 ? "user" : "assistant",
          transcript_text: `This is dynamic transcript message number ${i} to test virtualization performance.`,
          observed_at: new Date(new Date("2026-06-13T08:00:00Z").getTime() - i * 60000).toISOString()
        });
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          records,
          pagination: {
            offset: 0,
            total_count: 1000
          }
        })
      });
    } else {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          tenant_id: "perf-tenant",
          nodes: [],
          edges: [],
          ui: {}
        })
      });
    }
  });
});

test.describe("Graph UI - Virtualization Performance", () => {
  test.beforeEach(async ({ page }) => {
    // Set the boot config to view mode with mock performance scope
    await page.addInitScript(() => {
      window.__WAGGLE_GRAPH_CONFIG__ = {
        mode: "view",
        sampleMode: false,
        project: "perf-project",
        agent_id: "perf-agent",
        session_id: "perf-session"
      };
    });
    await page.goto("/graph");
  });

  test("should render only a small subset of the 1000 transcripts in the DOM", async ({ page }) => {
    // Switch to transcripts tab
    await page.click('button:has-text("Transcripts")');

    // Wait for the transcripts container search input to confirm render
    await expect(page.locator('input[placeholder*="Search transcripts"]')).toBeVisible();

    // Verify list responsiveness through the transcript-card UI contract rather
    // than coupling this performance check to Tailwind's escaped class names.
    const cardLocators = page.getByTestId("transcript-card");
    const totalVisibleCards = await cardLocators.count();

    console.log(`Total visible transcript cards: ${totalVisibleCards}`);

    // Out of 1000 records, only the visible window + buffer (typically < 30) should be rendered in the DOM
    expect(totalVisibleCards).toBeLessThan(100);
    expect(totalVisibleCards).toBeGreaterThan(0);
  });
});
