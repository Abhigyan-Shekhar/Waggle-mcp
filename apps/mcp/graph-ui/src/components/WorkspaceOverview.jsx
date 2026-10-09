import React from "react";
import { ArrowRight, Clock3, Play, ShieldCheck } from "lucide-react";
import { MemoryMapPreview } from "./MemoryMapPreview";
import { decisionOverview, isAuthoritativeNode } from "../lib/authority";

function updatedAt(node) {
  const value = node.updated_at || node.created_at;
  const time = new Date(value || "");
  return Number.isNaN(time.getTime())
    ? "Recently"
    : new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(time);
}

export function WorkspaceOverview({
  activity,
  brief,
  graphHref,
  onNavigate,
  onStartDemo,
  proposals,
  snapshot,
}) {
  const nodes = snapshot.nodes || [];
  const authoritative = nodes.filter(
    (node) => isAuthoritativeNode(node) && node.metadata?.authority !== "source_observation",
  );
  const decisionSummary = decisionOverview(authoritative, 5);
  const decisions = decisionSummary.displayed;
  const recent = [...authoritative]
    .sort((left, right) => String(right.updated_at || right.created_at || "").localeCompare(String(left.updated_at || left.created_at || "")))
    .slice(0, 4);
  const pending = proposals.filter((proposal) => proposal.status === "pending");
  const project = brief?.project || {};
  const repositoryObservations = brief?.repository_context || [];
  const lastUpdated = [...authoritative, ...repositoryObservations]
    .map((item) => item.updated_at)
    .filter(Boolean)
    .sort()
    .at(-1);

  return (
    <div className="workspace-page overview-canvas">
      <section className="overview-intro">
        <div>
          <div className="eyebrow">{project.repository ? "Connected repository" : "Waggle WebMCP"}</div>
          <h1>{project.name || "Shared project memory, governed by humans."}</h1>
          <p>{project.repository || project.root || "ChatGPT operates on the same governed memory you see here."}</p>
          {project.id ? (
            <div className="project-connection-stats" aria-label="Project connection details">
              <span>{authoritative.length} governed memories</span>
              <span>{decisionSummary.count} authoritative decisions</span>
              <span>{pending.length} pending proposals</span>
              <span>{repositoryObservations.length} repository observations</span>
              <span>{lastUpdated ? `Updated ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(lastUpdated))}` : "Ready"}</span>
              <span>{project.identity_source ? `Identity: ${project.identity_source.replace("_", " ")}` : "Scoped project"}</span>
            </div>
          ) : null}
          {onStartDemo ? <button className="see-waggle-button" onClick={onStartDemo} type="button"><Play size={17} /> See Waggle in Action</button> : <p>Ask your agent to call <code>get_project_brief</code> with no arguments to catch up on this repository.</p>}
        </div>
      </section>

      <div className="context-decision-grid">
        <section className="workspace-panel context-panel" data-guide-focus="context">
          <div className="section-heading-inline">
            <div><h2 className="eyebrow">Current Context</h2><h3>What the project is doing now</h3></div>
            <span>{authoritative.length} memories · {decisionSummary.count} decisions</span>
          </div>
          <div className="context-copy">
            <div><span>{brief?.purpose_authority === "source_observation" ? "Repository purpose · source observation" : "Goal"}</span><p>{brief?.goal || "No project goal recorded yet."}</p></div>
            <div><span>Current state</span><p>{brief?.current_state?.slice(0, 3).map((item) => item.content).filter(Boolean).join(" ") || "No current-state memory recorded yet."}</p></div>
          </div>
        </section>

        <section className="workspace-panel decisions-panel" data-guide-focus="decision">
          <div className="section-heading-inline">
            <div><h2 className="eyebrow">Key Decisions</h2><h3>Current authoritative choices</h3></div>
            <button onClick={() => onNavigate("memories")} type="button">View all <ArrowRight size={14} /></button>
          </div>
          {decisions.length ? (
            <ol>{decisions.map((node, index) => <li key={node.id}><span>{index + 1}</span><div><strong>{node.content}</strong><small>{node.label}</small></div></li>)}</ol>
          ) : <p className="muted-copy">No authoritative decisions recorded yet.</p>}
        </section>
      </div>

      {repositoryObservations.length ? (
        <section className="workspace-panel context-panel">
          <div className="section-heading-inline"><div><h2>Repository context</h2><p>Source observations, not approved decisions. Refresh with <code>refresh_project_context</code>.</p></div></div>
          <div className="context-copy">
            {repositoryObservations.filter((item) => ["stack", "components", "storage", "commands"].includes(item.category)).map((item) => (
              <div key={item.memory_id}><span>{item.label}</span><p>{item.content}</p><small>Source: {item.provenance?.path}</small></div>
            ))}
          </div>
          {brief?.repository_conflicts?.length ? <p>Repository changes or possible conflicts need review. Source scans never rewrite approved decisions.</p> : null}
        </section>
      ) : null}

      <MemoryMapPreview graphHref={graphHref} snapshot={snapshot} />

      <div className="overview-lower-grid">
        <section className="workspace-panel recent-memory-panel">
          <div className="section-heading-inline"><div><div className="eyebrow">Recent Memories</div><h2>Latest shared context</h2></div><button onClick={() => onNavigate("memories")} type="button">View all <ArrowRight size={14} /></button></div>
          <div className="compact-rows">
            {recent.map((node) => <button key={node.id} onClick={() => onNavigate("memories", node.id)} type="button"><Clock3 size={14} /><span>{updatedAt(node)}</span><strong>{node.label}</strong><small>{node.content}</small></button>)}
          </div>
        </section>

        <section className={`workspace-panel review-panel ${pending.length ? "has-pending" : ""}`}>
          <div className="section-heading-inline"><div><div className="eyebrow">Pending Human Review</div><h2>{pending.length ? `${pending.length} ${pending.length === 1 ? "proposal" : "proposals"} need you` : "Nothing awaiting review"}</h2></div><ShieldCheck size={20} /></div>
          {pending.length ? (
            <button className="pending-preview" onClick={() => onNavigate("proposals")} type="button"><strong>{pending[0].target?.label || "Memory correction"}</strong><span>{pending[0].proposed_content}</span><small>Review proposal <ArrowRight size={13} /></small></button>
          ) : (
            <div className="review-empty"><p>ChatGPT and other agents can propose changes, but only you can approve what becomes shared truth.</p><button onClick={() => onNavigate("proposals")} type="button">Open proposals <ArrowRight size={14} /></button></div>
          )}
        </section>
      </div>

      <div className="shared-memory-note">
        <ShieldCheck size={18} />
        <div><strong>Same memory. Same context.</strong><span>Both you and ChatGPT operate on the same governed project memory under the same authority rules.</span></div>
        <small>{activity.length ? "Live activity connected" : "Ready for WebMCP activity"}</small>
      </div>
    </div>
  );
}
