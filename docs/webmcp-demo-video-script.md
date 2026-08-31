# Waggle WebMCP Demo Video Script

**Target runtime:** 2:55 maximum

**Format:** 16:9, 1440p or 1080p, narrated screen recording
**Thesis:** **Waggle — humans govern what agents remember.**

## Recording gate

Do not record the final take until the complete public flow has passed inside
actual ChatGPT at least twice. The video must show real WebMCP calls and the
resulting live Workspace state; it may remove waiting time, but it must not
replace calls, responses, memories, proposals, or graph data with staged
content.

Before recording:

- warm `https://waggle-webmcp-api.onrender.com/health/ready` and wait for HTTP 200;
- use ChatGPT's built-in browser with a model and account configuration that supports Site tools;
- open `https://waggle-webmcp.onrender.com/` in ChatGPT's built-in browser;
- select **Site tools → Available site tools** in the address bar and confirm all five Waggle tools are listed before Prompt 1;
- confirm **Settings → Browser → Permissions → Enable site tools** is enabled; do not look for Waggle in the plugin catalog;
- select **Restart demo** and confirm the original Neo4j storage decision is authoritative;
- keep ChatGPT and Waggle in adjacent tabs or side-by-side windows;
- use 100% browser zoom and a viewport where the Workspace, guide rail, and proposal controls are readable;
- hide bookmarks, notifications, personal account details, unrelated tabs, and developer-console output;
- test the microphone, record system audio only if it adds useful tool-call feedback, and leave one second of silence at each cut;
- prepare the code view at `apps/mcp/graph-ui/src/lib/webmcp.js`, centered on `registerOnce` and the five `name` fields;
- confirm no secrets, local filesystem paths, session cookies, or private browser data are visible.

## Timed shot list and narration

| Time | Screen and action | Narration |
|---|---|---|
| **0:00–0:12** | Open on the live Workspace hero. Keep **Challenge Demo**, **Human controlled**, Current Context, and Key Decisions visible. | “Agents remember more every day, but that memory is usually opaque. People cannot easily see what became truth, why it changed, or stop an agent from overwriting it.” |
| **0:12–0:28** | Slowly pan across Current Context, Key Decisions, and the live Memory Map. Select **See Waggle in Action**. | “Waggle turns project memory into a shared workspace. ChatGPT operates on the same governed memory shown here, while humans control what becomes authoritative.” |
| **0:28–0:47** | Copy and send Prompt 1, which explicitly requests `get_project_brief` for `waggle-webmcp`. Show the real tool call, then return to the automatically advanced guide and highlighted Project Brief. | “First, ChatGPT calls `get_project_brief`. The answer is assembled from Waggle’s real project graph, and the guide advances from the WebMCP event—not from a scripted Next button.” |
| **0:47–1:05** | Copy and send Prompt 2, which explicitly requests `recall_memory` for `storage architecture`. Show it returning **Use Neo4j as the primary storage engine.** Briefly show the highlighted authoritative memory. | “Authoritative recall returns the current storage decision and excludes superseded history. Right now, Neo4j is the recorded truth.” |
| **1:05–1:27** | Copy and send Prompt 3, which explicitly requests `propose_memory_change` for the recalled storage memory. Show the real pending proposal card. | “The agent can identify a conflict and propose a replacement, but it cannot write directly. The authoritative memory remains unchanged while the proposal waits for a human.” |
| **1:27–1:50** | In Waggle, select **Edit & Approve**, enter **“Use SQLite by default; Neo4j remains optional.”**, and approve. Hold on the frozen approved payload. | “Now the human edits and approves the exact wording. That approved payload is frozen. The agent cannot modify it, and stale target protection prevents it from overwriting a newer decision.” |
| **1:50–2:10** | Copy and send Prompt 5, which explicitly requests `apply_approved_memory_change` using only the approved proposal ID. If Site-tool security review blocks that final mutation, click the approved card's **Apply approved change** fallback and confirm it. Return to the Previous → Authoritative transformation. | “Application accepts only the approved proposal ID. Site tools review every mutation independently; the reviewer also has a guarded fallback that can commit only the frozen approved value. Waggle creates a new authoritative memory, preserves the previous value, and records a native `updates` relationship with reviewer and proposal provenance.” |
| **2:10–2:25** | Copy and send Prompt 6, which explicitly requests `recall_memory` again. Show the human-approved SQLite value and the guide completion state. | “Ask the same question again and ChatGPT now retrieves the exact human-approved truth: SQLite by default, with Neo4j optional.” |
| **2:25–2:38** | Open Activity and scroll just enough to show brief, recall, proposal, approval, application, and final recall events. | “The complete sequence is visible in the activity trail, so the human can audit both agent actions and governance decisions.” |
| **2:38–2:50** | Select **Explore lineage in Graph Studio**. Hold on the focused changed-memory lineage, previous and authoritative nodes, real `updates` edge, and **Show full graph** control. | “Graph Studio shows what actually changed in the same isolated workspace: previous state, new authority, lineage, and provenance—not a mocked diagram.” |
| **2:50–2:56** | Cut to `apps/mcp/graph-ui/src/lib/webmcp.js`. Highlight `modelContext.registerTool` and quickly reveal the four registered names. | “Under the hood, these are four registered WebMCP tools backed by Waggle’s existing graph and governance services.” |
| **2:56–2:59** | End card over the Waggle logo and warm workspace background. | “Waggle — humans govern what agents remember.” |

