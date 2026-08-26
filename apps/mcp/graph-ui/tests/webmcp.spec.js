import { expect, test } from "@playwright/test";

test("registers and executes Waggle WebMCP tools from the live page", async ({ page }) => {
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
  const proposal = {
    proposal_id: "proposal_123",
    status: "pending",
    project_id: "waggle-webmcp",
    target: {
      memory_id: "memory-v3",
      current_content: "Use Neo4j for storage.",
      version: "target-fingerprint",
    },
    proposed_content: "Use SQLite by default; Neo4j remains optional.",
    reason: "Preserve local-first architecture.",
    evidence_ids: [],
    proposed_by: { type: "agent", id: "webmcp" },
    created_at: "2026-08-26T00:00:00+00:00",
    reviewed_at: null,
    reviewed_by: "",
    review_note: "",
    approved_content: null,
    applied_at: null,
    result_memory_id: null,
  };
  let persistedProposals = [];
  const auditEvents = [
    {
      event_id: "audit-brief",
      event_type: "webmcp.project_brief.read",
      actor_id: "webmcp",
      created_at: "2026-08-26T14:31:00+00:00",
      metadata: {},
    },
  ];

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
  await page.route("**/api/admin/audit-events**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(auditEvents),
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
  await page.route("**/api/webmcp/proposals**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          project_id: "waggle-webmcp",
          proposals: persistedProposals,
        }),
      });
      return;
    }
    if (url.pathname.endsWith("/review")) {
      expect(route.request().postDataJSON()).toEqual({
        action: "approve",
        approved_content: "Use SQLite by default; Neo4j remains optional and auditable.",
      });
      persistedProposals = [
        {
          ...proposal,
          status: "approved",
          reviewed_at: "2026-08-26T00:01:00+00:00",
          reviewed_by: "local-human",
          approved_content: "Use SQLite by default; Neo4j remains optional and auditable.",
        },
      ];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(persistedProposals[0]),
      });
      return;
    }
    if (url.pathname.endsWith("/apply")) {
      expect(route.request().postDataJSON()).toEqual({ project_id: "waggle-webmcp" });
      const appliedProposal = {
        ...persistedProposals[0],
        status: "applied",
        applied_at: "2026-08-26T00:02:00+00:00",
        result_memory_id: "memory-v4",
      };
      persistedProposals = [appliedProposal];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          proposal_id: proposal.proposal_id,
          status: "applied",
          authoritative_memory: {
            memory_id: "memory-v4",
            content: appliedProposal.approved_content,
          },
          already_applied: false,
          proposal: appliedProposal,
        }),
      });
      return;
    }
    expect(route.request().postDataJSON()).toEqual({
      project_id: "waggle-webmcp",
      memory_id: "memory-v3",
      proposed_content: "Use SQLite by default; Neo4j remains optional.",
      reason: "Preserve local-first architecture.",
      evidence_ids: [],
    });
    persistedProposals = [proposal];
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(proposal),
    });
  });

  await page.goto("/");
  await expect
    .poll(() =>
      page.evaluate(() => window.__registeredSiteTools.map((tool) => tool.name)),
    )
    .toEqual([
      "get_project_brief",
      "recall_memory",
      "propose_memory_change",
      "apply_approved_memory_change",
    ]);

  const briefResult = await page.evaluate(() =>
    window.__registeredSiteTools
      .find((tool) => tool.name === "get_project_brief")
      .execute({ project_id: "waggle-webmcp" }),
  );
  expect(briefResult).toEqual(brief);
  await expect(page.getByText("Project brief shared with ChatGPT.")).toBeVisible();

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
    page.getByText("ChatGPT recalled 1 authoritative memory."),
  ).toBeVisible();

  const proposalResult = await page.evaluate(() =>
    window.__registeredSiteTools
      .find((tool) => tool.name === "propose_memory_change")
      .execute({
        project_id: "waggle-webmcp",
        memory_id: "memory-v3",
        proposed_content: "Use SQLite by default; Neo4j remains optional.",
        reason: "Preserve local-first architecture.",
      }),
  );
  expect(proposalResult).toEqual(proposal);
  await expect(page.getByRole("heading", { name: "Memory correction" })).toBeVisible();
  await expect(page.getByText("Use Neo4j for storage.")).toBeVisible();
  await expect(
    page.getByText("Use SQLite by default; Neo4j remains optional."),
  ).toBeVisible();
  await expect(page.getByText("pending review", { exact: true })).toBeVisible();
  await expect(
    page.getByText("A new proposal is ready for human review."),
  ).toBeVisible();

  await page.getByRole("button", { name: "Edit & Approve" }).click();
  await page.getByLabel("Human-approved content").fill(
    "Use SQLite by default; Neo4j remains optional and auditable.",
  );
  await page.getByRole("button", { name: "Confirm edit & approve" }).click();
  await expect(page.getByText("Human approved", { exact: true })).toBeVisible();
  await expect(page.getByText("Awaiting application by ChatGPT")).toBeVisible();

  const appliedResult = await page.evaluate(() =>
    window.__registeredSiteTools
      .find((tool) => tool.name === "apply_approved_memory_change")
      .execute({ proposal_id: "proposal_123" }),
  );
  expect(appliedResult.already_applied).toBe(false);
  expect(appliedResult.authoritative_memory.content).toBe(
    "Use SQLite by default; Neo4j remains optional and auditable.",
  );
  await expect(page.locator(".workspace-status-applied")).toBeVisible();
  await expect(page.getByText("Authoritative", { exact: true })).toBeVisible();

  await page.reload();
  await page.goto("/workspace/proposals");
  await expect(page.getByRole("heading", { name: "Memory correction" })).toBeVisible();
  await expect(page.locator(".workspace-status-applied")).toBeVisible();
  await expect(
    page.getByText("Use SQLite by default; Neo4j remains optional and auditable."),
  ).toBeVisible();
});
