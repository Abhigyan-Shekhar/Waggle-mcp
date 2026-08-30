export const SAMPLE_GRAPH_SNAPSHOT = {
  tenant_id: "sample-tenant",
  nodes: [
    {
      id: "node-decision",
      authority_status: "authoritative",
      label: "Dual-layer graph UI decision",
      content: "Use Cytoscape for the graph canvas and expose transcript provenance in the inspector.",
      node_type: "decision",
      tags: ["ui", "graph", "imported"],
      source_prompt: "user: update the graph UI\nassistant: decision recorded",
      evidence_records: [
        {
          evidence_id: "ev-1",
          session_id: "sess-a",
          turn_index: 1,
          source_role: "assistant",
          source_text: "Decision: use Cytoscape and transcript provenance.",
          observed_at: "2026-05-01T03:00:00Z"
        }
      ],
      project: "MCP",
      agent_id: "codex",
      session_id: "sess-a",
      updated_at: "2026-05-01T03:00:00Z",
      created_at: "2026-05-01T03:00:00Z"
    },
    {
      id: "node-fact",
      authority_status: "authoritative",
      label: "Transcript turn-pairs persisted",
      content: "Transcript records are stored alongside extracted memory and can be replayed independently.",
      node_type: "fact",
      tags: ["memory", "transcript"],
      source_prompt: "observe_conversation stores transcript records",
      evidence_records: [
        {
          evidence_id: "ev-2",
          session_id: "sess-a",
          turn_index: 3,
          source_role: "assistant",
          source_text: "Transcript records are persisted for replay retrieval.",
          observed_at: "2026-05-01T03:02:00Z"
        }
      ],
      project: "MCP",
      agent_id: "codex",
      session_id: "sess-a",
      updated_at: "2026-05-01T03:02:00Z",
      created_at: "2026-05-01T03:02:00Z"
    },
    {
      id: "node-widget",
      authority_status: "authoritative",
      label: "Extraction health widget",
      content: "Track how many transcript turn-pairs produced durable memory and list misses.",
      node_type: "concept",
      tags: ["telemetry", "health"],
      source_prompt: "Need extraction telemetry.",
      evidence_records: [
        {
          evidence_id: "ev-3",
          session_id: "sess-a",
          turn_index: 5,
          source_role: "user",
          source_text: "We need extraction health telemetry.",
          observed_at: "2026-05-01T03:04:00Z"
        }
      ],
      project: "MCP",
      agent_id: "codex",
      session_id: "sess-a",
      updated_at: "2026-05-01T03:04:00Z",
      created_at: "2026-05-01T03:04:00Z"
    },
    {
      id: "node-import",
      authority_status: "authoritative",
      label: "Imported ABHI snapshot",
      content: "Imported namespace nodes should glow and retain ABHI provenance.",
      node_type: "entity",
      tags: ["imported", "abhi"],
      source_prompt: "Imported from graph-archive.abhi",
      evidence_records: [
        {
          evidence_id: "ev-4",
          session_id: "sess-b",
          turn_index: 1,
          source_role: "assistant",
          source_text: "Imported graph archive preview.",
          observed_at: "2026-05-01T03:05:00Z"
        }
      ],
      project: "imported/archive",
      agent_id: "claude",
      session_id: "sess-b",
      updated_at: "2026-05-01T03:05:00Z",
      created_at: "2026-05-01T03:05:00Z"
    }
  ],
  edges: [
    { id: "edge-1", source_id: "node-decision", target_id: "node-fact", relationship: "depends_on", weight: 1 },
    { id: "edge-2", source_id: "node-widget", target_id: "node-decision", relationship: "derived_from", weight: 1 },
    { id: "edge-3", source_id: "node-import", target_id: "node-decision", relationship: "relates_to", weight: 0.6 }
  ],
  ui: {}
};

