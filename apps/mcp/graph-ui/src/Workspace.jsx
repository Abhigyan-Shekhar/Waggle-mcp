import React, { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import waggleIcon from "../../../../assets/waggle-icon.png";
import waggleLogo from "../../../../assets/waggle-logo.png";
import { apiRequest, buildScopeQuery } from "./lib/api";
import { decisionOverview, isAuthoritativeNode, nodeAuthorityStatus } from "./lib/authority";
import { readBootConfig } from "./lib/boot-config";
import {
  registerApplyApprovedMemoryChangeTool,
  registerGetProjectBriefTool,
  registerProposeMemoryChangeTool,
  registerRecallMemoryTool,
} from "./lib/webmcp";

const NAV_ITEMS = [
  { id: "overview", label: "Overview" },
  { id: "memories", label: "Memories" },
  { id: "proposals", label: "Proposals" },
  { id: "activity", label: "Activity" },
];

const ACTIVITY_LABELS = {
  "webmcp.project_brief.read": "ChatGPT requested project brief",
  "webmcp.memory.recalled": "ChatGPT recalled memories",
  "proposal.created": "ChatGPT proposed memory change",
  "proposal.deduplicated": "ChatGPT revisited an open proposal",
  "proposal.approved": "Human approved correction",
  "proposal.edited_and_approved": "Human edited and approved",
  "proposal.rejected": "Human rejected proposed change",
  "proposal.stale": "Proposal became stale",
  "proposal.applied": "Approved memory change applied",
  "memory.superseded": "Previous memory preserved in history",
  "demo.workspace.seeded": "Challenge workspace prepared",
  "demo.reset": "Human reset the challenge demo",
};

function pathView(pathname) {
  const match = pathname.match(/^\/workspace\/(memories|proposals|activity)\/?$/);
  return match?.[1] || "overview";
}

function formatTime(value) {
  if (!value) return "Now";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Now";
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(date);
}

function actorLabel(value) {
  if (!value || value === "webmcp" || value === "local-http") return "ChatGPT";
  if (value === "local-human") return "Human";
  return value;
}

function ShellIcon({ children }) {
  return <span className="workspace-nav-icon" aria-hidden="true">{children}</span>;
}

function StatusBadge({ status }) {
  const normalized = status || "authoritative";
  return <span className={`workspace-status workspace-status-${normalized}`}>{normalized.replaceAll("_", " ")}</span>;
}

function ActivityRow({ event, last }) {
  const label = event.label || ACTIVITY_LABELS[event.event_type] || event.event_type?.replaceAll(".", " ") || "Workspace updated";
  const detail = event.detail || (
    event.event_type === "webmcp.memory.recalled" && Number.isFinite(event.metadata?.result_count)
      ? `${event.metadata.result_count} authoritative ${event.metadata.result_count === 1 ? "memory" : "memories"}`
      : ""
  );
  return (
    <div className="activity-row">
      <div className="activity-rail">
        <span className="activity-dot" />
        {!last ? <span className="activity-line" /> : null}
      </div>
      <time>{formatTime(event.created_at)}</time>
      <div>
        <div className="activity-title">{label}</div>
        {detail ? <div className="activity-detail">{detail}</div> : null}
      </div>
      <span className="activity-actor">{actorLabel(event.actor_id)}</span>
    </div>
  );
}

function EmptyState({ children }) {
  return <div className="workspace-empty">{children}</div>;
}

function ProposalCard({ proposal, readOnly, editing, editedContent, onEdit, onEditedContent, onCancelEdit, onReview }) {
  const pending = proposal.status === "pending";
  return (
    <motion.article layout className={`proposal-card proposal-${proposal.status}`} data-proposal-id={proposal.proposal_id}>
      <div className="proposal-heading">
        <div>
          <div className="eyebrow">
            {pending ? "Proposed change" : proposal.status === "approved" ? "Human approved" : proposal.status}
          </div>
          <h3>{proposal.target?.label || "Memory correction"}</h3>
        </div>
        <StatusBadge status={pending ? "pending_review" : proposal.status} />
      </div>

      {proposal.status === "applied" ? (
        <div className="proposal-transformation">
          <div className="proposal-value muted-value">
            <span>Previous</span>
            <p>{proposal.target?.current_content}</p>
          </div>
          <div className="transformation-arrow" aria-hidden="true">→</div>
          <div className="proposal-value authoritative-value">
            <span>Authoritative</span>
            <p>{proposal.approved_content}</p>
          </div>
        </div>
      ) : proposal.status === "approved" ? (
        <div className="approved-panel">
          <span>Approved value</span>
          <p>{proposal.approved_content}</p>
          <div className="awaiting"><span /> Awaiting application by ChatGPT</div>
        </div>
      ) : (
        <div className="proposal-comparison">
          <div className="proposal-value muted-value">
            <span>Current</span>
            <p>{proposal.target?.current_content}</p>
          </div>
          <div className="proposal-value proposed-value">
            <span>Proposed</span>
            <p>{proposal.proposed_content}</p>
          </div>
        </div>
      )}

      {proposal.reason ? (
        <div className="proposal-reason"><span>Reason</span><p>{proposal.reason}</p></div>
      ) : null}

      <div className="proposal-meta">
        <div><span>Proposed by</span><strong>{actorLabel(proposal.proposed_by?.id)}</strong></div>
        {proposal.reviewed_by ? <div><span>Approved by</span><strong>{actorLabel(proposal.reviewed_by)}</strong></div> : null}
      </div>

      {pending && !readOnly ? (
        editing ? (
          <div className="proposal-editor">
            <label htmlFor={`approved-${proposal.proposal_id}`}>Human-approved value</label>
            <textarea
              id={`approved-${proposal.proposal_id}`}
              aria-label="Human-approved content"
              value={editedContent}
              onChange={(event) => onEditedContent(event.target.value)}
            />
            <div className="proposal-actions">
              <button className="button-quiet" onClick={onCancelEdit} type="button">Cancel</button>
              <button className="button-primary" onClick={() => onReview("approve", editedContent)} type="button">Confirm edit & approve</button>
            </div>
          </div>
        ) : (
          <div className="proposal-actions">
            <button className="button-danger" onClick={() => onReview("reject")} type="button">Reject</button>
            <button className="button-quiet" onClick={onEdit} type="button">Edit &amp; Approve</button>
            <button className="button-primary" onClick={() => onReview("approve")} type="button">Approve</button>
          </div>
        )
      ) : null}

      {proposal.status === "rejected" ? <div className="proposal-message rejected-message">Rejected by {actorLabel(proposal.reviewed_by)}</div> : null}
      {proposal.status === "stale" ? <div className="proposal-message stale-message">The source memory changed. This proposal can no longer be applied.</div> : null}
    </motion.article>
  );
}

function BriefSection({ label, value, items }) {
  const values = items?.map((item) => item.content).filter(Boolean) || [];
  return (
    <div className="brief-section">
      <div className="eyebrow">{label}</div>
      {value ? <p>{value}</p> : values.length ? <ul>{values.slice(0, 4).map((item) => <li key={item}>{item}</li>)}</ul> : <p className="muted-copy">No current {label.toLowerCase()} recorded.</p>}
    </div>
  );
}

export function Workspace() {
  const boot = useMemo(() => readBootConfig(), []);
  const readOnly = boot.mode === "view";
  const project = boot.scope.project || "waggle-webmcp";
  const scope = useMemo(() => ({ ...boot.scope, project }), [boot.scope.agent_id, boot.scope.session_id, project]);
  const scopeRef = useRef(scope);
  const [view, setView] = useState(() => pathView(window.location.pathname));
  const [snapshot, setSnapshot] = useState({ nodes: [], edges: [] });
  const [proposals, setProposals] = useState([]);
  const [brief, setBrief] = useState(null);
  const [activity, setActivity] = useState([]);
  const [selectedMemoryId, setSelectedMemoryId] = useState("");
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [editingProposalId, setEditingProposalId] = useState("");
  const [editedProposalContent, setEditedProposalContent] = useState("");
  const [toast, setToast] = useState("");
  const [loading, setLoading] = useState(true);

  const showToast = (message) => {
    setToast(message);
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => setToast(""), 3000);
  };

  const addLiveActivity = (event) => {
    setActivity((current) => [{
      event_id: `live-${Date.now()}-${Math.random()}`,
      created_at: new Date().toISOString(),
      actor_id: event.actor_id || "webmcp",
      ...event,
    }, ...current].slice(0, 40));
  };

  const replaceProposal = (proposal) => {
    setProposals((current) => [proposal, ...current.filter((item) => item.proposal_id !== proposal.proposal_id)]);
  };

  const loadActivity = async () => {
    if (boot.sampleMode) return;
    const events = await apiRequest("/api/admin/audit-events?limit=80");
    const visibleTypes = new Set(Object.keys(ACTIVITY_LABELS));
    setActivity(events.filter((event) => visibleTypes.has(event.event_type)).slice(0, 40));
  };

  const loadWorkspace = async () => {
    if (boot.sampleMode) return;
    const query = buildScopeQuery(scope);
    const [graphData, proposalData, briefData, events] = await Promise.all([
      apiRequest(`/api/graph${query}${query ? "&" : "?"}include_source_prompt=true`),
      apiRequest(`/api/webmcp/proposals?project_id=${encodeURIComponent(project)}`),
      apiRequest("/api/webmcp/project-brief", { method: "POST", body: JSON.stringify({ project_id: project }) }),
      apiRequest("/api/admin/audit-events?limit=80"),
    ]);
    setSnapshot(graphData);
    setProposals(proposalData.proposals || []);
    setBrief(briefData);
    const visibleTypes = new Set(Object.keys(ACTIVITY_LABELS));
    setActivity(events.filter((event) => visibleTypes.has(event.event_type)).slice(0, 40));
    setLoading(false);
  };

  useEffect(() => {
    scopeRef.current = scope;
    loadWorkspace().catch((error) => { setLoading(false); showToast(error.message); });
  }, []);

  useEffect(() => {
    const onPopState = () => setView(pathView(window.location.pathname));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    if (boot.sampleMode) return undefined;
    const timer = window.setInterval(() => loadActivity().catch(() => undefined), 5000);
    return () => window.clearInterval(timer);
  }, [boot.sampleMode]);

  useEffect(() => {
    if (boot.sampleMode) return;
    Promise.all([
      registerGetProjectBriefTool({
        getScope: () => scopeRef.current,
        onActivity: ({ result }) => {
          setBrief(result);
          addLiveActivity({ event_type: "webmcp.project_brief.read", label: "ChatGPT requested project brief" });
          showToast("Project brief shared with ChatGPT.");
        },
      }),
      registerRecallMemoryTool({
        getScope: () => scopeRef.current,
        onActivity: ({ result_count: resultCount }) => {
          addLiveActivity({ event_type: "webmcp.memory.recalled", label: `ChatGPT recalled ${resultCount} ${resultCount === 1 ? "memory" : "memories"}` });
          showToast(`ChatGPT recalled ${resultCount} authoritative ${resultCount === 1 ? "memory" : "memories"}.`);
        },
      }),
      registerProposeMemoryChangeTool({
        getScope: () => scopeRef.current,
        onActivity: ({ proposal }) => {
          replaceProposal(proposal);
          addLiveActivity({ event_type: "proposal.created", label: "ChatGPT proposed memory change" });
          setView("proposals");
          window.history.replaceState({}, "", "/workspace/proposals");
          showToast("A new proposal is ready for human review.");
        },
      }),
      registerApplyApprovedMemoryChangeTool({
        getScope: () => scopeRef.current,
        onActivity: ({ result }) => {
          replaceProposal(result.proposal);
          addLiveActivity({ event_type: "proposal.applied", label: "Approved memory change applied" });
          loadWorkspace().catch((error) => showToast(error.message));
          showToast(result.already_applied ? "This approved change was already applied." : "Approved change applied to authoritative memory.");
        },
      }),
    ]).catch((error) => showToast(`WebMCP: ${error.message}`));
  }, [boot.sampleMode]);

  const navigate = (nextView) => {
    const nextPath = nextView === "overview" ? "/workspace" : `/workspace/${nextView}`;
    window.history.pushState({}, "", nextPath);
    setView(nextView);
  };

  const reviewProposal = async (proposal, action, approvedContent) => {
    const payload = { action };
    if (approvedContent !== undefined) payload.approved_content = approvedContent;
    const reviewed = await apiRequest(`/api/webmcp/proposals/${encodeURIComponent(proposal.proposal_id)}/review`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    replaceProposal(reviewed);
    setEditingProposalId("");
    setEditedProposalContent("");
    addLiveActivity({
      event_type: action === "reject" ? "proposal.rejected" : approvedContent !== undefined ? "proposal.edited_and_approved" : "proposal.approved",
      actor_id: "local-human",
    });
    showToast(action === "reject" ? "Proposal rejected." : "The exact human-approved value is now frozen.");
  };

  const resetDemo = async () => {
    await apiRequest("/api/webmcp/demo/reset", { method: "POST", body: "{}" });
    setSelectedMemoryId("");
    setEditingProposalId("");
    await loadWorkspace();
    addLiveActivity({ event_type: "demo.reset", actor_id: "local-human" });
    showToast("Demo reset to the original governed-memory fixture.");
  };

  const nodes = snapshot.nodes || [];
  const edges = snapshot.edges || [];
  const authoritativeNodes = nodes.filter(isAuthoritativeNode);
  const pendingCount = proposals.filter((proposal) => proposal.status === "pending").length;
  const { count: decisionCount } = decisionOverview(authoritativeNodes, 5);
  const types = [...new Set(nodes.map((node) => node.node_type).filter(Boolean))].sort();
  const statuses = [...new Set(nodes.map(nodeAuthorityStatus))].sort();
  const filteredMemories = nodes.filter((node) => {
    const status = nodeAuthorityStatus(node);
    const haystack = `${node.label || ""} ${node.content || ""} ${(node.tags || []).join(" ")}`.toLowerCase();
    return (!search.trim() || haystack.includes(search.trim().toLowerCase()))
      && (typeFilter === "all" || node.node_type === typeFilter)
      && (statusFilter === "all" || status === statusFilter);
  });
  const selectedMemory = nodes.find((node) => node.id === selectedMemoryId) || null;
  const selectedStatus = selectedMemory ? nodeAuthorityStatus(selectedMemory) : "unknown";
  const supersedesEdge = selectedMemory ? edges.find((edge) => edge.relationship === "updates" && edge.source_id === selectedMemory.id) : null;
  const supersededByEdge = selectedMemory ? edges.find((edge) => edge.relationship === "updates" && edge.target_id === selectedMemory.id) : null;
  const nodeById = Object.fromEntries(nodes.map((node) => [node.id, node]));
  const selectedProposal = selectedMemory ? proposals.find((proposal) => proposal.result_memory_id === selectedMemory.id || proposal.target?.memory_id === selectedMemory.id) : null;
  const latestActivity = activity.slice(0, 6);

  return (
    <div className="workspace-shell">
      <aside className="workspace-sidebar">
        <button
          aria-label="Waggle — Shared memory"
          className="workspace-brand"
          onClick={() => navigate("overview")}
          type="button"
        >
          <img alt="Waggle" className="brand-logo brand-logo-full" src={waggleLogo} />
          <img alt="" aria-hidden="true" className="brand-logo brand-logo-icon" src={waggleIcon} />
          <small className="brand-subtitle">Shared memory</small>
        </button>
        <nav aria-label="Workspace navigation">
          {NAV_ITEMS.map((item) => (
            <button className={view === item.id ? "active" : ""} key={item.id} onClick={() => navigate(item.id)} type="button">
              <ShellIcon>{item.id === "overview" ? "⌂" : item.id === "memories" ? "◇" : item.id === "proposals" ? "✓" : "↗"}</ShellIcon>
              {item.label}
              {item.id === "proposals" && pendingCount ? <span className="nav-count">{pendingCount}</span> : null}
            </button>
          ))}
        </nav>
        <div className="sidebar-spacer" />
        <a className="graph-studio-link" href={`${boot.apiBaseUrl || ""}/graph?project=${encodeURIComponent(project)}`}>
          <ShellIcon>⌘</ShellIcon>
          <span>Graph Studio<small>Explore lineage</small></span>
          <b>↗</b>
        </a>
        <div className="connection-state"><span /> WebMCP ready</div>
      </aside>

      <main className="workspace-main">
        <header className="workspace-topbar">
          <div>
            <div className="eyebrow">Project</div>
            <strong>{brief?.project?.name || "Waggle WebMCP"}</strong>
          </div>
          <div className="topbar-actions">
            <span className="consumer-badge" aria-label="Consumer: ChatGPT WebMCP"><span>Consumer</span><b>ChatGPT WebMCP</b></span>
            {boot.demoMode ? (
              <span
                className="challenge-demo"
                title="This hosted workspace is an isolated seeded demonstration of Waggle's WebMCP governance experience."
              ><i /> Challenge Demo</span>
            ) : null}
            <span className="human-control"><i /> Human controlled</span>
            {boot.demoMode ? <button className="reset-demo-button" onClick={() => resetDemo().catch((error) => showToast(error.message))} type="button">Reset Demo</button> : null}
            <button className="refresh-button" onClick={() => loadWorkspace().catch((error) => showToast(error.message))} type="button">↻ <span>Refresh</span></button>
          </div>
        </header>

        <div className="workspace-content">
          {loading ? <div className="workspace-loading"><span /> Loading governed memory…</div> : null}

          {!loading && view === "overview" ? (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="workspace-page">
              <section className="workspace-hero">
                <div>
                  <div className="eyebrow">Waggle</div>
                  <h1>Memory humans and agents share.</h1>
                  <p>Both you and ChatGPT operate on the same governed project memory under the same authority rules.</p>
                </div>
                <div className="hero-signal" aria-hidden="true"><span /><span /><span /></div>
              </section>
              <section className="metric-grid">
                <button onClick={() => navigate("memories")} type="button"><span>Memories</span><strong>{authoritativeNodes.length}</strong><small>authoritative</small></button>
                <button onClick={() => navigate("memories")} type="button"><span>Decisions</span><strong>{decisionCount}</strong><small>current choices</small></button>
                <button className={pendingCount ? "attention" : ""} onClick={() => navigate("proposals")} type="button"><span>Pending proposals</span><strong>{pendingCount}</strong><small>{pendingCount ? "needs your review" : "all reviewed"}</small></button>
              </section>
              <div className="overview-grid">
                <section className="workspace-panel brief-panel">
                  <div className="panel-heading"><div><div className="eyebrow">Project brief</div><h2>What everyone should know</h2></div><span className="live-pill"><i /> Live memory</span></div>
                  <BriefSection label="Goal" value={brief?.goal} />
                  <BriefSection label="Current state" items={brief?.current_state} />
                  <BriefSection label="Key decisions" items={brief?.decisions} />
                  <BriefSection label="Constraints" items={brief?.constraints} />
                </section>
                <section className="workspace-panel activity-panel">
                  <div className="panel-heading"><div><div className="eyebrow">Recent activity</div><h2>Human + agent timeline</h2></div><button onClick={() => navigate("activity")} type="button">View all</button></div>
                  {latestActivity.length ? latestActivity.map((event, index) => <ActivityRow event={event} key={event.event_id} last={index === latestActivity.length - 1} />) : <EmptyState>WebMCP activity will appear here as it happens.</EmptyState>}
                </section>
              </div>
            </motion.div>
          ) : null}

          {!loading && view === "proposals" ? (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="workspace-page narrow-page">
              <div className="page-heading"><div><div className="eyebrow">Human review</div><h1>Proposals</h1><p>Agents may suggest. Only you decide what becomes shared truth.</p></div><div className="trust-note"><span>✓</span><div><strong>Approval boundary active</strong><small>Agents cannot edit or bypass an approved payload.</small></div></div></div>
              <div className="proposal-list">
                {proposals.length ? proposals.map((proposal) => (
                  <ProposalCard
                    key={proposal.proposal_id}
                    proposal={proposal}
                    readOnly={readOnly}
                    editing={editingProposalId === proposal.proposal_id}
                    editedContent={editedProposalContent}
                    onEdit={() => { setEditingProposalId(proposal.proposal_id); setEditedProposalContent(proposal.proposed_content); }}
                    onEditedContent={setEditedProposalContent}
                    onCancelEdit={() => setEditingProposalId("")}
                    onReview={(action, content) => reviewProposal(proposal, action, content).catch((error) => showToast(error.message))}
                  />
                )) : <EmptyState>No proposals yet. Ask ChatGPT to suggest a correction to an existing memory.</EmptyState>}
              </div>
            </motion.div>
          ) : null}

          {!loading && view === "activity" ? (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="workspace-page narrow-page">
              <div className="page-heading"><div><div className="eyebrow">Audit trail</div><h1>Activity</h1><p>Every meaningful handoff between ChatGPT, humans, and authoritative memory.</p></div><span className="live-pill"><i /> Live</span></div>
              <section className="workspace-panel full-activity">
                {activity.length ? activity.map((event, index) => <ActivityRow event={event} key={event.event_id} last={index === activity.length - 1} />) : <EmptyState>No governed-memory activity yet.</EmptyState>}
              </section>
            </motion.div>
          ) : null}

          {!loading && view === "memories" ? (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="workspace-page">
              <div className="page-heading"><div><div className="eyebrow">Shared truth</div><h1>Memories</h1><p>Current knowledge and the history it replaced.</p></div><span className="memory-total">{nodes.length} total</span></div>
              <div className="memory-toolbar">
                <input aria-label="Search memories" placeholder="Search memories…" value={search} onChange={(event) => setSearch(event.target.value)} />
                <select aria-label="Memory type" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">All types</option>{types.map((type) => <option key={type} value={type}>{type}</option>)}</select>
                <select aria-label="Memory status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All states</option>{statuses.map((status) => <option key={status} value={status}>{status.replaceAll("_", " ")}</option>)}</select>
              </div>
              <div className="memories-layout">
                <section className="memory-list workspace-panel">
                  {filteredMemories.length ? filteredMemories.map((node) => {
                    const status = nodeAuthorityStatus(node);
                    return <button className={selectedMemoryId === node.id ? "selected" : ""} key={node.id} onClick={() => setSelectedMemoryId(node.id)} type="button"><div><span className="memory-type">{node.node_type}</span><StatusBadge status={status} /></div><strong>{node.label}</strong><p>{node.content}</p><small>{node.project || project} · {formatTime(node.updated_at || node.created_at)}</small></button>;
                  }) : <EmptyState>No memories match these filters.</EmptyState>}
                </section>
                <aside className="memory-inspector workspace-panel">
                  {selectedMemory ? (
                    <>
                      <div className="panel-heading"><div><div className="eyebrow">Memory detail</div><h2>{selectedMemory.label}</h2></div><StatusBadge status={selectedStatus} /></div>
                      <div className="inspector-field"><span>Content</span><p>{selectedMemory.content}</p></div>
                      <div className="inspector-grid">
                        <div><span>Type</span><strong>{selectedMemory.node_type}</strong></div>
                        <div><span>Project</span><strong>{selectedMemory.project || project}</strong></div>
                        <div><span>Source</span><strong>{selectedMemory.metadata?.source_type || selectedMemory.agent_id || "Waggle"}</strong></div>
                        <div><span>Created by</span><strong>{actorLabel(selectedMemory.agent_id || "Waggle")}</strong></div>
                      </div>
                      <div className="inspector-field"><span>Supersedes</span><p>{supersedesEdge ? nodeById[supersedesEdge.target_id]?.content || supersedesEdge.target_id : "—"}</p></div>
                      <div className="inspector-field"><span>Superseded by</span><p>{supersededByEdge ? nodeById[supersededByEdge.source_id]?.content || supersededByEdge.source_id : "—"}</p></div>
                      <div className="inspector-field"><span>Proposal provenance</span><p>{selectedProposal ? `${selectedProposal.proposal_id} · ${selectedProposal.status}` : "No proposal linked"}</p></div>
                      <div className="inspector-field"><span>Evidence</span><p>{(selectedMemory.evidence_records || []).length ? `${selectedMemory.evidence_records.length} supporting record(s)` : "No attached evidence records"}</p></div>
                      <div className="inspector-field"><span>History</span><p>{supersedesEdge || supersededByEdge ? "Native updates lineage preserved" : "Original authoritative memory"}</p></div>
                    </>
                  ) : <EmptyState>Select a memory to inspect its provenance and history.</EmptyState>}
                </aside>
              </div>
            </motion.div>
          ) : null}
        </div>
      </main>

      <AnimatePresence>{toast ? <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 12 }} className="workspace-toast"><span>✓</span>{toast}</motion.div> : null}</AnimatePresence>
    </div>
  );
}
