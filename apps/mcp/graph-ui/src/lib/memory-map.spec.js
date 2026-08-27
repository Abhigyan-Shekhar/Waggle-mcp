import { describe, expect, it } from "vitest";
import { selectMemoryMapPreview } from "./memory-map.js";

const storageOld = { id: "storage-v1", label: "Storage architecture", content: "Use Neo4j.", node_type: "decision", valid_to: "2026-08-26" };
const storageNew = { id: "storage-v2", label: "Storage architecture", content: "Use SQLite by default.", node_type: "decision", valid_to: null };
const localFirst = { id: "local-first", label: "Local-first direction", content: "Preserve a local-first default.", node_type: "decision", valid_to: null };
const approval = { id: "approval", label: "Human approval boundary", content: "Humans approve changes.", node_type: "decision", valid_to: null };
const webmcp = { id: "webmcp", label: "WebMCP integration", content: "Expose governed tools.", node_type: "fact", valid_to: null };
const unrelated = { id: "unrelated", label: "License", content: "Apache-2.0.", node_type: "fact", valid_to: null };
const update = { source_id: "storage-v2", target_id: "storage-v1", relationship: "updates" };
const supports = { source_id: "local-first", target_id: "storage-v2", relationship: "supports" };
const enables = { source_id: "approval", target_id: "webmcp", relationship: "enables" };
const snapshot = {
  nodes: [unrelated, storageOld, localFirst, approval, webmcp, storageNew],
  edges: [update, supports, enables],
};

describe("selectMemoryMapPreview", () => {
  it("returns focused lineage using only supplied graph objects", () => {
    const result = selectMemoryMapPreview(snapshot, { focusMemoryId: "storage-v2", limit: 4 });

    expect(result.nodes.map((node) => node.id)).toEqual(["storage-v2", "storage-v1", "local-first", "approval"]);
    expect(result.edges).toEqual([update, supports]);
    expect(result.nodes.every((node) => snapshot.nodes.includes(node))).toBe(true);
    expect(result.edges.every((edge) => snapshot.edges.includes(edge))).toBe(true);
  });

  it("prefers the current storage decision when no focus is supplied", () => {
    const result = selectMemoryMapPreview(snapshot, { limit: 3 });

    expect(result.nodes[0]).toBe(storageNew);
    expect(result.nodes).not.toContain(storageOld);
  });

  it("returns an empty preview for an empty graph", () => {
    expect(selectMemoryMapPreview({ nodes: [], edges: [] }, { limit: 6 })).toEqual({ nodes: [], edges: [] });
  });
});