## Exact interaction prompts

Use the prompts exactly as displayed by the Guided Demo:

```text
Call the Waggle Site tool `get_project_brief` with `project_id`: `waggle-webmcp`. Use its result to catch me up; do not answer from chat history.
Call the Waggle Site tool `recall_memory` with `project_id`: `waggle-webmcp`, `query`: `storage architecture`, and `limit`: 5. Report the authoritative decision from the tool result.
Using the authoritative storage memory returned in the previous Waggle tool result, call the Waggle Site tool `propose_memory_change` for `project_id`: `waggle-webmcp`. Propose a local-first replacement, but do not change authoritative memory directly.
Use SQLite by default; Neo4j remains optional.
Call the Waggle Site tool `apply_approved_memory_change` with the `proposal_id` I approved in Waggle. Do not supply replacement content.
Call the Waggle Site tool `recall_memory` with `project_id`: `waggle-webmcp`, `query`: `storage architecture`, and `limit`: 5. Confirm the current authoritative decision from the tool result; do not answer from chat history.
```

The fourth line is entered by the human in Waggle, not sent as a ChatGPT
prompt.

## Edit plan

- Use hard cuts for model or Render waiting time. Retain the prompt submission,
  visible tool identity, successful result, and corresponding Workspace update
  in chronological order.
- Do not speed up cursor movement or narration. A judge must be able to read the
  decision, approved payload, final recall, and Graph Studio lineage at normal
  playback speed.
- Use at most three restrained callouts: **Agent reads**, **Human approves**,
  and **Authoritative memory changes**.
- Avoid decorative transitions, background music that competes with narration,
  stock footage, mock WebMCP responses, or a long title animation.
- If a take exceeds 2:59, shorten pauses and the opening problem statement.
  Do not remove human approval, corrected recall, provenance, Graph Studio, or
  the registration-code shot.

## Final review checklist

- [ ] Total duration is under three minutes.
- [ ] Narration starts with the problem within the first three seconds.
- [ ] Audio is clear and free of clipping, keyboard noise, and long silence.
- [ ] Text is readable at normal YouTube playback size.
- [ ] The live Workspace and all five WebMCP tool names are visible.
- [ ] Project brief and authoritative recall are shown.
- [ ] Proposal creation leaves the original memory unchanged.
- [ ] Human Edit & Approve and the exact frozen payload are shown.
- [ ] Application uses only the approved proposal ID, whether it completes through the Site tool or the explicit human fallback.
- [ ] Corrected recall returns the human-approved value.
- [ ] Activity shows the complete sequence.
- [ ] Graph Studio shows the real focused `updates` lineage and provenance.
- [ ] The actual WebMCP registration code appears briefly.
- [ ] No secrets, private data, cookies, local paths, or debug errors are visible.
- [ ] The ending line is **“Waggle — humans govern what agents remember.”**
- [ ] The uploaded YouTube video is public, plays without login, and has working audio.

## Failed-take conditions

Discard the take if a tool is simulated, the guide advances without its real
event, the approved text differs between review and application, final recall
returns the old value, Activity is incomplete, Graph Studio shows another
session's graph, a raw backend error appears, or any private information is
visible. Reset only the current demo session, resolve the issue, and record the
entire governed-memory sequence again.
