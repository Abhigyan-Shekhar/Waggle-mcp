import { Section } from "./Section";
import { Pill } from "./Pill";

const DATE_RANGES = [
  { id: "24h", label: "24h" },
  { id: "7d", label: "7d" },
  { id: "30d", label: "30d" },
  { id: "90d", label: "90d" },
  { id: "all", label: "All time" }
];

export function LeftPanel({
  boot, graph, transcriptPairs,
  activeTab, setActiveTab,
  layerMode, setLayerMode,
  scope, setScope, applyScope,
  filters, setFilters,
  buckets,
  extractionHealth, showMisses, setShowMisses,
  setHighlightedTurnPairId
}) {
  return (
    <div className="flex min-h-0 flex-col gap-4">
      <Section title="Waggle Graph Studio" extra={<span className="text-xs text-graph-muted">{boot.sampleMode ? "Sample data" : boot.mode === "view" ? "View mode" : "Edit mode"}</span>}>
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
  );
}
