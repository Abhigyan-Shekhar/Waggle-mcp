const SOURCE_RULES = [
  { id: "codex", label: "Codex", color: "#6bdcff", matchers: ["codex"] },
  { id: "claude", label: "Claude", color: "#f4c06c", matchers: ["claude", "anthropic"] },
  { id: "cursor", label: "Cursor", color: "#b696ff", matchers: ["cursor"] },
  { id: "chatgpt", label: "ChatGPT", color: "#79f2a0", matchers: ["chatgpt", "openai"] },
  { id: "gemini", label: "Gemini", color: "#ff9f7d", matchers: ["gemini", "google"] },
  { id: "graphify", label: "Graphify", color: "#22d3ee", matchers: ["graphify"] },
  { id: "imported", label: "Imported", color: "#ff8bd1", matchers: ["imported"] }
];

// Palette cycled for community coloring. Index by (community_id % length).
const COMMUNITY_COLORS = [
  "#6bdcff",
  "#f4c06c",
  "#79f2a0",
  "#ff9f7d",
  "#b696ff",
  "#ff8bd1",
  "#94e2d5",
  "#fca5a5",
  "#a5b4fc",
  "#fcd34d"
];

export function communitySource(communityId, communityLabel) {
  if (communityId === null || communityId === undefined) {
    return { id: "community:none", label: "Unclustered", color: "#7f8ea3" };
  }
  const color = COMMUNITY_COLORS[communityId % COMMUNITY_COLORS.length];
  return {
    id: `community:${communityId}`,
    label: communityLabel || `Cluster ${communityId}`,
    color
  };
}

export const GRAPH_TOKENS = {
  colors: {
    background: "#1d1f23",
    panel: "#23262b",
    panelAlt: "#1a1c20",
    border: "#30343b",
    text: "#f3f5f7",
    muted: "#97a1af",
    focus: "#e8eef7",
    edge: "rgba(196, 205, 219, 0.2)",
    edgeActive: "rgba(255, 255, 255, 0.72)",
    importedGlow: "#ff8bd1",
    transcript: "#8ba2bf"
  },
  glow: {
    node: "0 0 18px rgba(107, 220, 255, 0.3)",
    imported: "0 0 18px rgba(255, 139, 209, 0.32)",
    panel: "0 18px 50px rgba(0,0,0,0.35)"
  },
  spacing: {
    canvasPadding: 28,
    pillGap: 8,
    panelRadius: 18
  }
};

export function inferSource(node) {
  const haystack = [
    node.agent_id,
    node.project,
    node.session_id,
    node.label,
    node.content,
    ...(node.tags || [])
  ]
    .join(" ")
    .toLowerCase();

  if ((node.tags || []).includes("imported")) {
    return SOURCE_RULES.find((rule) => rule.id === "imported");
  }

  const match = SOURCE_RULES.find((rule) =>
    rule.matchers.some((matcher) => haystack.includes(matcher))
  );
  return match || { id: "waggle", label: "Waggle", color: "#7f8ea3" };
}

export function normalizeGraph(snapshot, importedNodeIds = [], colorMode = "source") {
  const importedSet = new Set(importedNodeIds);
  const nodes = (snapshot.nodes || []).map((node) => {
    const imported = importedSet.has(node.id) || (node.tags || []).includes("imported");
    let source;
    if (colorMode === "community") {
      source = communitySource(node.community_id, node.community_label);
    } else if (imported) {
      source = SOURCE_RULES.find((rule) => rule.id === "imported");
    } else {
      source = inferSource(node);
    }
    return {
      ...node,
      imported,
      source
    };
  });
  const degree = Object.fromEntries(nodes.map((node) => [node.id, 0]));
  for (const edge of snapshot.edges || []) {
    degree[edge.source_id] = (degree[edge.source_id] || 0) + 1;
    degree[edge.target_id] = (degree[edge.target_id] || 0) + 1;
  }
  return {
    ...snapshot,
    nodes: nodes.map((node) => ({
      ...node,
      degree: degree[node.id] || 0,
      size: 18 + Math.min((degree[node.id] || 0) * 3.25, 28)
    })),
    edges: (snapshot.edges || []).map((edge) => ({
      ...edge,
      label: edge.relationship
    }))
  };
}

