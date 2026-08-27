import React from "react";
import {
  Check,
  Clipboard,
  ExternalLink,
  RotateCcw,
  ShieldCheck,
  X,
} from "lucide-react";
import { DEMO_STEPS } from "../lib/demo-state";

const SITE_TOOL_NAMES = [
  "get_project_brief",
  "recall_memory",
  "propose_memory_change",
  "apply_approved_memory_change",
];

export function GuidedDemo({ state, graphHref, onCopyPrompt, onExit, onRestart, siteToolsStatus }) {
  if (!state.active) return null;
  const step = DEMO_STEPS[state.step - 1];
  const humanStep = step.tool === "human_review";

  return (
    <aside
      aria-label="Guided Demo"
      className={`guided-demo${humanStep && !state.completed ? " guided-demo-human" : ""}`}
    >
      <div className="guided-demo-header">
        <div><span className="guided-live-dot" /> Guided Demo</div>
        <button aria-label="Exit demo" onClick={onExit} type="button"><X size={16} /> Exit demo</button>
      </div>

      <div className="guided-demo-body">
        {state.completed ? (
          <div className="guided-complete" aria-live="polite">
            <span className="guided-complete-icon"><Check size={22} /></span>
            <div className="eyebrow">Demo complete</div>
            <h2>Human-approved truth, recalled.</h2>
            <p>ChatGPT retrieved the exact value that the human approved and Waggle applied.</p>
            <a className="button-primary guided-graph-button" href={graphHref}>
              Explore lineage in Graph Studio <ExternalLink size={15} />
            </a>
          </div>
        ) : (
          <>
            <div className="guided-progress-copy">Step {state.step} of {DEMO_STEPS.length}</div>
            <div aria-label={`Step ${state.step} of ${DEMO_STEPS.length}`} className="guided-progress">
              {DEMO_STEPS.map((item) => <span className={item.number <= state.step ? "reached" : ""} key={item.number} />)}
            </div>
            <div className="guided-step">
              <div className="eyebrow">{humanStep ? "Your turn" : "Ask ChatGPT"}</div>
              <h2>{step.tool}</h2>
              <p>{step.title}</p>
            </div>

            {state.step === 1 ? (
              <section className={`guided-preflight guided-preflight-${siteToolsStatus.kind}`}>
                <div className="eyebrow">Before you send the prompt</div>
                <h3>Check Site tools before Prompt 1</h3>
                <p>
                  Open this workspace in the latest ChatGPT desktop app's built-in browser.
                  Use ChatGPT Work or Codex with GPT-5.6 Sol or Terra; Luna does not support Site tools.
                </p>
                <div className="guided-preflight-status" aria-live="polite">
                  <span />
                  {siteToolsStatus.kind === "ready"
                    ? `${siteToolsStatus.registeredCount} Site tools registered on this page`
                    : siteToolsStatus.kind === "checking"
                      ? "Checking this page for Site tools…"
                      : "Site tools are unavailable in this browser"}
                </div>
                <p>
                  In the browser address bar, select <strong>Site tools → Available site tools</strong>
                  {" "}and confirm all four tools before continuing.
                </p>
                <ul aria-label="Required Waggle Site tools">
                  {SITE_TOOL_NAMES.map((name) => <li key={name}><Check size={12} /> <code>{name}</code></li>)}
                </ul>
                {siteToolsStatus.kind === "unavailable" || siteToolsStatus.kind === "error" ? (
                  <p className="guided-preflight-help">
                    In ChatGPT, enable <strong>Settings → Browser → Permissions → Enable site tools</strong>,
                    then reload this page. Waggle is a page-level Site tool, not a plugin or connector.
                  </p>
                ) : null}
              </section>
            ) : null}

            <div className="guided-prompt">
              <span>{humanStep ? "Approved value to enter" : "Prompt to ChatGPT"}</span>
              <blockquote>{step.prompt}</blockquote>
              {!humanStep ? (
                <button onClick={() => onCopyPrompt(step.prompt)} type="button">
                  <Clipboard size={15} /> Copy prompt
                </button>
              ) : null}
            </div>

            <div className="guided-wait" aria-live="polite">
              <span className="guided-wait-dot" />
              <div>
                <strong>{humanStep ? "Waiting for your approval" : "Waiting for a real WebMCP event"}</strong>
                <p>{humanStep
                  ? "Edit the proposal and approve it. The exact human-approved payload will be frozen."
                  : "Send the prompt in ChatGPT. This step advances only when Waggle receives the matching tool event."}</p>
              </div>
            </div>
          </>
        )}
      </div>

      <div className="guided-demo-footer">
        <button onClick={onRestart} type="button"><RotateCcw size={16} /> Restart demo</button>
        <div><ShieldCheck size={15} /> Agents suggest. Humans decide.</div>
      </div>
    </aside>
  );
}
