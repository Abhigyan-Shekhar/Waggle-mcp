import { Section } from "./Section";

export function GraphCanvas({
  activeTab,
  hostRef,
  graph, layerMode, hoverNodeId,
  transcriptPairs,
  readOnly, boot,
  loadSnapshot, scope, createNode, undo, redo,
  historyPast, historyFuture,
  transcriptSearch, setTranscriptSearch,
  runTranscriptSearch,
  visibleTranscriptRecords,
  loadMoreTranscripts, transcriptTotalCount, transcriptRecords,
  retrievalQuery, setRetrievalQuery,
  retrievalResult, runRetrievalDebug,
  setHighlightedTurnPairId, setActiveTab, setLayerMode, setSelectedNodeId,
  setToast
}) {
  return (
    <section className="relative h-[720px] min-h-[720px] overflow-hidden rounded-[22px] border border-white/8 bg-black/20 panel-shell max-[1280px]:h-[680px] max-[1280px]:min-h-[680px]">
      {activeTab === "graph" ? (
        <>
          <div className="flex items-center gap-2 border-b border-white/8 px-4 py-3">
            <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={() => loadSnapshot(scope).catch((error) => setToast(error.message))} type="button" disabled={boot.sampleMode}>
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
  );
}