export function buildFilterBuckets(nodes, transcripts = []) {
  const tagCounts = new Map();
  const sessionCounts = new Map();
  const sourceCounts = new Map();
  const agentCounts = new Map();
  const projectCounts = new Map();

  for (const node of nodes) {
    for (const tag of node.tags || []) {
      tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1);
    }
    if (node.session_id) {
      sessionCounts.set(node.session_id, (sessionCounts.get(node.session_id) || 0) + 1);
    }
    if (node.agent_id) {
      agentCounts.set(node.agent_id, (agentCounts.get(node.agent_id) || 0) + 1);
    }
    if (node.project) {
      projectCounts.set(node.project, (projectCounts.get(node.project) || 0) + 1);
    }
    sourceCounts.set(node.source.id, {
      id: node.source.id,
      label: node.source.label,
      color: node.source.color,
      count: (sourceCounts.get(node.source.id)?.count || 0) + 1
    });
  }

  for (const record of transcripts) {
    if (record.session_id) {
      sessionCounts.set(record.session_id, (sessionCounts.get(record.session_id) || 0) + 1);
    }
    if (record.agent_id) {
      agentCounts.set(record.agent_id, (agentCounts.get(record.agent_id) || 0) + 1);
    }
    if (record.project) {
      projectCounts.set(record.project, (projectCounts.get(record.project) || 0) + 1);
    }
  }

  const sortCounts = (left, right) => right[1] - left[1];
  return {
    tags: [...tagCounts.entries()].sort(sortCounts).slice(0, 12).map(([id, count]) => ({ id, label: id, count })),
    sessions: [...sessionCounts.entries()].sort(sortCounts).slice(0, 12).map(([id, count]) => ({ id, label: id, count })),
    agents: [...agentCounts.entries()].sort(sortCounts).slice(0, 8).map(([id, count]) => ({ id, label: id, count })),
    projects: [...projectCounts.entries()].sort(sortCounts).slice(0, 8).map(([id, count]) => ({ id, label: id, count })),
    sources: [...sourceCounts.values()].sort((left, right) => right.count - left.count)
  };
}

export function matchesDateRange(item, range) {
  if (!range || range === "all") {
    return true;
  }
  const timestamp = item.updated_at || item.created_at || item.valid_from || item.observed_at;
  if (!timestamp) {
    return range === "all";
  }
  const updatedAt = new Date(timestamp).getTime();
  if (Number.isNaN(updatedAt)) {
    return true;
  }
  const now = Date.now();
  const days = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90
  }[range];
  return days ? now - updatedAt <= days * 24 * 60 * 60 * 1000 : true;
}

export function filterGraph(graph, filters) {
  const search = filters.search.trim().toLowerCase();
  const activeTags = new Set(filters.tags || []);
  const activeSessions = new Set(filters.sessions || []);
  const activeSources = new Set(filters.sources || []);
  const activeAgents = new Set(filters.agents || []);
  const activeProjects = new Set(filters.projects || []);
  const nodes = graph.nodes.filter((node) => {
    const haystack = [
      node.label,
      node.content,
      node.node_type,
      node.source.label,
      ...(node.tags || [])
    ]
      .join(" ")
      .toLowerCase();
    if (search && !haystack.includes(search)) {
      return false;
    }
    if (activeTags.size && !(node.tags || []).some((tag) => activeTags.has(tag))) {
      return false;
    }
    if (activeSessions.size && !activeSessions.has(node.session_id || "")) {
      return false;
    }
    if (activeSources.size && !activeSources.has(node.source.id)) {
      return false;
    }
    if (activeAgents.size && !activeAgents.has(node.agent_id || "")) {
      return false;
    }
    if (activeProjects.size && !activeProjects.has(node.project || "")) {
      return false;
    }
    return matchesDateRange(node, filters.dateRange);
  });

  const visibleIds = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter(
    (edge) => visibleIds.has(edge.source_id) && visibleIds.has(edge.target_id)
  );
  return { nodes, edges };
}

function pairIdForRecord(record) {
  return `${record.session_id || "default"}:pair:${Math.floor((record.turn_index || 0) / 2)}`;
}

