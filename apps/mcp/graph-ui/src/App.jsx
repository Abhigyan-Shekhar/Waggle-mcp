import React, { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import Cytoscape from "cytoscape";
import coseBilkent from "cytoscape-cose-bilkent";
import { apiRequest, buildScopeQuery } from "./lib/api";
import { getBootConfig } from "./lib/boot";
import { readFileText, readFileBase64 } from "./lib/file-utils";
import {
  buildExtractionHealth,
  buildFilterBuckets,
  buildLayerGraph,
  buildNodeEdgeList,
  buildProvenanceTrail,
  buildTranscriptPairs,
  filterGraph,
  firstTurnPairId,
  normalizeGraph,
  summarizeSourcePrompts
} from "./lib/graph-utils";
import { SAMPLE_GRAPH_SNAPSHOT, SAMPLE_RETRIEVAL, SAMPLE_TRANSCRIPTS } from "./sample-data";
import { ContextMenu } from "./components/ContextMenu";
import { EdgeDialog } from "./components/EdgeDialog";
import { LeftPanel } from "./components/LeftPanel";
import { GraphCanvas } from "./components/GraphCanvas";
import { RightPanel } from "./components/RightPanel";
import { useCytoscape } from "./hooks/useCytoscape";
import { useUndoRedo } from "./hooks/useUndoRedo";

Cytoscape.use(coseBilkent);

export function App() {
  const boot = useMemo(getBootConfig, []);
  const mode = boot.mode;
  const readOnly = mode === "view";
  const hostRef = useRef(null);
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

  const { pushHistory, undo, redo } = useUndoRedo({
    graph, scope, boot, historyPast, historyFuture,
    setHistoryPast, setHistoryFuture, setToast, loadSnapshot
  });

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

  useCytoscape({
    hostRef,
    activeTab,
    layerGraph,
    graph,
    readOnly,
    selectedNodeId,
    selectedEdgeId,
    highlightedTurnPairId,
    transcriptPairs,
    sampleMode: boot.sampleMode,
    onSelectNode: (id) => {
      setSelectedNodeId(id);
      setSelectedEdgeId("");
      setMenu(null);
    },
    onSelectEdge: (id) => {
      setSelectedEdgeId(id);
      setSelectedNodeId("");
      setMenu(null);
    },
    onHoverNode: (id) => setHoverNodeId(id),
    onContextMenu: (x, y, nodeId) => {
      setMenu({
        x, y, nodeId,
        actions: [
          { id: "rename", label: "Rename node" },
          { id: "merge", label: "Merge into selected node" },
          { id: "delete", label: "Delete node" }
        ]
      });
    },
    onEdgeDoubleTap: (edgeData) => setEdgeDialog(edgeData),
    onCreateEdge: async (sourceId, targetId) => {
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
    }
  });

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
        <LeftPanel
          boot={boot}
          graph={graph}
          transcriptPairs={transcriptPairs}
          activeTab={activeTab}
          setActiveTab={setActiveTab}
          layerMode={layerMode}
          setLayerMode={setLayerMode}
          scope={scope}
          setScope={setScope}
          applyScope={applyScope}
          filters={filters}
          setFilters={setFilters}
          buckets={buckets}
          extractionHealth={extractionHealth}
          showMisses={showMisses}
          setShowMisses={setShowMisses}
          setHighlightedTurnPairId={setHighlightedTurnPairId}
        />

        <GraphCanvas
          activeTab={activeTab}
          hostRef={hostRef}
          graph={graph}
          layerMode={layerMode}
          hoverNodeId={hoverNodeId}
          transcriptPairs={transcriptPairs}
          readOnly={readOnly}
          boot={boot}
          loadSnapshot={loadSnapshot}
          scope={scope}
          createNode={createNode}
          undo={undo}
          redo={redo}
          historyPast={historyPast}
          historyFuture={historyFuture}
          transcriptSearch={transcriptSearch}
          setTranscriptSearch={setTranscriptSearch}
          runTranscriptSearch={runTranscriptSearch}
          visibleTranscriptRecords={visibleTranscriptRecords}
          loadMoreTranscripts={loadMoreTranscripts}
          transcriptTotalCount={transcriptTotalCount}
          transcriptRecords={transcriptRecords}
          retrievalQuery={retrievalQuery}
          setRetrievalQuery={setRetrievalQuery}
          retrievalResult={retrievalResult}
          runRetrievalDebug={runRetrievalDebug}
          setHighlightedTurnPairId={setHighlightedTurnPairId}
          setActiveTab={setActiveTab}
          setLayerMode={setLayerMode}
          setSelectedNodeId={setSelectedNodeId}
          setToast={setToast}
        />

        <RightPanel
          selectedGraphNode={selectedGraphNode}
          selectedPair={selectedPair}
          selectedEdge={selectedEdge}
          graph={graph}
          nodeEdges={nodeEdges}
          provenanceTrail={provenanceTrail}
          sourcePrompts={sourcePrompts}
          sourceTurnPairId={sourceTurnPairId}
          readOnly={readOnly}
          boot={boot}
          saveNodeEdits={saveNodeEdits}
          deleteNode={deleteNode}
          deleteEdge={deleteEdge}
          setEdgeDialog={setEdgeDialog}
          setHighlightedTurnPairId={setHighlightedTurnPairId}
          setActiveTab={setActiveTab}
          setSelectedNodeId={setSelectedNodeId}
          exportGraph={exportGraph}
          loadImportFile={loadImportFile}
          importPreview={importPreview}
          commitImport={commitImport}
          loadDiffFiles={loadDiffFiles}
          abhiDiff={abhiDiff}
          setToast={setToast}
        />
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
