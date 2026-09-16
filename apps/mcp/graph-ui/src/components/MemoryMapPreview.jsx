import React, { useEffect, useMemo, useRef } from "react";
import cytoscape from "cytoscape";
import { ArrowRight } from "lucide-react";
import { selectMemoryMapPreview } from "../lib/memory-map";

export function MemoryMapPreview({ snapshot, focusMemoryId = "", graphHref }) {
  const containerRef = useRef(null);
  const preview = useMemo(
    () => selectMemoryMapPreview(snapshot, { focusMemoryId, limit: 6 }),
    [focusMemoryId, snapshot],
  );

  useEffect(() => {
    if (!containerRef.current || !preview.nodes.length) return undefined;
    const selectedIds = new Set(preview.nodes.map((node) => node.id));
    const graph = cytoscape({
      container: containerRef.current,
      elements: [
        ...preview.nodes.map((node) => ({
          data: { id: node.id, label: node.label || node.content || "Memory", type: node.node_type || "memory" },
        })),
        ...preview.edges
          .filter((edge) => selectedIds.has(edge.source_id) && selectedIds.has(edge.target_id))
          .map((edge, index) => ({
            data: {
              id: `${edge.source_id}-${edge.relationship}-${edge.target_id}-${index}`,
              source: edge.source_id,
              target: edge.target_id,
              label: edge.relationship,
            },
          })),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#eaf3ec",
            "border-color": "#4d8062",
            "border-width": 1.5,
            color: "#244032",
            "font-family": "Inter, ui-sans-serif, system-ui, sans-serif",
            "font-size": 10,
            height: 34,
            label: "data(label)",
            shape: "round-rectangle",
            "text-max-width": 108,
            "text-valign": "center",
            "text-wrap": "ellipsis",
            width: 120,
          },
        },
        {
          selector: 'node[type = "decision"]',
          style: { "background-color": "#d8eadc", "border-color": "#276749" },
        },
        {
          selector: "edge",
          style: {
            "curve-style": "bezier",
            "line-color": "#91ad9b",
            "target-arrow-color": "#91ad9b",
            "target-arrow-shape": "triangle",
            width: 1.2,
          },
        },
      ],
      layout: { name: "breadthfirst", directed: true, padding: 18, spacingFactor: 1.15 },
      userPanningEnabled: false,
      userZoomingEnabled: false,
      boxSelectionEnabled: false,
      autoungrabify: true,
    });
    graph.fit(undefined, 16);
    return () => graph.destroy();
  }, [preview]);

  return (
    <section className="memory-map-preview" data-guide-focus="memory-map">
      <div className="section-heading-inline">
        <div><div className="eyebrow">Live Memory Map</div><h2>How current memory connects</h2></div>
        <a href={graphHref}>Explore full graph <ArrowRight size={15} /></a>
      </div>
      {preview.nodes.length ? (
        <div aria-label="Live memory graph preview" className="memory-map-canvas" ref={containerRef} />
      ) : (
        <p className="memory-map-empty">The live graph will appear when project memory is available.</p>
      )}
    </section>
  );
}