export function buildTranscriptPairs(records, graphNodes) {
  const pairs = new Map();
  const nodesByPair = new Map();
  for (const node of graphNodes) {
    for (const evidence of node.evidence_records || []) {
      const key = `${evidence.session_id || node.session_id || "default"}:pair:${Math.floor((evidence.turn_index || 0) / 2)}`;
      const list = nodesByPair.get(key) || [];
      list.push(node.id);
      nodesByPair.set(key, list);
    }
  }

  for (const record of records) {
    const pairId = pairIdForRecord(record);
    const current = pairs.get(pairId) || {
      id: pairId,
      label: "",
      project: record.project || "",
      agent_id: record.agent_id || "",
      session_id: record.session_id || "",
      observed_at: record.observed_at,
      pair_index: Math.floor((record.turn_index || 0) / 2),
      transcripts: [],
      derivedNodeIds: []
    };
    current.transcripts.push(record);
    current.label = current.label || `${record.role}: ${record.transcript_text.slice(0, 48)}`;
    current.observed_at = current.observed_at < record.observed_at ? current.observed_at : record.observed_at;
    pairs.set(pairId, current);
  }

  const result = [...pairs.values()]
    .map((pair) => ({
      ...pair,
      transcripts: [...pair.transcripts].sort((left, right) => left.turn_index - right.turn_index),
      derivedNodeIds: [...new Set(nodesByPair.get(pair.id) || [])]
    }))
    .sort((left, right) => new Date(left.observed_at).getTime() - new Date(right.observed_at).getTime());

  return result;
}

function conversationSummary(pair) {
  return pair.transcripts.map((item) => `${item.role}: ${item.transcript_text}`).join("\n\n");
}

function presetPositionForPair(index) {
  return { x: 180 + index * 220, y: 220 + (index % 2) * 120 };
}

function orbitPosition(anchor, offsetIndex) {
  const angle = (Math.PI * 2 * offsetIndex) / 6;
  return {
    x: anchor.x + Math.cos(angle) * 140,
    y: anchor.y + Math.sin(angle) * 100
  };
}

export function buildLayerGraph({ graph, transcriptPairs, layerMode, highlightedTurnPairId = "", focusedNodeId = "" }) {
  const transcriptElements = [];
  const transcriptEdges = [];
  const pairNodeIds = new Set();
  const graphNodeIds = new Set(graph.nodes.map((node) => node.id));

  transcriptPairs.forEach((pair, index) => {
    const position = presetPositionForPair(index);
    pairNodeIds.add(pair.id);
    transcriptElements.push({
      data: {
        id: pair.id,
        label: pair.label,
        content: conversationSummary(pair),
        nodeKind: "transcript",
        sourceColor: GRAPH_TOKENS.colors.transcript,
        source: "Transcript",
        turnPairId: pair.id,
        size: 44,
        highlight: pair.id === highlightedTurnPairId
      },
      position
    });
    if (index > 0) {
      transcriptEdges.push({
        data: {
          id: `chain:${transcriptPairs[index - 1].id}:${pair.id}`,
          source: transcriptPairs[index - 1].id,
          target: pair.id,
          label: "next",
          edgeKind: "conversation-chain"
        }
      });
    }
  });

  if (layerMode === "conversation") {
    return {
      elements: [...transcriptElements, ...transcriptEdges],
      layout: { name: "preset", fit: true, padding: GRAPH_TOKENS.spacing.canvasPadding }
    };
  }

  const graphElements = graph.nodes.map((node) => ({
    data: {
      id: node.id,
      label: node.label,
      content: node.content,
      sourceColor: node.source.color,
      source: node.source.label,
      degree: node.degree,
      size: node.size,
      nodeKind: "graph",
      imported: node.imported,
      turnPairId: firstTurnPairId(node),
      highlight: node.id === focusedNodeId
    }
  }));

  const semanticEdges = graph.edges
    .filter((edge) => graphNodeIds.has(edge.source_id) && graphNodeIds.has(edge.target_id))
    .map((edge) => ({
      data: {
        id: edge.id,
        source: edge.source_id,
        target: edge.target_id,
        label: edge.relationship,
        edgeKind: edge.relationship === "derived_from" ? "derived_from" : "semantic",
        relationship: edge.relationship,
        confidence: edge.confidence || "explicit"
      }
    }));

  if (layerMode === "graph") {
    return {
      elements: [...graphElements, ...semanticEdges],
      layout: {
        name: "cose-bilkent",
        animate: false,
        fit: true,
        padding: GRAPH_TOKENS.spacing.canvasPadding,
        idealEdgeLength: 130,
        nodeRepulsion: 800000,
        gravity: 0.22,
        tile: false
      }
    };
  }

  const positions = new Map(transcriptElements.map((item) => [item.data.id, item.position]));
  const bothNodes = [...transcriptElements];
  const derivedEdges = [];
  const pairDerivedCounts = new Map();
  for (const node of graph.nodes) {
    const turnPairId = resolveFirstTurnPairId(node, pairNodeIds);
    const anchor = positions.get(turnPairId) || { x: 180, y: 220 };
    const nextOffset = pairDerivedCounts.get(turnPairId) || 0;
    pairDerivedCounts.set(turnPairId, nextOffset + 1);
    bothNodes.push({
      data: {
        id: node.id,
        label: node.label,
        content: node.content,
        sourceColor: node.source.color,
        source: node.source.label,
        degree: node.degree,
        size: node.size,
        nodeKind: "graph",
        imported: node.imported,
        turnPairId,
        highlight: node.id === focusedNodeId || turnPairId === highlightedTurnPairId
      },
      position: orbitPosition(anchor, nextOffset)
    });
    if (turnPairId && pairNodeIds.has(turnPairId)) {
      derivedEdges.push({
        data: {
          id: `derived:${node.id}:${turnPairId}`,
          source: node.id,
          target: turnPairId,
          label: "derived_from",
          edgeKind: "derived_from",
          relationship: "derived_from"
        }
      });
    }
  }

  const allNodes = [...bothNodes];
  const renderNodeIds = new Set(allNodes.map((item) => item.data.id));
  const renderEdges = [...semanticEdges, ...transcriptEdges, ...derivedEdges].filter(
    (edge) => renderNodeIds.has(edge.data.source) && renderNodeIds.has(edge.data.target)
  );

  return {
    elements: [...allNodes, ...renderEdges],
    layout: { name: "preset", fit: true, padding: GRAPH_TOKENS.spacing.canvasPadding }
  };
}

