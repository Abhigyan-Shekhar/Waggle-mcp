const PREVIEW_TERMS = ["storage", "local-first", "approval", "webmcp", "authoritative", "governance"];

function searchable(node) {
  return `${node.label || ""} ${node.content || ""} ${(node.tags || []).join(" ")}`.toLowerCase();
}

function previewScore(node) {
  const text = searchable(node);
  return PREVIEW_TERMS.reduce(
    (score, term, index) => score + (text.includes(term) ? PREVIEW_TERMS.length - index : 0),
    node.node_type === "decision" ? 2 : 0,
  );
}

function isSuperseded(node, edges) {
  return Boolean(node.valid_to) || edges.some(
    (edge) => edge.relationship === "updates" && edge.target_id === node.id,
  );
}

export function selectMemoryMapPreview(snapshot, { focusMemoryId = "", limit = 6 } = {}) {
  const nodes = snapshot?.nodes || [];
  const edges = snapshot?.edges || [];
  if (!nodes.length || limit < 1) return { nodes: [], edges: [] };

  const byId = new Map(nodes.map((node) => [node.id, node]));
  const selected = [];
  const selectedIds = new Set();
  const add = (node) => {
    if (!node || selectedIds.has(node.id) || selected.length >= limit) return false;
    selected.push(node);
    selectedIds.add(node.id);
    return true;
  };

  const fallback = nodes
    .filter((node) => !isSuperseded(node, edges))
    .map((node, index) => ({ node, index, score: previewScore(node) }))
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .map(({ node }) => node);
  const seed = byId.get(focusMemoryId) || fallback[0];
  add(seed);

  if (focusMemoryId && byId.has(focusMemoryId)) {
    const queue = [focusMemoryId];
    const visited = new Set(queue);
    while (queue.length && selected.length < limit) {
      const current = queue.shift();
      for (const edge of edges) {
        const neighborId = edge.source_id === current
          ? edge.target_id
          : edge.target_id === current
            ? edge.source_id
            : "";
        if (!neighborId || visited.has(neighborId) || !byId.has(neighborId)) continue;
        visited.add(neighborId);
        queue.push(neighborId);
        add(byId.get(neighborId));
        if (selected.length >= limit) break;
      }
    }
  }

  for (const node of fallback) add(node);

  return {
    nodes: selected,
    edges: edges.filter(
      (edge) => selectedIds.has(edge.source_id) && selectedIds.has(edge.target_id),
    ),
  };
}
