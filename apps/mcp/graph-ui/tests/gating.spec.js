import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  // Mock the API calls so the tests are 100% deterministic and do not depend on a running server
  await page.route("**/api/graph**", async (route) => {
    const url = route.request().url();
    if (url.includes("/transcripts")) {
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
    } else {
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
              source_prompt: "user: Help me write a document.",
              evidence_records: [
                {
                  evidence_id: "ev-1",
                  session_id: "test-session",
                  turn_index: 1,
                  source_role: "assistant",
                  source_text: "Document created successfully.",
                  observed_at: "2026-06-13T08:01:00Z"
                }
              ],
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
    }
  });
});

test.describe("Graph UI - Gating in View Mode", () => {
  test.beforeEach(async ({ page }) => {
    // Set the boot config to view mode
    await page.addInitScript(() => {
      window.__WAGGLE_GRAPH_CONFIG__ = {
        mode: "view",
        sampleMode: false,
        project: "test-project",
        agent_id: "test-agent",
        session_id: "test-session"
      };
    });
    await page.goto("/");
  });

  test("should display view mode indicator and disable graph mutations", async ({ page }) => {
    // Verify view mode header text
    await expect(page.locator("text=View mode")).toBeVisible();

    // Verify canvas action buttons are disabled
    await expect(page.locator('button:has-text("New node")')).toBeDisabled();
    await expect(page.locator('button:has-text("Undo")')).toBeDisabled();
    await expect(page.locator('button:has-text("Redo")')).toBeDisabled();

    // Verify import button is disabled
    await expect(page.locator('label:has-text("Import preview") input')).toBeDisabled();

    // Switch to transcripts tab to select a node and verify inspector
    await page.click('button:has-text("Transcripts")');
    await page.click('button:has-text("Show in graph")');

    // Inspector should now open for node-1
    await expect(page.locator('input[name="label"]')).toBeDisabled();
    await expect(page.locator('textarea[name="content"]')).toBeDisabled();
    await expect(page.locator('input[name="tags"]')).toBeDisabled();

    // Inspector buttons should be disabled
    await expect(page.locator('button:has-text("Save node")')).toBeDisabled();
    await expect(page.locator('button:has-text("Delete")')).toBeDisabled();
  });
});

test.describe("Graph UI - Allowed Actions in Edit Mode", () => {
  test.beforeEach(async ({ page }) => {
    // Set the boot config to edit mode
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

  test("should display edit mode indicator and allow graph mutations", async ({ page }) => {
    // Verify edit mode header text
    await expect(page.locator("text=Edit mode")).toBeVisible();

    // Verify canvas action buttons are enabled
    await expect(page.locator('button:has-text("New node")')).toBeEnabled();

    // Verify import button is enabled
    await expect(page.locator('label:has-text("Import preview") input')).toBeEnabled();

    // Switch to transcripts tab to select a node and verify inspector
    await page.click('button:has-text("Transcripts")');
    await page.click('button:has-text("Show in graph")');

    // Inspector should now open for node-1
    await expect(page.locator('input[name="label"]')).toBeEnabled();
    await expect(page.locator('textarea[name="content"]')).toBeEnabled();
    await expect(page.locator('input[name="tags"]')).toBeEnabled();

    // Inspector buttons should be enabled
    await expect(page.locator('button:has-text("Save node")')).toBeEnabled();
    await expect(page.locator('button:has-text("Delete")')).toBeEnabled();
  });
});
