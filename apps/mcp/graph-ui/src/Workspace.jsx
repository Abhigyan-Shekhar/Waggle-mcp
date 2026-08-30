import React, { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity as ActivityIcon,
  ArrowRight,
  Clipboard,
  Database,
  FileCheck2,
  Home,
  Network,
  RefreshCw,
} from "lucide-react";
import waggleIcon from "../../../../assets/waggle-icon-ui.png?inline";
import waggleLogo from "../../../../assets/waggle-logo-ui.png?inline";
import { GuidedDemo } from "./components/GuidedDemo";
import { WorkspaceOverview } from "./components/WorkspaceOverview";
import { apiRequest, buildScopeQuery } from "./lib/api";
import { nodeAuthorityStatus } from "./lib/authority";
import { readBootConfig } from "./lib/boot-config";
import {
  clearDemoState,
  createDemoState,
  DEMO_STEPS,
  loadDemoState,
  reduceDemoState,
  saveDemoState,
} from "./lib/demo-state";
import { resolveSiteToolsStatus } from "./lib/site-tools-status";
import {
  clearSessionWorkspace,
  compileSessionBrief,
  createSessionApi,
  loadAbhiIntoSession,
} from "./lib/session-abhi";
import {
  registerApplyApprovedMemoryChangeTool,
  registerGetProjectBriefTool,
  registerLoadAbhiSessionTool,
  registerProposeMemoryChangeTool,
  registerRecallMemoryTool,
} from "./lib/webmcp";

