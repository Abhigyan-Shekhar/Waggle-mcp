import React, { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import Cytoscape from "cytoscape";
import coseBilkent from "cytoscape-cose-bilkent";
import { apiRequest, buildScopeQuery } from "./lib/api";
import {
  buildExtractionHealth,
  buildFilterBuckets,
  buildLayerGraph,
  buildNodeEdgeList,
  buildProvenanceTrail,
  buildRestorePayload,
  buildTranscriptPairs,
  filterGraph,
  firstTurnPairId,
  GRAPH_TOKENS,
  normalizeGraph,
  summarizeSourcePrompts
} from "./lib/graph-utils";
import { SAMPLE_GRAPH_SNAPSHOT, SAMPLE_RETRIEVAL, SAMPLE_TRANSCRIPTS } from "./sample-data";

Cytoscape.use(coseBilkent);

const DATE_RANGES = [
  { id: "24h", label: "24h" },
  { id: "7d", label: "7d" },
  { id: "30d", label: "30d" },
  { id: "90d", label: "90d" },
  { id: "all", label: "All time" }
];

const RELATION_TYPES = ["relates_to", "contradicts", "depends_on", "part_of", "updates", "derived_from", "similar_to"];

function getBootConfig() {
  const config = window.__WAGGLE_GRAPH_CONFIG__ || {};
  return {
    mode: config.mode === "view" ? "view" : "edit",
    sampleMode: Boolean(config.sampleMode),
    scope: {
      project: config.project || "",
      agent_id: config.agent_id || "",
      session_id: config.session_id || ""
    }
  };
}

function Pill({ active, children, color, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition ${
        active ? "border-white/20 bg-white/12 text-white" : "border-white/10 bg-black/15 text-graph-muted hover:bg-white/8"
      }`}
      style={active && color ? { boxShadow: `0 0 0 1px ${color} inset`, color } : undefined}
      type="button"
    >
      {children}
    </button>
  );
}

function Section({ title, children, extra }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-white/[0.04] p-4 panel-shell">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-graph-muted">{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}

function ContextMenu({ menu, onClose, onAction }) {
  useEffect(() => {
    if (!menu) {
      return undefined;
    }
    const close = () => onClose();
    window.addEventListener("click", close);
    window.addEventListener("contextmenu", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("contextmenu", close);
    };
  }, [menu, onClose]);

  return (
    <AnimatePresence>
      {menu ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.96 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.96 }}
          className="fixed z-50 min-w-44 rounded-xl border border-white/10 bg-[#171a1f] p-2 shadow-2xl"
          style={{ left: menu.x, top: menu.y }}
        >
          {menu.actions.map((action) => (
            <button
              key={action.id}
              className="block w-full rounded-lg px-3 py-2 text-left text-sm text-white transition hover:bg-white/8"
              onClick={() => onAction(action.id, menu.nodeId)}
              type="button"
            >
              {action.label}
            </button>
          ))}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function EdgeDialog({ edge, onCancel, onSave }) {
  const [relationship, setRelationship] = useState(edge?.relationship || "relates_to");

  useEffect(() => {
    setRelationship(edge?.relationship || "relates_to");
  }, [edge]);

  return (
    <AnimatePresence>
      {edge ? (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4">
          <motion.div initial={{ y: 12, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 12, opacity: 0 }} className="w-full max-w-sm rounded-2xl border border-white/10 bg-graph-panel p-5 shadow-2xl">
            <h3 className="text-lg font-semibold">Edit edge label</h3>
            <p className="mt-1 text-sm text-graph-muted">This updates the stored relationship type for the edge.</p>
            <select className="mt-4 w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" value={relationship} onChange={(event) => setRelationship(event.target.value)}>
              {RELATION_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <div className="mt-4 flex justify-end gap-2">
              <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={onCancel} type="button">
                Cancel
              </button>
              <button className="rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" onClick={() => onSave(relationship)} type="button">
                Save
              </button>
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

function FileInputButton({ label, accept, onChange, disabled }) {
  return (
    <label
      className={`rounded-xl border border-white/10 px-3 py-2 text-sm text-white ${
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
      }`}
    >
      {label}
      <input className="hidden" type="file" accept={accept} onChange={onChange} disabled={disabled} />
    </label>
  );
}

function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("Failed to read file."));
    reader.readAsText(file);
  });
}

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const [, base64] = result.split(",", 2);
      resolve(base64 || "");
    };
    reader.onerror = () => reject(reader.error || new Error("Failed to read file."));
    reader.readAsDataURL(file);
  });
}

export function App() {
  const boot = useMemo(getBootConfig, []);
  const mode = boot.mode;
  const readOnly = mode === "view";
  const cyRef = useRef(null);
  const hostRef = useRef(null);
  const lastEdgeTapRef = useRef({ id: "", at: 0 });
  const dragStateRef = useRef(null);
  const [scope, setScope] = useState(boot.scope);
  const [snapshot, setSnapshot] = useState(boot.sampleMode ? SAMPLE_GRAPH_SNAPSHOT : { tenant_id: "", nodes: [], edges: [], ui: {} });
  const [transcriptRecords, setTranscriptRecords] = useState(boot.sampleMode ? SAMPLE_TRANSCRIPTS : []);
  const [filters, setFilters] = useState({ search: "", tags: [], sessions: [], sources: [], agents: [], projects: [], dateRange: "all" });
  const [transcriptSearch, setTranscriptSearch] = useState("");
  const [transcriptOffset, setTranscriptOffset] = useState(0);
  const [transcriptTotalCount, setTranscriptTotalCount] = useState(0);
  const [transcriptHits, setTranscriptHits] = useState([]);
  const [retrievalQuery, setRetrievalQuery] = useState("how do transcript provenance and derived nodes interact?");
  const [retrievalResult, setRetrievalResult] = useState(boot.sampleMode ? SAMPLE_RETRIEVAL : null);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [selectedEdgeId, setSelectedEdgeId] = useState("");
  const [hoverNodeId, setHoverNodeId] = useState("");
  const [status, setStatus] = useState("");
  const [menu, setMenu] = useState(null);
  const [edgeDialog, setEdgeDialog] = useState(null);
  const [historyPast, setHistoryPast] = useState([]);
  const [historyFuture, setHistoryFuture] = useState([]);
  const [activeTab, setActiveTab] = useState("graph");
  const [layerMode, setLayerMode] = useState("both");
  const [highlightedTurnPairId, setHighlightedTurnPairId] = useState("");
  const [importedNodeIds, setImportedNodeIds] = useState([]);
  const [importPreview, setImportPreview] = useState(null);
  const [abhiDiff, setAbhiDiff] = useState(null);
  const [showMisses, setShowMisses] = useState(false);

  const graph = useMemo(() => normalizeGraph(snapshot, importedNodeIds), [snapshot, importedNodeIds]);
  const visibleGraph = useMemo(() => filterGraph(graph, filters), [graph, filters]);
  const transcriptPairs = useMemo(() => buildTranscriptPairs(transcriptRecords, graph.nodes), [transcriptRecords, graph.nodes]);
  const extractionHealth = useMemo(() => buildExtractionHealth(transcriptPairs), [transcriptPairs]);
  const buckets = useMemo(() => buildFilterBuckets(graph.nodes, transcriptRecords), [graph.nodes, transcriptRecords]);
  const layerGraph = useMemo(
    () =>
      buildLayerGraph({
        graph: visibleGraph,
        transcriptPairs,
        layerMode,
        highlightedTurnPairId,
        focusedNodeId: selectedNodeId
      }),
    [visibleGraph, transcriptPairs, layerMode, highlightedTurnPairId, selectedNodeId]
  );

  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId) || transcriptPairs.find((pair) => pair.id === selectedNodeId) || null;
  const selectedEdge = graph.edges.find((edge) => edge.id === selectedEdgeId) || null;

  const setToast = (message) => {
    setStatus(message);
    window.clearTimeout(setToast.timer);
    setToast.timer = window.setTimeout(() => setStatus(""), 2400);
  };

  const loadSnapshot = async (nextScope = scope) => {
    if (boot.sampleMode) {
      return;
    }
    const [graphData, transcriptData] = await Promise.all([
      apiRequest(`/api/graph${buildScopeQuery(nextScope)}${buildScopeQuery(nextScope) ? "&" : "?"}include_source_prompt=true`),
      apiRequest(`/api/graph/transcripts${buildScopeQuery(nextScope)}`)
    ]);
    setSnapshot(graphData);
    setTranscriptRecords(transcriptData.records || []);
    setTranscriptOffset(transcriptData.pagination?.offset ?? 0);
    setTranscriptTotalCount(transcriptData.pagination?.total_count ?? 0);
    setSelectedNodeId("");
    setSelectedEdgeId("");
    setHoverNodeId("");
  };

  useEffect(() => {
    loadSnapshot(boot.scope).catch((error) => setToast(error.message));
  }, []);

  const pushHistory = async () => {
    const restorePayload = buildRestorePayload(graph, scope);
    setHistoryPast((current) => [...current, restorePayload].slice(-50));
    setHistoryFuture([]);
  };

  const restoreSnapshot = async (payload) => {
    await apiRequest("/api/graph/restore", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    await loadSnapshot(scope);
  };

  const undo = async () => {
    const previous = historyPast[historyPast.length - 1];
    if (!previous || boot.sampleMode) {
      return;
    }
    const current = buildRestorePayload(graph, scope);
    setHistoryPast((items) => items.slice(0, -1));
    setHistoryFuture((items) => [...items, current]);
    await restoreSnapshot(previous);
    setToast("Undid last graph edit.");
  };

  const redo = async () => {
    const next = historyFuture[historyFuture.length - 1];
    if (!next || boot.sampleMode) {
      return;
    }
    const current = buildRestorePayload(graph, scope);
    setHistoryFuture((items) => items.slice(0, -1));
    setHistoryPast((items) => [...items, current].slice(-50));
    await restoreSnapshot(next);
    setToast("Redid graph edit.");
  };

  useEffect(() => {
    const onKeyDown = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) {
          redo().catch((error) => setToast(error.message));
        } else {
          undo().catch((error) => setToast(error.message));
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [historyPast, historyFuture, graph, scope]);

  useEffect(() => {
    if (!hostRef.current || activeTab !== "graph") {
      return undefined;
    }
    const cy = Cytoscape({
      container: hostRef.current,
      elements: layerGraph.elements,
      layout: layerGraph.layout,
      style: [
        {
          selector: "node",
          style: {
            width: "data(size)",
            height: "data(size)",
            label: "data(label)",
            color: GRAPH_TOKENS.colors.text,
            "font-size": 11,
            "text-wrap": "wrap",
            "text-max-width": 120,
            "text-valign": "center",
            "text-halign": "center",
            "text-outline-color": "#101216",
            "text-outline-width": 2,
            "background-color": "data(sourceColor)",
            "border-width": 1,
            "border-color": "rgba(255,255,255,0.12)",
            shape: "ellipse"
          }
        },
        {
          selector: 'node[nodeKind = "transcript"]',
          style: {
            shape: "rectangle",
            width: 120,
            height: 56,
            "font-size": 10,
            "background-color": "#324054",
            "text-max-width": 110
          }
        },
        {
          selector: 'node[imported = "true"]',
          style: {
            "border-color": GRAPH_TOKENS.colors.importedGlow,
            "border-width": 3,
            "overlay-color": GRAPH_TOKENS.colors.importedGlow,
            "overlay-opacity": 0.16,
            "overlay-padding": 7
          }
        },
        {
          selector: "edge",
          style: {
            width: 1.2,
            "line-color": "rgba(196,205,219,0.25)",
            "target-arrow-color": "rgba(196,205,219,0.25)",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            label: "data(label)",
            "font-size": 9,
            color: "rgba(243,245,247,0.76)",
            "text-background-color": "rgba(16,18,22,0.75)",
            "text-background-opacity": 1,
            "text-background-padding": 3,
            "text-rotation": "autorotate"
          }
        },
        {
          selector: 'edge[edgeKind = "derived_from"]',
          style: {
            "line-style": "dashed",
            "target-arrow-shape": "none",
            "line-color": "rgba(255,255,255,0.4)"
          }
        },
        {
          selector: 'edge[edgeKind = "conversation-chain"]',
          style: {
            "line-style": "dotted",
            "target-arrow-shape": "none",
            "line-color": "rgba(139,162,191,0.46)"
          }
        },
        { selector: ".faded", style: { opacity: 0.14 } },
        {
          selector: ".focused",
          style: {
            opacity: 1,
            "line-color": "rgba(255,255,255,0.72)",
            "target-arrow-color": "rgba(255,255,255,0.72)",
            width: 2.1
          }
        },
        { selector: ".selected", style: { "border-width": 3, "border-color": "#ffffff" } },
        { selector: ".turn-focus", style: { "overlay-color": "#6bdcff", "overlay-opacity": 0.18, "overlay-padding": 10 } }
      ]
    });

    cy.on("tap", "node", (event) => {
      setSelectedNodeId(event.target.id());
      setSelectedEdgeId("");
      setMenu(null);
    });

    cy.on("tap", "edge", (event) => {
      const now = Date.now();
      const edgeId = event.target.id();
      if (lastEdgeTapRef.current.id === edgeId && now - lastEdgeTapRef.current.at < 300 && !readOnly) {
        const match = graph.edges.find((edge) => edge.id === edgeId);
        setEdgeDialog(match || null);
      }
      lastEdgeTapRef.current = { id: edgeId, at: now };
      setSelectedEdgeId(edgeId);
      setSelectedNodeId("");
      setMenu(null);
    });

    cy.on("tap", (event) => {
      if (event.target === cy) {
        setSelectedNodeId("");
        setSelectedEdgeId("");
        setMenu(null);
      }
    });

    cy.on("mouseover", "node", (event) => {
      const node = event.target;
      setHoverNodeId(node.id());
      cy.elements().addClass("faded").removeClass("focused");
      node.removeClass("faded").addClass("focused");
      node.connectedEdges().removeClass("faded").addClass("focused");
      node.neighborhood().removeClass("faded").addClass("focused");
    });

    cy.on("mouseout", "node", () => {
      setHoverNodeId("");
      cy.elements().removeClass("faded").removeClass("focused");
    });

    cy.on("cxttap", "node", (event) => {
      if (readOnly || !graph.nodes.find((node) => node.id === event.target.id())) {
        return;
      }
      event.preventDefault();
      setMenu({
        x: event.renderedPosition.x + 12,
        y: event.renderedPosition.y + 12,
        nodeId: event.target.id(),
        actions: [
          { id: "rename", label: "Rename node" },
          { id: "merge", label: "Merge into selected node" },
          { id: "delete", label: "Delete node" }
        ]
      });
    });

    cy.on("mousedown", "node", (event) => {
      if (readOnly || !event.originalEvent.shiftKey || !graph.nodes.find((node) => node.id === event.target.id())) {
        return;
      }
      dragStateRef.current = { sourceId: event.target.id() };
    });

    cy.on("mouseup", "node", async (event) => {
      if (readOnly || !dragStateRef.current || boot.sampleMode) {
        return;
      }
      const { sourceId } = dragStateRef.current;
      dragStateRef.current = null;
      const targetId = event.target.id();
      if (!sourceId || sourceId === targetId || targetId.includes(":pair:")) {
        return;
      }
      try {
        await pushHistory();
        await apiRequest("/api/graph/edges", {
          method: "POST",
          body: JSON.stringify({
            source_id: sourceId,
            target_id: targetId,
            relationship: "relates_to",
            weight: 1.0
          })
        });
        await loadSnapshot(scope);
        setToast("Created relationship.");
      } catch (error) {
        setToast(error.message);
      }
    });

    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [activeTab, layerGraph, graph.edges, graph.nodes, readOnly, selectedNodeId, boot.sampleMode]);

  useEffect(() => {
    if (!cyRef.current) {
      return;
    }
    cyRef.current.nodes().removeClass("selected").removeClass("turn-focus");
    cyRef.current.edges().removeClass("selected");
    if (selectedNodeId) {
      cyRef.current.$id(selectedNodeId).addClass("selected");
    }
    if (selectedEdgeId) {
      cyRef.current.$id(selectedEdgeId).addClass("selected");
    }
    if (highlightedTurnPairId) {
      cyRef.current.$id(highlightedTurnPairId).addClass("turn-focus");
      const pair = transcriptPairs.find((item) => item.id === highlightedTurnPairId);
      for (const nodeId of pair?.derivedNodeIds || []) {
        cyRef.current.$id(nodeId).addClass("turn-focus");
      }
    }
  }, [selectedNodeId, selectedEdgeId, highlightedTurnPairId, transcriptPairs]);

  const applyScope = async () => {
    await loadSnapshot(scope);
    setToast("Scope updated.");
  };

  const saveNodeEdits = async (event) => {
    event.preventDefault();
    if (!graph.nodes.find((node) => node.id === selectedNodeId) || readOnly || boot.sampleMode) {
      return;
    }
    const selectedGraphNode = graph.nodes.find((node) => node.id === selectedNodeId);
    const form = new FormData(event.currentTarget);
    await pushHistory();
    await apiRequest(`/api/graph/nodes/${selectedGraphNode.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        label: form.get("label"),
        content: form.get("content"),
        tags: String(form.get("tags") || "")
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean)
      })
    });
    await loadSnapshot(scope);
    setToast("Node updated.");
  };

  const deleteNode = async (nodeId) => {
    if (boot.sampleMode || readOnly) {
      return;
    }
    await pushHistory();
    await apiRequest(`/api/graph/nodes/${nodeId}`, { method: "DELETE" });
    await loadSnapshot(scope);
    setSelectedNodeId("");
    setToast("Node deleted.");
  };

  const deleteEdge = async (edgeId) => {
    if (boot.sampleMode || readOnly) {
      return;
    }
    await pushHistory();
    await apiRequest(`/api/graph/edges/${edgeId}`, {
      method: "DELETE"
    });
    await loadSnapshot(scope);
    setSelectedEdgeId("");
    setToast("Edge deleted.");
  };

  const mergeNode = async (sourceId) => {
    if (readOnly) {
      setToast("Cannot modify graph in view mode.");
      return;
    }
    if (boot.sampleMode) {
      setToast("Cannot modify sample data.");
      return;
    }
    if (!selectedNodeId || selectedNodeId === sourceId) {
      setToast("Select a destination graph node first.");
      return;
    }
    const source = graph.nodes.find((node) => node.id === sourceId);
    const target = graph.nodes.find((node) => node.id === selectedNodeId);
    if (!source || !target) {
      return;
    }
    await pushHistory();
    await apiRequest(`/api/graph/nodes/${target.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        label: target.label,
        content: [target.content, source.content].filter(Boolean).join("\n\n"),
        tags: [...new Set([...(target.tags || []), ...(source.tags || [])])]
      })
    });
    for (const edge of graph.edges) {
      if (edge.source_id === source.id || edge.target_id === source.id) {
        const nextSource = edge.source_id === source.id ? target.id : edge.source_id;
        const nextTarget = edge.target_id === source.id ? target.id : edge.target_id;
        if (nextSource !== nextTarget) {
          await apiRequest("/api/graph/edges", {
            method: "POST",
            body: JSON.stringify({
              source_id: nextSource,
              target_id: nextTarget,
              relationship: edge.relationship,
              weight: edge.weight
            })
          });
        }
      }
    }
    await apiRequest(`/api/graph/nodes/${source.id}`, { method: "DELETE" });
    await loadSnapshot(scope);
    setToast("Nodes merged.");
  };

  const handleMenuAction = async (actionId, nodeId) => {
    setMenu(null);
    if (actionId === "delete") {
      await deleteNode(nodeId);
      return;
    }
    if (actionId === "rename") {
      setSelectedNodeId(nodeId);
      return;
    }
    if (actionId === "merge") {
      await mergeNode(nodeId);
    }
  };

  const createNode = async () => {
    if (readOnly || boot.sampleMode) {
      return;
    }
    await pushHistory();
    await apiRequest("/api/graph/nodes", {
      method: "POST",
      body: JSON.stringify({
        label: "Untitled memory",
        content: "New graph note.",
        node_type: "note",
        ...scope
      })
    });
    await loadSnapshot(scope);
    setToast("Node created.");
  };

  const saveEdgeDialog = async (relationship) => {
    if (!edgeDialog || boot.sampleMode || readOnly) {
      return;
    }
    await pushHistory();
    await apiRequest(`/api/graph/edges/${edgeDialog.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        source_id: edgeDialog.source_id,
        target_id: edgeDialog.target_id,
        relationship,
        weight: edgeDialog.weight
      })
    });
    setEdgeDialog(null);
    await loadSnapshot(scope);
    setToast("Edge label updated.");
  };

  const exportGraph = async (format) => {
    if (boot.sampleMode || readOnly) {
      setToast(
        boot.sampleMode
          ? "Sample mode. Export is disabled."
          : "Read-only mode. Export is disabled."
      );
      return;
    }

    const query = new URLSearchParams({ ...scope, format });
    const response = await fetch(`/api/graph/export?${query.toString()}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = format === "abhi" ? "waggle-memory.abhi" : "waggle-memory.json";
    link.click();
    URL.revokeObjectURL(url);
  };

  const runTranscriptSearch = async () => {
    if (transcriptSearch.trim() === "") {
      setTranscriptHits([]);
      setToast("Please enter a search query.");
      return;
    }
    if (boot.sampleMode) {
      const queryText = transcriptSearch.trim().toLowerCase();
      setTranscriptHits(
        SAMPLE_TRANSCRIPTS.filter((record) => record.transcript_text.toLowerCase().includes(queryText)).map((record) => ({
          score: 0.8,
          ...record,
          transcript_snippet: record.transcript_text
        }))
      );
      return;
    }
    const query = new URLSearchParams({
      ...scope,
      query: transcriptSearch,
      limit: "20"
    });
    const payload = await apiRequest(`/api/graph/transcripts?${query.toString()}`);
    setTranscriptHits(payload.hits || []);
  };

  const loadMoreTranscripts = async () => {
    const nextOffset = transcriptRecords.length;
    const query = new URLSearchParams({
      ...scope,
      limit: "200",
      offset: String(nextOffset),
    });
    const payload = await apiRequest(`/api/graph/transcripts?${query.toString()}`);
    if (payload.records?.length) {
      setTranscriptRecords((prev) => [...prev, ...payload.records]);
      setTranscriptOffset(nextOffset);
      setTranscriptTotalCount(payload.pagination?.total_count ?? 0);
    }
  };

  const runRetrievalDebug = async () => {
    if (boot.sampleMode) {
      setRetrievalResult(SAMPLE_RETRIEVAL);
      return;
    }
    const payload = await apiRequest("/api/graph/retrieval-debug", {
      method: "POST",
      body: JSON.stringify({
        ...scope,
        query: retrievalQuery,
        max_nodes: 8,
        max_depth: 1
      })
    });
    setRetrievalResult(payload);
  };

  const previewImport = async (content, format = "abhi") => {
    if (boot.sampleMode) {
      setImportPreview({
        snapshot: SAMPLE_GRAPH_SNAPSHOT,
        imported_node_ids: SAMPLE_GRAPH_SNAPSHOT.nodes.map((node) => node.id),
        validation: { valid: true, errors: [] }
      });
      return;
    }
    const payload = await apiRequest("/api/graph/abhi/preview-import", {
      method: "POST",
      body: JSON.stringify({ content, format })
    });
    setImportPreview(payload);
  };

  const commitImport = async () => {
    if (!importPreview || boot.sampleMode || readOnly) {
      return;
    }
    const payload = await apiRequest("/api/graph/import", {
      method: "POST",
      body: JSON.stringify({
        content: importPreview.rawContent,
        content_base64: importPreview.rawContentBase64,
        format: importPreview.format || "abhi"
      })
    });
    setImportedNodeIds(payload.imported_node_ids || []);
    setImportPreview(null);
    await loadSnapshot(scope);
    setToast("Imported graph data.");
  };

  const loadImportFile = async (event) => {
    if (readOnly) {
      return;
    }
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    const format = file.name.endsWith(".json") ? "json" : "abhi";
    const content = format === "json" ? await readFileText(file) : "";
    const contentBase64 = format === "abhi" ? await readFileBase64(file) : "";
    if (boot.sampleMode) {
      await previewImport(content, format);
      return;
    }
    const preview = await apiRequest("/api/graph/abhi/preview-import", {
      method: "POST",
      body: JSON.stringify({ content, content_base64: contentBase64, format })
    }).catch(async () => {
      await previewImport(content, format);
      return null;
    });
    if (preview) {
      setImportPreview({ ...preview, rawContent: content, rawContentBase64: contentBase64, format });
    }
  };

  const loadDiffFiles = async (event, side) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    const contentBase64 = await readFileBase64(file);
    setAbhiDiff((current) => ({ ...(current || {}), [`${side}Base64`]: contentBase64 }));
  };

  useEffect(() => {
    if ((!abhiDiff?.leftBase64 || !abhiDiff?.rightBase64) || boot.sampleMode) {
      return;
    }
    apiRequest("/api/graph/abhi/diff", {
      method: "POST",
      body: JSON.stringify({ content_a_base64: abhiDiff.leftBase64, content_b_base64: abhiDiff.rightBase64 })
    })
      .then((payload) => setAbhiDiff((current) => ({ ...(current || {}), payload })))
      .catch((error) => setToast(error.message));
  }, [abhiDiff?.leftBase64, abhiDiff?.rightBase64, boot.sampleMode]);

  const visibleTranscriptRecords = transcriptSearch.trim()
    ? transcriptHits
    : transcriptRecords.filter((record) => {
        const activeSessions = new Set(filters.sessions || []);
        const activeAgents = new Set(filters.agents || []);
        const activeProjects = new Set(filters.projects || []);
        if (activeSessions.size && !activeSessions.has(record.session_id || "")) {
          return false;
        }

        if (activeAgents.size && !activeAgents.has(record.agent_id || "")) {
          return false;
        }

        if (activeProjects.size && !activeProjects.has(record.project || "")) {
          return false;
        }

        return true;
      });

  const selectedGraphNode = graph.nodes.find((node) => node.id === selectedNodeId) || null;
  const selectedPair = transcriptPairs.find((pair) => pair.id === selectedNodeId) || null;
  const nodeEdges = selectedGraphNode ? buildNodeEdgeList(selectedGraphNode.id, graph) : [];
  const provenanceTrail = selectedGraphNode ? buildProvenanceTrail(selectedGraphNode, graph) : [];
  const sourcePrompts = selectedGraphNode ? summarizeSourcePrompts(selectedGraphNode) : [];
  const sourceTurnPairId = selectedGraphNode ? firstTurnPairId(selectedGraphNode) : "";

  return (
    <div className="min-h-screen p-4">
      <div className="grid min-h-[calc(100vh-2rem)] items-start grid-cols-[320px_minmax(0,1fr)_380px] gap-4 max-[1280px]:grid-cols-1">
        <div className="flex min-h-0 flex-col gap-4">
          <Section title="Waggle Graph Studio" extra={<span className="text-xs text-graph-muted">{boot.sampleMode ? "Sample data" : readOnly ? "View mode" : "Edit mode"}</span>}>
            <p className="text-sm leading-6 text-graph-muted">
              Dual-layer memory explorer for extracted graph nodes and verbatim transcript turn-pairs, with provenance, retrieval tuning,
              and ABHI workflows.
            </p>
            <div className="mt-4 grid grid-cols-3 gap-2 text-sm">
              <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Graph nodes</div>
                <div className="mt-1 text-xl font-semibold">{graph.nodes.length}</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Turn-pairs</div>
                <div className="mt-1 text-xl font-semibold">{transcriptPairs.length}</div>
              </div>
              <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Imported</div>
                <div className="mt-1 text-xl font-semibold">{graph.nodes.filter((node) => node.imported).length}</div>
              </div>
            </div>
          </Section>

          <Section title="Views">
            <div className="flex flex-wrap gap-2">
              {["graph", "transcripts", "retrieval"].map((tab) => (
                <Pill key={tab} active={activeTab === tab} onClick={() => setActiveTab(tab)}>
                  {tab[0].toUpperCase() + tab.slice(1)}
                </Pill>
              ))}
            </div>
            {activeTab === "graph" ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {["graph", "conversation", "both"].map((item) => (
                  <Pill key={item} active={layerMode === item} onClick={() => setLayerMode(item)}>
                    {item[0].toUpperCase() + item.slice(1)}
                  </Pill>
                ))}
              </div>
            ) : null}
          </Section>

          <Section title="Scope">
            <div className="space-y-2">
              <input className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" placeholder="Project" value={scope.project} onChange={(event) => setScope((current) => ({ ...current, project: event.target.value }))} />
              <input className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" placeholder="Agent" value={scope.agent_id} onChange={(event) => setScope((current) => ({ ...current, agent_id: event.target.value }))} />
              <input className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" placeholder="Session" value={scope.session_id} onChange={(event) => setScope((current) => ({ ...current, session_id: event.target.value }))} />
              <button className="w-full rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" onClick={applyScope} type="button" disabled={boot.sampleMode}>
                Apply scope
              </button>
            </div>
          </Section>

          <Section title="Filters">
            <input className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" placeholder="Search graph nodes" value={filters.search} onChange={(event) => setFilters((current) => ({ ...current, search: event.target.value }))} />
            <div className="mt-4 space-y-3">
              <div>
                <div className="mb-2 text-xs uppercase tracking-[0.16em] text-graph-muted">Date</div>
                <div className="flex flex-wrap gap-2">
                  {DATE_RANGES.map((range) => (
                    <Pill key={range.id} active={filters.dateRange === range.id} onClick={() => setFilters((current) => ({ ...current, dateRange: range.id }))}>
                      {range.label}
                    </Pill>
                  ))}
                </div>
              </div>
              <div>
                <div className="mb-2 text-xs uppercase tracking-[0.16em] text-graph-muted">Source app</div>
                <div className="flex flex-wrap gap-2">
                  {buckets.sources.map((source) => (
                    <Pill
                      key={source.id}
                      active={filters.sources.includes(source.id)}
                      color={source.color}
                      onClick={() =>
                        setFilters((current) => ({
                          ...current,
                          sources: current.sources.includes(source.id) ? current.sources.filter((value) => value !== source.id) : [...current.sources, source.id]
                        }))
                      }
                    >
                      {source.label} {source.count}
                    </Pill>
                  ))}
                </div>
              </div>
              <div>
                <div className="mb-2 text-xs uppercase tracking-[0.16em] text-graph-muted">Tags</div>
                <div className="flex max-h-24 flex-wrap gap-2 overflow-auto scrollbar-thin">
                  {buckets.tags.map((tag) => (
                    <Pill
                      key={tag.id}
                      active={filters.tags.includes(tag.id)}
                      onClick={() =>
                        setFilters((current) => ({
                          ...current,
                          tags: current.tags.includes(tag.id) ? current.tags.filter((value) => value !== tag.id) : [...current.tags, tag.id]
                        }))
                      }
                    >
                      #{tag.label}
                    </Pill>
                  ))}
                </div>
              </div>
            </div>
          </Section>

          <Section title="Extraction health" extra={<span className="text-sm text-white">{extractionHealth.percent}%</span>}>
            <p className="text-sm text-graph-muted">
              {extractionHealth.produced} of {extractionHealth.total} turn-pairs in the current transcript produced memory.
            </p>
            <button className="mt-3 rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={() => setShowMisses((value) => !value)} type="button">
              {showMisses ? "Hide misses" : "Show zero-candidate turns"}
            </button>
            {showMisses ? (
              <div className="mt-3 max-h-32 space-y-2 overflow-auto scrollbar-thin">
                {extractionHealth.zeroPairs.map((pair) => (
                  <button
                    key={pair.id}
                    className="block w-full rounded-xl border border-white/8 bg-black/15 px-3 py-2 text-left text-xs"
                    onClick={() => {
                      setHighlightedTurnPairId(pair.id);
                      setActiveTab("graph");
                      setLayerMode("both");
                    }}
                    type="button"
                  >
                    <div className="text-white">{pair.label}</div>
                    <div className="mt-1 text-graph-muted">{pair.transcripts.map((item) => item.role).join(" / ")}</div>
                  </button>
                ))}
              </div>
            ) : null}
          </Section>
        </div>

        <section className="relative h-[720px] min-h-[720px] overflow-hidden rounded-[22px] border border-white/8 bg-black/20 panel-shell max-[1280px]:h-[680px] max-[1280px]:min-h-[680px]">
          {activeTab === "graph" ? (
            <>
              <div className="flex items-center gap-2 border-b border-white/8 px-4 py-3">
                <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={() => loadSnapshot(scope)} type="button" disabled={boot.sampleMode}>
                  Refresh
                </button>
                <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={createNode} disabled={readOnly || boot.sampleMode} type="button">
                  New node
                </button>
                <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={undo} disabled={!historyPast.length || readOnly || boot.sampleMode} type="button">
                  Undo
                </button>
                <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={redo} disabled={!historyFuture.length || readOnly || boot.sampleMode} type="button">
                  Redo
                </button>
                <div className="ml-auto flex items-center gap-2 text-xs text-graph-muted">
                  <span>{layerMode}</span>
                  {hoverNodeId ? <span className="rounded-full bg-white/8 px-2 py-1 text-white">Hover focus</span> : null}
                </div>
              </div>
              <div className="grid-noise h-[calc(100%-57px)] w-full" ref={hostRef} />
            </>
          ) : null}

          {activeTab === "transcripts" ? (
            <div className="flex h-full flex-col">
              <div className="border-b border-white/8 px-4 py-3">
                <div className="flex gap-2">
                  <input className="flex-1 rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" placeholder="Search transcripts (hybrid BM25 + vector)" value={transcriptSearch} onChange={(event) => setTranscriptSearch(event.target.value)} />
                  <button className="rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" onClick={() => runTranscriptSearch().catch((error) => setToast(error.message))} type="button">
                    Search
                  </button>
                </div>
              </div>
              <div className="flex-1 overflow-auto p-4 scrollbar-thin">
                <div className="space-y-3">
                  {visibleTranscriptRecords.map((record) => {
                    const pairId = `${record.session_id || "default"}:pair:${Math.floor((record.turn_index || 0) / 2)}`;
                    const pair = transcriptPairs.find((item) => item.id === pairId);
                    return (
                      <div key={`${record.session_id}:${record.turn_index}:${record.role}`} className="rounded-2xl border border-white/8 bg-black/15 p-4">
                        <div className="flex items-center justify-between gap-3">
                          <div>
                            <div className="text-sm font-semibold text-white">{record.role}</div>
                            <div className="text-xs text-graph-muted">
                              {record.project || "-"} · {record.agent_id || "-"} · {record.session_id || "-"} · turn {record.turn_index}
                            </div>
                          </div>
                          <button
                            className="rounded-xl border border-white/10 px-3 py-2 text-xs"
                            onClick={() => {
                              setHighlightedTurnPairId(pairId);
                              setActiveTab("graph");
                              setLayerMode("both");
                              if (pair?.derivedNodeIds?.[0]) {
                                setSelectedNodeId(pair.derivedNodeIds[0]);
                              }
                            }}
                            type="button"
                          >
                            Show in graph
                          </button>
                        </div>
                        <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-graph-text">{record.transcript_text || record.transcript_snippet}</p>
                      </div>
                    );
                  })}
                </div>
                {!transcriptSearch.trim() && transcriptTotalCount > transcriptRecords.length ? (
                  <div className="flex justify-center pt-2 pb-4">
                    <button
                      className="rounded-xl border border-white/10 px-4 py-2 text-sm text-graph-muted hover:text-white"
                      onClick={() => loadMoreTranscripts().catch((error) => setToast(error.message))}
                      type="button"
                    >
                      Load more ({transcriptRecords.length} of {transcriptTotalCount})
                    </button>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {activeTab === "retrieval" ? (
            <div className="flex h-full flex-col overflow-auto p-4 scrollbar-thin">
              <div className="flex gap-2">
                <textarea className="h-24 flex-1 rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" value={retrievalQuery} onChange={(event) => setRetrievalQuery(event.target.value)} />
                <button className="rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" onClick={() => runRetrievalDebug().catch((error) => setToast(error.message))} type="button">
                  Run debugger
                </button>
              </div>
              {retrievalResult ? (
                <div className="mt-4 space-y-4">
                  <Section title="Top hits">
                    <div className="grid gap-4 md:grid-cols-2">
                      <div>
                        <div className="mb-2 text-xs uppercase tracking-[0.16em] text-graph-muted">Graph / vector / recency</div>
                        <div className="space-y-2">
                          {(retrievalResult.debug?.flat_top_nodes || []).map((node) => (
                            <div key={node.node_id} className="rounded-xl border border-white/8 bg-black/15 p-3 text-sm">
                              <div className="font-medium text-white">{node.label}</div>
                              <div className="mt-1 text-xs text-graph-muted">
                                final {Number(node.final_score || 0).toFixed(2)} · vector {Number(node.similarity_score || 0).toFixed(2)} · recency {Number(node.recency_score || 0).toFixed(2)}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div>
                        <div className="mb-2 text-xs uppercase tracking-[0.16em] text-graph-muted">Replay / BM25 hybrid</div>
                        <div className="space-y-2">
                          {(retrievalResult.replay_hits || []).map((hit, index) => (
                            <div key={`${hit.session_id}:${hit.turn_index}:${index}`} className="rounded-xl border border-white/8 bg-black/15 p-3 text-sm">
                              <div className="font-medium text-white">{hit.role}</div>
                              <div className="mt-1 text-xs text-graph-muted">score {Number(hit.score || 0).toFixed(2)} · {hit.session_id} · turn {hit.turn_index}</div>
                              <div className="mt-2 text-sm text-white">{hit.transcript_snippet}</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  </Section>

                  <Section title="RRF fused ranking" extra={<span className="text-sm text-white">{retrievalResult.token_estimate} tokens</span>}>
                    <div className="space-y-2">
                      {(retrievalResult.fusion_hits || []).map((hit) => (
                        <div key={`${hit.fused_rank}:${hit.content}`} className="rounded-xl border border-white/8 bg-black/15 p-3 text-sm">
                          <div className="font-medium text-white">
                            #{hit.fused_rank} {hit.content}
                          </div>
                          <div className="mt-1 text-xs text-graph-muted">
                            lane {hit.source_lane} · graph {hit.graph_rank ?? "-"} · replay {hit.replay_rank ?? "-"} · score {Number(hit.score || 0).toFixed(2)}
                          </div>
                          <div className="mt-2 text-sm text-white">{hit.reasoning}</div>
                        </div>
                      ))}
                    </div>
                  </Section>

                  <Section title="Window routing">
                    <div className="space-y-2">
                      {(retrievalResult.debug?.all_windows || []).map((window) => (
                        <div key={window.window_id} className="rounded-xl border border-white/8 bg-black/15 p-3 text-sm">
                          <div className="font-medium text-white">{window.title || window.session_id}</div>
                          <div className="mt-1 text-xs text-graph-muted">
                            route {Number(window.routing_score || 0).toFixed(2)} · similarity {Number(window.similarity || 0).toFixed(2)} · recency {Number(window.recency || 0).toFixed(2)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </Section>
                </div>
              ) : null}
            </div>
          ) : null}
        </section>

        <div className="flex min-h-0 flex-col gap-4">
          <Section title="Inspector">
            {selectedGraphNode ? (
              <form className="space-y-3" onSubmit={saveNodeEdits}>
                <input className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" name="label" defaultValue={selectedGraphNode.label} disabled={readOnly || boot.sampleMode} />
                <textarea className="h-32 w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" name="content" defaultValue={selectedGraphNode.content} disabled={readOnly || boot.sampleMode} />
                <input className="w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" name="tags" defaultValue={(selectedGraphNode.tags || []).join(", ")} disabled={readOnly || boot.sampleMode} />
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3 text-xs leading-6 text-graph-muted">
                  <div>Type: {selectedGraphNode.node_type}</div>
                  <div>Source app: {selectedGraphNode.source.label}</div>
                  <div>Evidence count: {(selectedGraphNode.evidence_records || []).length}</div>
                  <div>Imported: {selectedGraphNode.imported ? "yes" : "no"}</div>
                </div>
                {sourceTurnPairId ? (
                  <button
                    className="w-full rounded-xl border border-white/10 px-3 py-2 text-sm"
                    onClick={() => {
                      setHighlightedTurnPairId(sourceTurnPairId);
                      setActiveTab("transcripts");
                    }}
                    type="button"
                  >
                    Jump to source turn-pair
                  </button>
                ) : null}
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                  <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Edges</div>
                  <div className="mt-2 space-y-2 text-sm">
                    {nodeEdges.map((edge) => (
                      <div key={edge.id} className="rounded-xl border border-white/6 bg-black/10 p-2">
                        {`${edge.sourceLabel} --[${edge.relationship}]--> ${edge.targetLabel}`}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                  <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Derived from</div>
                  <div className="mt-2 space-y-2 text-sm">
                    {provenanceTrail.length ? provenanceTrail.map((node) => <div key={node.id}>{node.label}</div>) : <div className="text-graph-muted">No derived_from trail.</div>}
                  </div>
                </div>
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                  <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Source prompts</div>
                  <div className="mt-2 max-h-28 space-y-2 overflow-auto text-sm scrollbar-thin">
                    {sourcePrompts.map((prompt, index) => (
                      <div key={`${index}:${prompt.slice(0, 12)}`} className="rounded-xl border border-white/6 bg-black/10 p-2 whitespace-pre-wrap">
                        {prompt}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="flex gap-2">
                  <button className="flex-1 rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" disabled={readOnly || boot.sampleMode} type="submit">
                    Save node
                  </button>
                  <button className="rounded-xl border border-red-400/30 px-3 py-2 text-sm text-red-200" disabled={readOnly || boot.sampleMode} onClick={() => deleteNode(selectedGraphNode.id).catch((error) => setToast(error.message))} type="button">
                    Delete
                  </button>
                </div>
              </form>
            ) : selectedPair ? (
              <div className="space-y-3 text-sm">
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                  <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Turn-pair</div>
                  <div className="mt-2 text-white">{selectedPair.label}</div>
                </div>
                <div className="space-y-2">
                  {selectedPair.transcripts.map((item) => (
                    <div key={`${item.role}:${item.turn_index}`} className="rounded-xl border border-white/6 bg-black/10 p-3">
                      <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">{item.role}</div>
                      <div className="mt-1 whitespace-pre-wrap text-white">{item.transcript_text}</div>
                    </div>
                  ))}
                </div>
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                  <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Derived nodes</div>
                  <div className="mt-2 space-y-2">
                    {selectedPair.derivedNodeIds.map((nodeId) => {
                      const node = graph.nodes.find((item) => item.id === nodeId);
                      return (
                        <button key={nodeId} className="block w-full rounded-xl border border-white/6 bg-black/10 p-2 text-left text-sm" onClick={() => setSelectedNodeId(nodeId)} type="button">
                          {node?.label || nodeId}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            ) : selectedEdge ? (
              <div className="space-y-3 text-sm text-graph-muted">
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3">
                  <div className="text-white">{selectedEdge.relationship}</div>
                  <div className="mt-1 break-all text-xs">Edge ID: {selectedEdge.id}</div>
                </div>
                <button className="rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" disabled={readOnly || boot.sampleMode} onClick={() => setEdgeDialog(selectedEdge)} type="button">
                  Edit edge label
                </button>
                <button
                  className="rounded-xl border border-red-400/30 px-3 py-2 text-sm text-red-200"
                  disabled={readOnly || boot.sampleMode}
                  onClick={() =>
                    deleteEdge(selectedEdge.id).catch((error) =>
                      setToast(error.message)
                    )
                  }
                  type="button"
                >
                  Delete edge
                </button>
              </div>
            ) : (
              <p className="text-sm leading-6 text-graph-muted">
                Click a graph node for provenance and evidence, or a transcript turn-pair to inspect its verbatim messages and derived nodes.
              </p>
            )}
          </Section>

          <Section title=".ABHI workflow">
            <div className="grid gap-2">
              <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={() => exportGraph("abhi")} type="button">
                Export
              </button>
              <FileInputButton label="Import preview" accept=".abhi,.json" onChange={(event) => loadImportFile(event).catch((error) => setToast(error.message))} disabled={readOnly || boot.sampleMode} />
              <button className="rounded-xl border border-white/10 px-3 py-2 text-sm text-graph-muted" type="button">
                Sync to Drive
              </button>
              <button className="rounded-xl border border-white/10 px-3 py-2 text-sm text-graph-muted" type="button">
                Share
              </button>
            </div>
            {importPreview ? (
              <div className="mt-3 rounded-2xl border border-white/8 bg-black/15 p-3 text-sm">
                <div className="font-medium text-white">Import preview</div>
                <div className="mt-1 text-graph-muted">
                  {importPreview.snapshot?.nodes?.length || 0} nodes · {importPreview.snapshot?.edges?.length || 0} edges
                </div>
                <div className="mt-2 max-h-24 overflow-auto text-xs text-graph-muted scrollbar-thin">
                  {(importPreview.snapshot?.nodes || []).slice(0, 6).map((node) => (
                    <div key={node.id}>{node.label}</div>
                  ))}
                </div>
                {!boot.sampleMode ? (
                  <button className="mt-3 w-full rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" onClick={() => commitImport().catch((error) => setToast(error.message))} disabled={readOnly} type="button">
                    Commit import
                  </button>
                ) : null}
              </div>
            ) : null}
            <div className="mt-4 grid gap-2">
              <div className="text-xs uppercase tracking-[0.16em] text-graph-muted">Visual diff</div>
              <div className="flex gap-2">
                <FileInputButton label="Left .abhi" accept=".abhi" onChange={(event) => loadDiffFiles(event, "left").catch((error) => setToast(error.message))} />
                <FileInputButton label="Right .abhi" accept=".abhi" onChange={(event) => loadDiffFiles(event, "right").catch((error) => setToast(error.message))} />
              </div>
              {abhiDiff?.payload ? (
                <div className="rounded-2xl border border-white/8 bg-black/15 p-3 text-xs text-graph-muted">
                  <div>Nodes added: {(abhiDiff.payload.diff?.nodes_added || []).length}</div>
                  <div>Nodes updated: {(abhiDiff.payload.diff?.nodes_updated || []).length}</div>
                  <div>Edges added: {(abhiDiff.payload.diff?.edges_added || []).length}</div>
                </div>
              ) : null}
            </div>
          </Section>
        </div>
      </div>

      <AnimatePresence>
        {status ? (
          <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 12 }} className="fixed bottom-4 right-4 rounded-xl border border-white/10 bg-black/75 px-4 py-3 text-sm shadow-2xl">
            {status}
          </motion.div>
        ) : null}
      </AnimatePresence>

      <ContextMenu menu={menu} onClose={() => setMenu(null)} onAction={(actionId, nodeId) => handleMenuAction(actionId, nodeId).catch((error) => setToast(error.message))} />
      <EdgeDialog edge={edgeDialog} onCancel={() => setEdgeDialog(null)} onSave={(relationship) => saveEdgeDialog(relationship).catch((error) => setToast(error.message))} />
    </div>
  );
}
