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

export function GuidedDemo({ state, graphHref, onCopyPrompt, onExit, onRestart }) {
  if (!state.active) return null;
  const step = DEMO_STEPS[state.step - 1];
  const humanStep = step.tool === "human_review";

  return (
    <aside aria-label="Guided Demo" className="guided-demo">
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