const NAV_ITEMS = [
  { id: "overview", label: "Overview", icon: Home },
  { id: "memories", label: "Memories", icon: Database },
  { id: "proposals", label: "Proposals", icon: FileCheck2 },
  { id: "activity", label: "Activity", icon: ActivityIcon },
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
  "demo.abhi.imported": "Human imported portable memory graph",
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

function ShellIcon({ icon: Icon }) {
  return <span className="workspace-nav-icon" aria-hidden="true"><Icon size={17} strokeWidth={1.8} /></span>;
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

function ProposalCard({ proposal, readOnly, editing, editedContent, applying, onEdit, onEditedContent, onCancelEdit, onReview, onHumanApply, onCopyApplyPrompt }) {
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
          <div className="approved-next-action">
            <div><span className="awaiting-dot" /> Ready to commit the frozen approved value.</div>
            <div className="proposal-actions">
              <button className="button-primary" disabled={applying || readOnly} onClick={onHumanApply} type="button">{applying ? "Applying…" : "Apply approved change"}</button>
              <button aria-label="Copy apply prompt" onClick={onCopyApplyPrompt} type="button"><Clipboard size={14} /> Ask ChatGPT instead</button>
            </div>
          </div>
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
  const sessionApi = useMemo(() => createSessionApi(project, window.sessionStorage), [project]);
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
  const [applyingProposalId, setApplyingProposalId] = useState("");
  const [toast, setToast] = useState("");
  const [loading, setLoading] = useState(true);
  const [demo, setDemo] = useState(() => loadDemoState(window.sessionStorage, project));
  const [siteToolsStatus, setSiteToolsStatus] = useState({ kind: "checking", registeredCount: 0 });
  const importInputRef = useRef(null);
  const [importing, setImporting] = useState(false);

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
    if (boot.sampleMode || sessionApi.active()) return;
    const events = await apiRequest("/api/admin/audit-events?limit=80");
    const visibleTypes = new Set(Object.keys(ACTIVITY_LABELS));
    setActivity(events.filter((event) => visibleTypes.has(event.event_type)).slice(0, 40));
  };

  const loadWorkspace = async () => {
    if (boot.sampleMode) return;
    const sessionState = sessionApi.getState();
    if (sessionState) {
      setSnapshot(sessionState.snapshot);
      setProposals(sessionState.proposals || []);
      setBrief(compileSessionBrief(sessionState));
      setLoading(false);
      return;
    }
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
    if (demo.active) saveDemoState(window.sessionStorage, demo);
    else clearDemoState(window.sessionStorage, project);
  }, [demo, project]);

  useEffect(() => {
    if (boot.sampleMode) return;
    Promise.all([
      registerGetProjectBriefTool({
        getScope: () => scopeRef.current,
        getSessionApi: () => sessionApi,
        onActivity: ({ result }) => {
          setBrief(result);
          addLiveActivity({ event_type: "webmcp.project_brief.read", label: "ChatGPT requested project brief" });
          setDemo((current) => reduceDemoState(current, { type: "webmcp.project_brief.read" }));
          showToast("Project brief shared with ChatGPT.");
        },
      }),
      registerRecallMemoryTool({
        getScope: () => scopeRef.current,
        getSessionApi: () => sessionApi,
        onActivity: ({ result_count: resultCount, result }) => {
          addLiveActivity({ event_type: "webmcp.memory.recalled", label: `ChatGPT recalled ${resultCount} ${resultCount === 1 ? "memory" : "memories"}` });
          const recalledIds = result?.memories?.map((memory) => memory.memory_id || memory.id).filter(Boolean) || [];
          const storageMemoryId = result?.memories?.find((memory) => /storage|sqlite|neo4j/i.test(
            `${memory.label || ""} ${memory.content || ""}`,
          ))?.memory_id || (/(storage|sqlite|neo4j)/i.test(result?.query || "") ? recalledIds[0] : "");
          setDemo((current) => reduceDemoState(current, {
            type: "webmcp.memory.recalled",
            memoryIds: recalledIds,
            storageMemoryId,
          }));
          showToast(`ChatGPT recalled ${resultCount} authoritative ${resultCount === 1 ? "memory" : "memories"}.`);
        },
      }),
      registerProposeMemoryChangeTool({
        getScope: () => scopeRef.current,
        getSessionApi: () => sessionApi,
        onActivity: ({ proposal }) => {
          replaceProposal(proposal);
          addLiveActivity({ event_type: "proposal.created", label: "ChatGPT proposed memory change" });
          setDemo((current) => reduceDemoState(current, {
            type: "proposal.created",
            proposalId: proposal.proposal_id,
            memoryId: proposal.target?.memory_id || "",
          }));
          setView("proposals");
          window.history.replaceState({}, "", "/workspace/proposals");
          showToast("A new proposal is ready for human review.");
        },
      }),
      registerApplyApprovedMemoryChangeTool({
        getScope: () => scopeRef.current,
        getSessionApi: () => sessionApi,
        onActivity: ({ result }) => {
          replaceProposal(result.proposal);
          addLiveActivity({ event_type: "proposal.applied", label: "Approved memory change applied" });
          setDemo((current) => reduceDemoState(current, {
            type: "proposal.applied",
            proposalId: result.proposal_id || result.proposal?.proposal_id,
            memoryId: result.authoritative_memory?.memory_id || result.authoritative_memory?.id || "",
          }));
          loadWorkspace().catch((error) => showToast(error.message));
          showToast(result.already_applied ? "This approved change was already applied." : "Approved change applied to authoritative memory.");
        },
      }),
      registerLoadAbhiSessionTool({
        getScope: () => scopeRef.current,
        loadAbhi: async ({ projectId, fileName, contentBase64 }) => {
          const result = await loadAbhiIntoSession({
            contentBase64,
            fileName,
            project: projectId,
            storage: window.sessionStorage,
          });
          setSnapshot(result.snapshot);
          setProposals([]);
          setBrief(result.brief);
          setSelectedMemoryId("");
          return { ...result, snapshot: undefined };
        },
        onActivity: ({ result }) => {
          addLiveActivity({
            event_type: "demo.abhi.imported",
            actor_id: "webmcp",
            detail: `${result.node_count} memories · browser session only`,
          });
          showToast(`Loaded ${result.node_count} private session memories from ChatGPT.`);
        },
      }),
    ])
      .then((results) => setSiteToolsStatus(resolveSiteToolsStatus(results)))
      .catch((error) => {
        setSiteToolsStatus({ kind: "error", registeredCount: 0 });
        showToast(`WebMCP: ${error.message}`);
      });
  }, [boot.sampleMode]);

  const navigate = (nextView, memoryId = "") => {
    const nextPath = nextView === "overview" ? "/workspace" : `/workspace/${nextView}`;
    window.history.pushState({}, "", nextPath);
    setView(nextView);
    if (memoryId) setSelectedMemoryId(memoryId);
  };

  const reviewProposal = async (proposal, action, approvedContent) => {
    const reviewed = sessionApi.active()
      ? sessionApi.reviewProposal({ proposalId: proposal.proposal_id, action, approvedContent })
      : await apiRequest(`/api/webmcp/proposals/${encodeURIComponent(proposal.proposal_id)}/review`, {
        method: "POST",
        body: JSON.stringify({
          action,
          ...(approvedContent !== undefined ? { approved_content: approvedContent } : {}),
        }),
      });
    replaceProposal(reviewed);
    setEditingProposalId("");
    setEditedProposalContent("");
    addLiveActivity({
      event_type: action === "reject" ? "proposal.rejected" : approvedContent !== undefined ? "proposal.edited_and_approved" : "proposal.approved",
      actor_id: "local-human",
    });
    if (action === "approve" && approvedContent !== undefined) {
      setDemo((current) => reduceDemoState(current, {
        type: "proposal.edited_and_approved",
        proposalId: reviewed.proposal_id,
      }));
    }
    showToast(action === "reject" ? "Proposal rejected." : "The exact human-approved value is now frozen.");
  };

  const humanApplyProposal = async (proposal) => {
    const approvedValue = proposal.approved_content || "the approved value";
    if (!window.confirm(`Apply this exact human-approved value?\n\n${approvedValue}\n\nThis creates a new authoritative memory and preserves the previous one as lineage.`)) {
      return;
    }
    setApplyingProposalId(proposal.proposal_id);
    try {
      const result = sessionApi.active()
        ? sessionApi.applyApprovedMemoryChange({ proposalId: proposal.proposal_id })
        : await apiRequest(`/api/webmcp/proposals/${encodeURIComponent(proposal.proposal_id)}/human-apply`, {
          method: "POST",
          body: JSON.stringify({ project_id: project }),
        });
      replaceProposal(result.proposal);
      addLiveActivity({ event_type: "proposal.applied", actor_id: "local-human", label: "Human applied approved memory change" });
      setDemo((current) => reduceDemoState(current, {
        type: "proposal.applied",
        proposalId: result.proposal_id || result.proposal?.proposal_id,
        memoryId: result.authoritative_memory?.memory_id || result.authoritative_memory?.id || "",
      }));
      await loadWorkspace();
      showToast(result.already_applied ? "This approved change was already applied." : "Approved value applied and prior memory preserved in lineage.");
    } finally {
      setApplyingProposalId("");
    }
  };

  const resetDemo = async () => {
    clearSessionWorkspace(project, window.sessionStorage);
    await apiRequest("/api/webmcp/demo/reset", { method: "POST", body: "{}" });
    setSelectedMemoryId("");
    setEditingProposalId("");
    await loadWorkspace();
    addLiveActivity({ event_type: "demo.reset", actor_id: "local-human" });
    showToast("Demo reset to the original governed-memory fixture.");
  };

  const importAbhi = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".abhi")) {
      showToast("Choose a Waggle .abhi memory file.");
      return;
    }
    if (file.size > 700 * 1024) {
      showToast("This hosted demo accepts .abhi files up to 700 KB.");
      return;
    }
    setImporting(true);
    try {
      const contentBase64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
        reader.onerror = () => reject(reader.error || new Error("Could not read that file."));
        reader.readAsDataURL(file);
      });
      const result = await loadAbhiIntoSession({
        contentBase64,
        fileName: file.name,
        project,
        storage: window.sessionStorage,
      });
      setBrief(result.brief || null);
      setSnapshot(result.snapshot);
      setProposals([]);
      setSelectedMemoryId("");
      clearDemoState(window.sessionStorage, project);
      setDemo(createDemoState(project));
      showToast(`Loaded ${result.node_count} memories privately for this browser session. Nothing was uploaded to Waggle.`);
    } catch (error) {
      showToast(error.message || "The .abhi file could not be imported.");
    } finally {
      setImporting(false);
    }
  };

  const startDemo = async () => {
    await resetDemo();
    navigate("overview");
    setDemo((current) => reduceDemoState(
      createDemoState(current.project || project),
      { type: "demo.started", startedAt: new Date().toISOString() },
    ));
  };

  const exitDemo = () => {
    clearDemoState(window.sessionStorage, project);
    setDemo(createDemoState(project));
    showToast("Guided Demo closed. Your workspace data is unchanged.");
  };

  const copyPrompt = async (prompt) => {
    await navigator.clipboard.writeText(prompt);
    showToast("Prompt copied for ChatGPT.");
  };

  const nodes = snapshot.nodes || [];
  const edges = snapshot.edges || [];
  const pendingCount = proposals.filter((proposal) => proposal.status === "pending").length;
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
  const focusedGraphMemoryId = demo.memoryId || "";
  const graphHref = `${boot.apiBaseUrl || ""}/graph?project=${encodeURIComponent(project)}${focusedGraphMemoryId ? `&focus=${encodeURIComponent(focusedGraphMemoryId)}` : ""}`;
  const privateSessionActive = sessionApi.active();

  return (
    <div className={`workspace-shell ${demo.active ? "demo-active" : ""}`}>
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
              <ShellIcon icon={item.icon} />
              {item.label}
              {item.id === "proposals" && pendingCount ? <span className="nav-count">{pendingCount}</span> : null}
            </button>
          ))}
        </nav>
        <div className="sidebar-spacer" />
        <a className="graph-studio-link" href={graphHref}>
          <ShellIcon icon={Network} />
          <span>Graph Studio<small>Explore lineage</small></span>
          <ArrowRight size={14} />
        </a>
        <div className={`connection-state connection-state-${siteToolsStatus.kind}`}>
          <span />
          {siteToolsStatus.kind === "ready"
            ? `${siteToolsStatus.registeredCount} Site tools registered`
            : siteToolsStatus.kind === "checking"
              ? "Checking Site tools…"
              : "Open in ChatGPT browser"}
        </div>
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
            {privateSessionActive ? <span className="human-control" title="This graph exists only in this browser tab's sessionStorage and is never written to Waggle's hosted database."><i /> Private session graph</span> : null}
            <span className="human-control"><i /> Human controlled</span>
            {boot.demoMode ? <>
              <input accept=".abhi,application/octet-stream" aria-label="Import Waggle .abhi file" className="abhi-file-input" onChange={importAbhi} ref={importInputRef} type="file" />
              <button className="import-abhi-button" disabled={importing} onClick={() => importInputRef.current?.click()} title="Parsed locally and kept only until this browser tab/session closes or you reset the demo." type="button">{importing ? "Loading…" : "Load private .abhi"}</button>
            </> : null}
            {boot.demoMode ? <button className="reset-demo-button" onClick={() => (demo.active ? startDemo() : resetDemo()).catch((error) => showToast(error.message))} type="button">Reset Demo</button> : null}
            <button className="refresh-button" onClick={() => loadWorkspace().catch((error) => showToast(error.message))} type="button"><RefreshCw size={14} /> <span>Refresh</span></button>
          </div>
        </header>

        <div className="workspace-content">
          {loading ? <div className="workspace-loading"><span /> Connecting to Waggle…</div> : null}

          {!loading && view === "overview" ? (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="workspace-page">
              <WorkspaceOverview
                activity={activity}
                brief={brief}
                graphHref={graphHref}
                onNavigate={navigate}
                onStartDemo={() => startDemo().catch((error) => showToast(error.message))}
                proposals={proposals}
                snapshot={snapshot}
              />
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
                    applying={applyingProposalId === proposal.proposal_id}
                    onEdit={() => { setEditingProposalId(proposal.proposal_id); setEditedProposalContent(proposal.proposed_content); }}
                    onEditedContent={setEditedProposalContent}
                    onCancelEdit={() => setEditingProposalId("")}
                    onReview={(action, content) => reviewProposal(proposal, action, content).catch((error) => showToast(error.message))}
                    onHumanApply={() => humanApplyProposal(proposal).catch((error) => showToast(error.message))}
                    onCopyApplyPrompt={() => copyPrompt(DEMO_STEPS[4].prompt).catch((error) => showToast(error.message))}
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

      <GuidedDemo
        graphHref={graphHref}
        onCopyPrompt={(prompt) => copyPrompt(prompt).catch((error) => showToast(error.message))}
        onExit={exitDemo}
        onRestart={() => startDemo().catch((error) => showToast(error.message))}
        siteToolsStatus={siteToolsStatus}
        state={demo}
      />

      <AnimatePresence>{toast ? <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 12 }} className="workspace-toast"><span>✓</span>{toast}</motion.div> : null}</AnimatePresence>
    </div>
  );
}