export const SAMPLE_TRANSCRIPTS = [
  {
    id: "t-1",
    session_id: "sess-a",
    project: "MCP",
    agent_id: "codex",
    turn_index: 0,
    role: "user",
    transcript_text: "Rebuild the graph UI and make transcript provenance visible.",
    observed_at: "2026-05-01T02:59:00Z"
  },
  {
    id: "t-2",
    session_id: "sess-a",
    project: "MCP",
    agent_id: "codex",
    turn_index: 1,
    role: "assistant",
    transcript_text: "Decision: use Cytoscape and expose source turn-pairs in the inspector.",
    observed_at: "2026-05-01T03:00:00Z"
  },
  {
    id: "t-3",
    session_id: "sess-a",
    project: "MCP",
    agent_id: "codex",
    turn_index: 2,
    role: "user",
    transcript_text: "I also need a transcript view and a retrieval debugger.",
    observed_at: "2026-05-01T03:01:00Z"
  },
  {
    id: "t-4",
    session_id: "sess-a",
    project: "MCP",
    agent_id: "codex",
    turn_index: 3,
    role: "assistant",
    transcript_text: "Transcript records are persisted for replay retrieval and can feed a debugger.",
    observed_at: "2026-05-01T03:02:00Z"
  },
  {
    id: "t-5",
    session_id: "sess-a",
    project: "MCP",
    agent_id: "codex",
    turn_index: 4,
    role: "user",
    transcript_text: "Add telemetry for turns where extraction misses completely.",
    observed_at: "2026-05-01T03:03:00Z"
  },
  {
    id: "t-6",
    session_id: "sess-a",
    project: "MCP",
    agent_id: "codex",
    turn_index: 5,
    role: "assistant",
    transcript_text: "We need extraction health telemetry and a zero-candidate turn list.",
    observed_at: "2026-05-01T03:04:00Z"
  },
  {
    id: "t-7",
    session_id: "sess-b",
    project: "imported/archive",
    agent_id: "claude",
    turn_index: 0,
    role: "user",
    transcript_text: "Preview this imported ABHI archive before merging it.",
    observed_at: "2026-05-01T03:04:30Z"
  },
  {
    id: "t-8",
    session_id: "sess-b",
    project: "imported/archive",
    agent_id: "claude",
    turn_index: 1,
    role: "assistant",
    transcript_text: "Imported graph archive preview is ready with namespace highlighting.",
    observed_at: "2026-05-01T03:05:00Z"
  }
];

export const SAMPLE_RETRIEVAL = {
  debug: {
    flat_top_nodes: [
      { node_id: "node-decision", label: "Dual-layer graph UI decision", final_score: 0.93, similarity_score: 0.81, recency_score: 0.92, edge_score: 0.66 },
      { node_id: "node-fact", label: "Transcript turn-pairs persisted", final_score: 0.88, similarity_score: 0.74, recency_score: 0.9, edge_score: 0.61 }
    ],
    tiered_top_nodes: [
      { node_id: "node-decision", label: "Dual-layer graph UI decision", final_score: 0.95, similarity_score: 0.84, recency_score: 0.92, edge_score: 0.7 }
    ],
    all_windows: [
      { window_id: "w-a", session_id: "sess-a", routing_score: 0.91, recency: 0.88, similarity: 0.82, title: "UI rebuild", node_count: 3 },
      { window_id: "w-b", session_id: "sess-b", routing_score: 0.64, recency: 0.74, similarity: 0.57, title: "Import preview", node_count: 1 }
    ]
  },
  replay_hits: [
    {
      score: 0.92,
      session_id: "sess-a",
      turn_index: 1,
      role: "assistant",
      transcript_text: "Decision: use Cytoscape and expose source turn-pairs in the inspector.",
      transcript_snippet: "Decision: use Cytoscape and expose source turn-pairs in the inspector."
    },
    {
      score: 0.78,
      session_id: "sess-a",
      turn_index: 3,
      role: "assistant",
      transcript_text: "Transcript records are persisted for replay retrieval and can feed a debugger.",
      transcript_snippet: "Transcript records are persisted for replay retrieval and can feed a debugger."
    }
  ],
  fusion_hits: [
    {
      content: "Dual-layer graph UI decision",
      score: 0.98,
      source_lane: "graph",
      graph_rank: 1,
      replay_rank: 1,
      fused_rank: 1,
      node_id: "node-decision",
      session_id: "sess-a",
      turn_index: 1,
      transcript_snippet: "Decision: use Cytoscape and expose source turn-pairs in the inspector.",
      reasoning: "semantic graph node, graph rank 1, replay rank 1, session sess-a"
    },
    {
      content: "Transcript turn-pairs persisted",
      score: 0.82,
      source_lane: "graph",
      graph_rank: 2,
      replay_rank: 2,
      fused_rank: 2,
      node_id: "node-fact",
      session_id: "sess-a",
      turn_index: 3,
      transcript_snippet: "Transcript records are persisted for replay retrieval and can feed a debugger.",
      reasoning: "semantic graph node, graph rank 2, replay rank 2, session sess-a"
    }
  ],
  token_estimate: 186
};
