import { useEffect, useRef } from "react";
import Cytoscape from "cytoscape";
import { GRAPH_TOKENS } from "../lib/graph-utils";

export function useCytoscape({
  hostRef,
  activeTab,
  layerGraph,
  graph,
  readOnly,
  selectedNodeId,
  selectedEdgeId,
  highlightedTurnPairId,
  transcriptPairs,
  sampleMode,
  onSelectNode,
  onSelectEdge,
  onContextMenu,
  onEdgeDoubleTap,
  onCreateEdge,
  onHoverNode
}) {
  const cyRef = useRef(null);
  const lastEdgeTapRef = useRef({ id: "", at: 0 });
  const dragStateRef = useRef(null);

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
      onSelectNode(event.target.id());
    });

    cy.on("tap", "edge", (event) => {
      const now = Date.now();
      const edgeId = event.target.id();
      if (lastEdgeTapRef.current.id === edgeId && now - lastEdgeTapRef.current.at < 300 && !readOnly) {
        const match = graph.edges.find((edge) => edge.id === edgeId);
        onEdgeDoubleTap(match || null);
      }
      lastEdgeTapRef.current = { id: edgeId, at: now };
      onSelectEdge(edgeId);
    });

    cy.on("tap", (event) => {
      if (event.target === cy) {
        onSelectNode("");
        onSelectEdge("");
      }
    });

    cy.on("mouseover", "node", (event) => {
      const node = event.target;
      onHoverNode(node.id());
      cy.elements().addClass("faded").removeClass("focused");
      node.removeClass("faded").addClass("focused");
      node.connectedEdges().removeClass("faded").addClass("focused");
      node.neighborhood().removeClass("faded").addClass("focused");
    });

    cy.on("mouseout", "node", () => {
      onHoverNode("");
      cy.elements().removeClass("faded").removeClass("focused");
    });

    cy.on("cxttap", "node", (event) => {
      if (readOnly || !graph.nodes.find((node) => node.id === event.target.id())) {
        return;
      }
      event.preventDefault();
      onContextMenu(event.renderedPosition.x + 12, event.renderedPosition.y + 12, event.target.id());
    });

    cy.on("mousedown", "node", (event) => {
      if (readOnly || !event.originalEvent.shiftKey || !graph.nodes.find((node) => node.id === event.target.id())) {
        return;
      }
      dragStateRef.current = { sourceId: event.target.id() };
    });

    cy.on("mouseup", "node", async (event) => {
      if (readOnly || !dragStateRef.current || sampleMode) {
        return;
      }
      const { sourceId } = dragStateRef.current;
      dragStateRef.current = null;
      const targetId = event.target.id();
      if (!sourceId || sourceId === targetId || targetId.includes(":pair:")) {
        return;
      }
      onCreateEdge(sourceId, targetId);
    });

    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [activeTab, layerGraph, graph.edges, graph.nodes, readOnly, sampleMode]);

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

  return cyRef;
}
