import { describe, it, expect } from "vitest";
import { buildFilterBuckets, filterGraph } from "./graph-utils.js";

const DUMMY_SOURCE = { id: "test_src", label: "Test Source", color: "#000" };

describe("graph-utils - buildFilterBuckets", () => {
  it("extracts and counts tags, sorting by frequency", () => {
    const nodes = [
      { tags: ["a", "b"], source: DUMMY_SOURCE },
      { tags: ["a", "b", "c"], source: DUMMY_SOURCE },
      { tags: ["b"], source: DUMMY_SOURCE }
    ];
    const buckets = buildFilterBuckets(nodes);
    expect(buckets.tags).toEqual([
      { id: "b", label: "b", count: 3 },
      { id: "a", label: "a", count: 2 },
      { id: "c", label: "c", count: 1 }
    ]);
  });

  it("extracts sessions, projects, agents from nodes and transcripts and deduplicates", () => {
    const nodes = [
      { session_id: "s1", project: "p1", agent_id: "a1", source: DUMMY_SOURCE },
      { session_id: "s1", project: "p2", agent_id: "a2", source: DUMMY_SOURCE }
    ];
    const transcripts = [
      { session_id: "s2", project: "p1", agent_id: "a2" }
    ];
    const buckets = buildFilterBuckets(nodes, transcripts);
    
    expect(buckets.sessions).toEqual([
      { id: "s1", label: "s1", count: 2 },
      { id: "s2", label: "s2", count: 1 }
    ]);
    
    expect(buckets.projects).toEqual([
      { id: "p1", label: "p1", count: 2 },
      { id: "p2", label: "p2", count: 1 }
    ]);

    expect(buckets.agents).toEqual([
      { id: "a2", label: "a2", count: 2 },
      { id: "a1", label: "a1", count: 1 }
    ]);
  });

  it("extracts sources correctly and counts them", () => {
    const nodes = [
      { source: { id: "codex", label: "Codex", color: "#6bdcff" } },
      { source: { id: "claude", label: "Claude", color: "#f4c06c" } },
      { source: { id: "codex", label: "Codex", color: "#6bdcff" } }
    ];
    const buckets = buildFilterBuckets(nodes);
    expect(buckets.sources).toEqual([
      { id: "codex", label: "Codex", color: "#6bdcff", count: 2 },
      { id: "claude", label: "Claude", color: "#f4c06c", count: 1 }
    ]);
  });

  it("returns empty arrays if no valid data is present", () => {
    const buckets = buildFilterBuckets([], []);
    expect(buckets.tags).toEqual([]);
    expect(buckets.sessions).toEqual([]);
    expect(buckets.agents).toEqual([]);
    expect(buckets.projects).toEqual([]);
    expect(buckets.sources).toEqual([]);
  });
});

describe("graph-utils - filterGraph", () => {
  const baseNodes = [
    {
      id: "1",
      label: "Find bugs",
      content: "Looking for null pointers",
      node_type: "decision",
      tags: ["urgent", "bug"],
      session_id: "s1",
      agent_id: "a1",
      project: "p1",
      source: { id: "codex", label: "Codex" },
      updated_at: new Date(Date.now() - 2 * 24 * 60 * 60 * 1000).toISOString() // 2 days ago
    },
    {
      id: "2",
      label: "Fix CSS",
      content: "Update button color",
      node_type: "task",
      tags: ["ui"],
      session_id: "s2",
      agent_id: "a2",
      project: "p2",
      source: { id: "claude", label: "Claude" },
      updated_at: new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString() // 10 days ago
    }
  ];

  const baseEdges = [
    { id: "e1", source_id: "1", target_id: "2" }
  ];

  const defaultFilters = {
    search: "",
    tags: [],
    sessions: [],
    sources: [],
    agents: [],
    projects: [],
    dateRange: "all"
  };

  it("returns all nodes and edges when no filters are applied", () => {
    const graph = { nodes: baseNodes, edges: baseEdges };
    const result = filterGraph(graph, defaultFilters);
    expect(result.nodes).toHaveLength(2);
    expect(result.edges).toHaveLength(1);
  });

  it("filters by search text in node fields", () => {
    const graph = { nodes: baseNodes, edges: baseEdges };
    // match content
    const result1 = filterGraph(graph, { ...defaultFilters, search: "null pointer" });
    expect(result1.nodes).toHaveLength(1);
    expect(result1.nodes[0].id).toBe("1");
    // edge is dropped because node 2 is filtered out
    expect(result1.edges).toHaveLength(0);

    // match label
    const result2 = filterGraph(graph, { ...defaultFilters, search: "fix css" });
    expect(result2.nodes).toHaveLength(1);
    expect(result2.nodes[0].id).toBe("2");
  });

  it("filters by tags, sessions, sources, agents, projects", () => {
    const graph = { nodes: baseNodes, edges: baseEdges };
    
    let result = filterGraph(graph, { ...defaultFilters, tags: ["bug"] });
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("1");

    result = filterGraph(graph, { ...defaultFilters, sessions: ["s2"] });
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("2");

    result = filterGraph(graph, { ...defaultFilters, sources: ["codex"] });
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("1");

    result = filterGraph(graph, { ...defaultFilters, agents: ["a2"] });
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("2");

    result = filterGraph(graph, { ...defaultFilters, projects: ["p1"] });
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("1");
  });

  it("filters by date range", () => {
    const graph = { nodes: baseNodes, edges: baseEdges };
    
    // "7d" should match node 1 (2 days ago), but not node 2 (10 days ago)
    let result = filterGraph(graph, { ...defaultFilters, dateRange: "7d" });
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].id).toBe("1");

    // "30d" should match both
    result = filterGraph(graph, { ...defaultFilters, dateRange: "30d" });
    expect(result.nodes).toHaveLength(2);
  });

  it("combinations of multiple filters use AND logic", () => {
    const graph = { nodes: baseNodes, edges: baseEdges };
    
    const result1 = filterGraph(graph, { ...defaultFilters, projects: ["p1"], tags: ["bug"] });
    expect(result1.nodes).toHaveLength(1);
    expect(result1.nodes[0].id).toBe("1");

    const result2 = filterGraph(graph, { ...defaultFilters, projects: ["p1"], tags: ["ui"] });
    expect(result2.nodes).toHaveLength(0);
  });
});
