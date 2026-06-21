import { Section } from "./Section";
import { FileInputButton } from "./FileInputButton";

export function RightPanel({
  selectedGraphNode, selectedPair, selectedEdge,
  graph, nodeEdges, provenanceTrail, sourcePrompts, sourceTurnPairId,
  readOnly, boot,
  saveNodeEdits, deleteNode, deleteEdge, setEdgeDialog,
  setHighlightedTurnPairId, setActiveTab, setSelectedNodeId,
  exportGraph, loadImportFile, importPreview, commitImport,
  loadDiffFiles, abhiDiff,
  setToast
}) {
  return (
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
  );
}