function resolveFirstTurnPairId(node, validPairIds = null) {
  for (const evidence of node.evidence_records || []) {
    const pairId = `${evidence.session_id || node.session_id || "default"}:pair:${Math.floor((evidence.turn_index || 0) / 2)}`;
    if (!validPairIds || validPairIds.has(pairId)) {
      return pairId;
    }
  }
  return "";
}

export function firstTurnPairId(node) {
  return resolveFirstTurnPairId(node, null);
}

export function buildProvenanceTrail(node, graph) {
  const byId = new Map(graph.nodes.map((item) => [item.id, item]));
  const sourceEdges = graph.edges.filter(
    (edge) => edge.relationship === "derived_from" && edge.source_id === node.id
  );
  return sourceEdges
    .map((edge) => byId.get(edge.target_id))
    .filter(Boolean);
}

export function buildNodeEdgeList(nodeId, graph) {
  const byId = new Map(graph.nodes.map((item) => [item.id, item]));
  return graph.edges
    .filter((edge) => edge.source_id === nodeId || edge.target_id === nodeId)
    .map((edge) => ({
      ...edge,
      sourceLabel: byId.get(edge.source_id)?.label || edge.source_id,
      targetLabel: byId.get(edge.target_id)?.label || edge.target_id,
    }));
}

export function summarizeSourcePrompts(node) {
  const values = [];
  if (node.source_prompt) {
    values.push(node.source_prompt);
  }
  for (const record of node.evidence_records || []) {
    if (record.source_text) {
      values.push(record.source_text);
    }
  }
  return [...new Set(values.filter(Boolean))];
}

export function buildExtractionHealth(transcriptPairs) {
  const total = transcriptPairs.length;
  const zeroPairs = transcriptPairs.filter((pair) => !pair.derivedNodeIds.length);
  const produced = total - zeroPairs.length;
  return {
    total,
    produced,
    percent: total ? Math.round((produced / total) * 100) : 0,
    zeroPairs
  };
}

export function buildRestorePayload(graph, scope) {
  return {
    project: scope.project,
    agent_id: scope.agent_id,
    session_id: scope.session_id,
    nodes: graph.nodes,
    edges: graph.edges,
    ui: graph.ui || {}
  };
}
