import { strFromU8, unzipSync } from "fflate";

const MAGIC = new Uint8Array([0x57, 0x47, 0x4c, 0x01]);
const STORAGE_PREFIX = "waggle:session-abhi:v1:";
const MAX_ARCHIVE_BYTES = 700 * 1024;
const MAX_EXPANDED_BYTES = 4 * 1024 * 1024;

function storageKey(project) {
  return `${STORAGE_PREFIX}${project}`;
}

function decodeBase64(value) {
  const clean = String(value || "").replace(/^data:[^,]*,/, "").replace(/\s+/g, "");
  if (!clean) throw new Error("INVALID_INPUT: content_base64 is required.");
  let binary;
  try {
    binary = atob(clean);
  } catch {
    throw new Error("INVALID_ABHI: the file content is not valid base64.");
  }
  if (binary.length > MAX_ARCHIVE_BYTES) {
    throw new Error("ABHI_TOO_LARGE: session imports are limited to 700 KB.");
  }
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function parseJsonLines(bytes, memberName) {
  if (!bytes) return [];
  return strFromU8(bytes)
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map((line, index) => {
      try {
        const value = JSON.parse(line);
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
        return value;
      } catch {
        throw new Error(`INVALID_ABHI: ${memberName} line ${index + 1} is not a JSON object.`);
      }
    });
}

async function verifyMemberHashes(archive, manifest) {
  if (!globalThis.crypto?.subtle) {
    throw new Error("SECURE_CONTEXT_REQUIRED: browser cryptography is required to validate .abhi files.");
  }
  for (const memberName of ["nodes.jsonl", "edges.jsonl"]) {
    const expected = String(manifest?.members?.[memberName]?.sha256 || "").replace(/^sha256:/, "").toLowerCase();
    if (!expected) continue;
    const digest = await globalThis.crypto.subtle.digest("SHA-256", archive[memberName]);
    const actual = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
    if (actual !== expected) throw new Error(`INVALID_ABHI: ${memberName} does not match its manifest hash.`);
  }
}

function isCurrent(node, supersededIds) {
  if (supersededIds.has(String(node.id))) return false;
  if (!node.valid_to) return true;
  const expiry = Date.parse(node.valid_to);
  return Number.isNaN(expiry) || expiry > Date.now();
}

function normalizeSnapshot(nodes, edges, project, sourceProject) {
  const ids = new Set();
  const normalizedNodes = nodes.map((node) => {
    const id = String(node.id || "").trim();
    if (!id || ids.has(id)) throw new Error("INVALID_ABHI: every memory must have a unique id.");
    ids.add(id);
    return {
      ...node,
      id,
      label: String(node.label || "Memory"),
      content: String(node.content || ""),
      node_type: String(node.node_type || "note").toLowerCase(),
      tags: Array.isArray(node.tags) ? node.tags.map(String) : [],
      project,
      metadata: {
        ...(node.metadata && typeof node.metadata === "object" ? node.metadata : {}),
        original_project: String(node.project || sourceProject || ""),
        source_type: "session_abhi_import",
      },
    };
  });
  const normalizedEdges = edges.map((edge, index) => {
    const sourceId = String(edge.source_id || "").trim();
    const targetId = String(edge.target_id || "").trim();
    if (!ids.has(sourceId) || !ids.has(targetId)) {
      throw new Error(`INVALID_ABHI: edge ${index + 1} points to a missing memory.`);
    }
    return {
      ...edge,
      id: String(edge.id || `session-edge-${index + 1}`),
      source_id: sourceId,
      target_id: targetId,
      relationship: String(edge.relationship || "relates_to").toLowerCase(),
    };
  });
  const supersededIds = new Set(
    normalizedEdges.filter((edge) => edge.relationship === "updates").map((edge) => edge.target_id),
  );
  return {
    nodes: normalizedNodes.map((node) => ({
      ...node,
      authority_status: isCurrent(node, supersededIds) ? "authoritative" : "superseded",
    })),
    edges: normalizedEdges,
  };
}

function projectName(project) {
  return project.split(/[\s_\-]+/).filter(Boolean).map((part) => part[0]?.toUpperCase() + part.slice(1)).join(" ");
}

function memoryPayload(node) {
  return {
    memory_id: node.id,
    type: node.node_type,
    label: node.label,
    content: node.content,
    status: node.authority_status,
    created_at: node.created_at || "",
    updated_at: node.updated_at || node.created_at || "",
    source: node.metadata?.source_type || node.agent_id || "waggle",
  };
}

export function compileSessionBrief(state) {
  const nodes = state.snapshot.nodes
    .filter((node) => node.authority_status === "authoritative")
    .sort((left, right) => String(right.updated_at || right.created_at || "").localeCompare(String(left.updated_at || left.created_at || "")));
  const hasTag = (node, tags) => node.tags.some((tag) => tags.has(String(tag).toLowerCase()));
  const goals = nodes.filter((node) => hasTag(node, new Set(["goal", "objective"])) || /^(project |product )?goal$/i.test(node.label));
  const constraints = nodes.filter((node) => hasTag(node, new Set(["constraint", "requirement", "guardrail"])) || /^(constraint|requirement|guardrail)/i.test(node.label) || /^(must |must not |do not |never )/i.test(node.content));
  const decisions = nodes.filter((node) => node.node_type === "decision" && !goals.includes(node) && !constraints.includes(node));
  const questions = nodes.filter((node) => node.node_type === "question");
  const reserved = new Set([...goals, ...constraints, ...decisions, ...questions].map((node) => node.id));
  const current = nodes.filter((node) => !reserved.has(node.id));
  const selected = [...goals.slice(0, 1), ...decisions.slice(0, 4), ...constraints.slice(0, 4), ...questions.slice(0, 4), ...current.slice(0, 4)];
  return {
    project: { id: state.project, name: projectName(state.project) },
    goal: goals[0]?.content || "",
    current_state: current.slice(0, 4).map(memoryPayload),
    decisions: decisions.slice(0, 4).map(memoryPayload),
    constraints: constraints.slice(0, 4).map(memoryPayload),
    open_questions: questions.slice(0, 4).map(memoryPayload),
    recent_changes: nodes.slice(0, 4).map(memoryPayload),
    supporting_memory_ids: [...new Set(selected.map((node) => node.id))],
    generated_at: new Date().toISOString(),
    storage: "browser_session_only",
  };
}

export function readSessionWorkspace(project, storage = window.sessionStorage) {
  try {
    const raw = storage.getItem(storageKey(project));
    if (!raw) return null;
    const state = JSON.parse(raw);
    return state?.version === 1 && state.project === project ? state : null;
  } catch {
    return null;
  }
}

export function saveSessionWorkspace(state, storage = window.sessionStorage) {
  const serialized = JSON.stringify(state);
  if (serialized.length > MAX_EXPANDED_BYTES) {
    throw new Error("ABHI_TOO_LARGE: the expanded graph is too large for private session storage.");
  }
  storage.setItem(storageKey(state.project), serialized);
  return state;
}

export function clearSessionWorkspace(project, storage = window.sessionStorage) {
  storage.removeItem(storageKey(project));
}

export async function loadAbhiIntoSession({ contentBase64, fileName = "memory.abhi", project, storage = window.sessionStorage }) {
  if (!String(fileName).toLowerCase().endsWith(".abhi")) {
    throw new Error("INVALID_ABHI: file_name must end in .abhi.");
  }
  const bytes = decodeBase64(contentBase64);
  if (bytes.length < MAGIC.length || MAGIC.some((value, index) => bytes[index] !== value)) {
    throw new Error("INVALID_ABHI: missing Waggle WGL v1 file signature.");
  }
  let archive;
  let declaredExpandedSize = 0;
  const requiredMembers = new Set(["manifest.json", "nodes.jsonl", "edges.jsonl"]);
  try {
    archive = unzipSync(bytes.subarray(MAGIC.length), {
      filter: (member) => {
        if (!requiredMembers.has(member.name)) return false;
        declaredExpandedSize += member.originalSize;
        if (declaredExpandedSize > MAX_EXPANDED_BYTES) {
          throw new Error("ABHI_TOO_LARGE");
        }
        return true;
      },
    });
  } catch (error) {
    if (error?.message === "ABHI_TOO_LARGE") {
      throw new Error("ABHI_TOO_LARGE: the expanded graph exceeds 4 MB.");
    }
    throw new Error("INVALID_ABHI: the archive could not be opened.");
  }
  const expandedSize = Object.values(archive).reduce((total, member) => total + member.length, 0);
  if (expandedSize > MAX_EXPANDED_BYTES) {
    throw new Error("ABHI_TOO_LARGE: the expanded graph exceeds 4 MB.");
  }
  if (!archive["manifest.json"] || !archive["nodes.jsonl"] || !archive["edges.jsonl"]) {
    throw new Error("INVALID_ABHI: manifest.json, nodes.jsonl, and edges.jsonl are required.");
  }
  let manifest;
  try {
    manifest = JSON.parse(strFromU8(archive["manifest.json"]));
  } catch {
    throw new Error("INVALID_ABHI: manifest.json is invalid.");
  }
  if (manifest?.encryption?.enabled) {
    throw new Error("ENCRYPTED_ABHI_UNSUPPORTED: decrypt locally before using the browser-session importer.");
  }
  const major = Number.parseInt(String(manifest?.schema_version || "0").split(".")[0], 10);
  if (major !== 2) throw new Error("INVALID_ABHI: this browser supports .abhi schema version 2.x.");
  await verifyMemberHashes(archive, manifest);
  const nodes = parseJsonLines(archive["nodes.jsonl"], "nodes.jsonl");
  const edges = parseJsonLines(archive["edges.jsonl"], "edges.jsonl");
  if (!nodes.length) throw new Error("INVALID_ABHI: the graph contains no memories.");
  const state = {
    version: 1,
    project,
    original_project: String(manifest.project || ""),
    file_name: String(fileName),
    imported_at: new Date().toISOString(),
    snapshot: normalizeSnapshot(nodes, edges, project, manifest.project),
    proposals: [],
  };
  saveSessionWorkspace(state, storage);
  return {
    status: "loaded_for_browser_session",
    project_id: project,
    original_project: state.original_project,
    file_name: state.file_name,
    node_count: state.snapshot.nodes.length,
    edge_count: state.snapshot.edges.length,
    privacy: "Stored only in this tab's sessionStorage; no Waggle server upload or database write.",
    brief: compileSessionBrief(state),
    snapshot: state.snapshot,
  };
}

function scoreNode(node, query) {
  const tokens = [...new Set(query.toLowerCase().split(/[^a-z0-9]+/).filter((token) => token.length > 1))];
  const haystack = `${node.label} ${node.content} ${node.tags.join(" ")}`.toLowerCase();
  return tokens.reduce((score, token) => score + (haystack.includes(token) ? 1 : 0), 0);
}

export function createSessionApi(project, storage = window.sessionStorage) {
  const get = () => readSessionWorkspace(project, storage);
  const requireState = () => {
    const state = get();
    if (!state) throw new Error("SESSION_ABHI_NOT_LOADED: import a .abhi file for this browser session first.");
    return state;
  };
  return {
    active: () => Boolean(get()),
    getState: get,
    getProjectBrief: () => compileSessionBrief(requireState()),
    recallMemory: ({ query, limit }) => {
      const state = requireState();
      const memories = state.snapshot.nodes
        .filter((node) => node.authority_status === "authoritative")
        .map((node) => ({ node, score: scoreNode(node, query) }))
        .filter(({ score }) => score > 0)
        .sort((left, right) => right.score - left.score)
        .slice(0, limit)
        .map(({ node }) => memoryPayload(node));
      return { query, project_id: project, memories, storage: "browser_session_only" };
    },
    proposeMemoryChange: ({ memoryId, proposedContent, reason, evidenceIds }) => {
      const state = requireState();
      const target = state.snapshot.nodes.find((node) => node.id === memoryId && node.authority_status === "authoritative");
      if (!target) throw new Error("MEMORY_NOT_FOUND: the authoritative memory is not in this session graph.");
      const existing = state.proposals.find((proposal) => proposal.status === "pending" && proposal.target.memory_id === memoryId && proposal.proposed_content === proposedContent);
      if (existing) return existing;
      const proposal = {
        proposal_id: `session_proposal_${crypto.randomUUID().replaceAll("-", "")}`,
        status: "pending",
        project_id: project,
        target: { memory_id: target.id, label: target.label, current_content: target.content, version: target.updated_at || target.created_at || target.id },
        proposed_content: proposedContent,
        reason,
        evidence_ids: evidenceIds,
        proposed_by: { type: "agent", id: "webmcp" },
        created_at: new Date().toISOString(),
        storage: "browser_session_only",
      };
      state.proposals.unshift(proposal);
      saveSessionWorkspace(state, storage);
      return proposal;
    },
    reviewProposal: ({ proposalId, action, approvedContent }) => {
      const state = requireState();
      const proposal = state.proposals.find((item) => item.proposal_id === proposalId);
      if (!proposal || proposal.status !== "pending") throw new Error("PROPOSAL_NOT_PENDING: this proposal cannot be reviewed.");
      proposal.status = action === "approve" ? "approved" : "rejected";
      proposal.reviewed_by = "local-human";
      proposal.reviewed_at = new Date().toISOString();
      if (action === "approve") proposal.approved_content = approvedContent === undefined ? proposal.proposed_content : approvedContent;
      saveSessionWorkspace(state, storage);
      return proposal;
    },
    applyApprovedMemoryChange: ({ proposalId }) => {
      const state = requireState();
      const proposal = state.proposals.find((item) => item.proposal_id === proposalId);
      if (!proposal) throw new Error("PROPOSAL_NOT_FOUND: this proposal is not in the session graph.");
      if (proposal.status === "applied") return { proposal_id: proposalId, status: "applied", authoritative_memory: proposal.authoritative_memory, already_applied: true, proposal };
      if (proposal.status !== "approved") throw new Error("PROPOSAL_NOT_APPROVED: human approval is required first.");
      const previous = state.snapshot.nodes.find((node) => node.id === proposal.target.memory_id);
      if (!previous || previous.authority_status !== "authoritative" || previous.content !== proposal.target.current_content) throw new Error("STALE_PROPOSAL: the source memory changed after proposal creation.");
      previous.authority_status = "superseded";
      previous.valid_to = new Date().toISOString();
      const next = { ...previous, id: `session_memory_${crypto.randomUUID().replaceAll("-", "")}`, content: proposal.approved_content, authority_status: "authoritative", valid_from: new Date().toISOString(), valid_to: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), metadata: { ...previous.metadata, source_type: "human_approved_session_change" } };
      state.snapshot.nodes.push(next);
      state.snapshot.edges.push({ id: `session_edge_${crypto.randomUUID().replaceAll("-", "")}`, source_id: next.id, target_id: previous.id, relationship: "updates", weight: 1, metadata: { source: "session_governance" } });
      proposal.status = "applied";
      proposal.applied_at = new Date().toISOString();
      proposal.result_memory_id = next.id;
      proposal.authoritative_memory = memoryPayload(next);
      saveSessionWorkspace(state, storage);
      return { proposal_id: proposalId, status: "applied", authoritative_memory: proposal.authoritative_memory, already_applied: false, proposal };
    },
  };
}
