from __future__ import annotations

from datetime import UTC, datetime

from waggle.models import (
    AbhiChunkLoadResult,
    AbhiDiffResult,
    AbhiInspectResult,
    AbhiMergeResult,
    AbhiQueryResult,
    AbhiValidationResult,
    ConflictEntry,
    ConflictListResult,
    ContextBundleExportResult,
    GraphDiffResult,
    GraphStats,
    Node,
    NodeHistoryResult,
    ObservationResult,
    PrimeContextResult,
    SubgraphResult,
    TimelineResult,
    TopicResult,
)


def _format_updated_ago(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "unknown"

    now = datetime.now(UTC)
    delta = max((now - timestamp.astimezone(UTC)).total_seconds(), 0.0)
    if delta < 60:
        return "just now"
    if delta < 3600:
        minutes = max(1, int(delta // 60))
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if delta < 86400:
        hours = max(1, int(delta // 3600))
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = max(1, int(delta // 86400))
    return f"{days} day{'s' if days != 1 else ''} ago"


def serialize_subgraph(result: SubgraphResult) -> str:
    """Convert a subgraph result into readable text for an LLM."""
    if result.retrieval_mode == "verbatim":
        if not result.replay_hits:
            return "=== Memory Replay Results: No results found ==="
        lines = [
            f"=== Memory Replay Results ({len(result.replay_hits)} hits) ===",
            "",
            "[TRANSCRIPT HITS]",
        ]
        for hit in result.replay_hits:
            lines.append(
                f"• (session: {hit.session_id or 'n/a'}, turn: {hit.turn_index}, role: {hit.role or 'unknown'}) "
                f"{hit.transcript_snippet or hit.transcript_text} [score={hit.score:.3f}]"
            )
        lines.extend(["", "=== End Results ==="])
        return "\n".join(lines)

    if result.retrieval_mode == "hybrid" and result.hybrid_hits:
        lines = [
            f"=== Hybrid Retrieval Results ({len(result.hybrid_hits)} hits) ===",
            "",
            "[TOP HITS]",
        ]
        for index, hit in enumerate(result.hybrid_hits, start=1):
            reasoning = f" reason={hit.reasoning_from_reranker}" if hit.reasoning_from_reranker else ""
            explanation = (
                " | ".join(f"{k}={v}" for k, v in hit.score_explanation.items()) if hit.score_explanation else "n/a"
            )
            lines.append(
                f"• #{index} [{hit.source}] {hit.content[:400]} [score={hit.score:.4f}] "
                f"(turn_pair={hit.turn_pair_id or 'n/a'}, node_ids={hit.node_ids}){reasoning} "
                f"[score_explanation: {explanation}]"
            )
        lines.extend(["", "=== End Results ==="])
        return "\n".join(lines)

    if not result.nodes and not result.fusion_hits:
        return "=== Memory Graph: No results found ==="

    lines = [
        f"=== Memory Graph Results ({len(result.nodes)} nodes, {len(result.edges)} edges, mode={result.retrieval_mode}) ===",
        "",
        "[NODES]",
    ]

    for node in result.nodes:
        tags_suffix = f" tags:{node.tags}" if node.tags else ""
        score_suffix = ""
        if node.final_score is not None:
            score_suffix = (
                f"\n  Score: {node.final_score:.2f} "
                f"(similarity: {(node.similarity_score or 0.0):.2f}, "
                f"recency: {(node.recency_score or 0.0):.2f}, "
                f"edge: {(node.edge_score or 0.0):.2f})"
                f"\n  Updated: {_format_updated_ago(node.updated_at)}"
            )
        lines.append(
            f'• (id: {node.id[:8]}) [{node.node_type.value}] "{node.label}" — {node.content} '
            f"(created: {node.created_at.strftime('%Y-%m-%d')}, accessed: {node.access_count} times){tags_suffix}{score_suffix}"
        )

    lines.append("")
    lines.append("[RELATIONSHIPS]")
    if result.edges:
        label_map = {node.id: node.label for node in result.nodes}
        for edge in result.edges:
            source_label = label_map.get(edge.source_id, edge.source_id[:8])
            target_label = label_map.get(edge.target_id, edge.target_id[:8])
            lines.append(f'• "{source_label}" --[{edge.relationship}]--> "{target_label}"')
    else:
        lines.append("• No connecting relationships in this subgraph.")

    if result.replay_hits:
        lines.extend(["", "[REPLAY HITS]"])
        for hit in result.replay_hits[:10]:
            lines.append(
                f"• (session: {hit.session_id or 'n/a'}, turn: {hit.turn_index}, role: {hit.role or 'unknown'}) "
                f"{hit.transcript_snippet or hit.transcript_text} [score={hit.score:.3f}]"
            )
    if result.fusion_hits:
        lines.extend(["", "[FUSION RANKING]"])
        for hit in result.fusion_hits[:10]:
            lines.append(
                f"• #{hit.fused_rank} [{hit.source_lane}] {hit.content} "
                f"(graph_rank={hit.graph_rank}, replay_rank={hit.replay_rank}, score={hit.score:.4f})"
            )

    lines.extend(["", "=== End Results ==="])
    return "\n".join(lines)


def serialize_abhi_validation(result: AbhiValidationResult) -> str:
    lines = [
        "=== ABHI Validation ===",
        f"Valid: {'yes' if result.valid else 'no'}",
        f"Nodes: {result.node_count}",
        f"Edges: {result.edge_count}",
        f"Spec version: {result.abhi_spec_version}",
    ]
    if result.content_hash:
        lines.append(f"Content hash: {result.content_hash}")
    if result.errors:
        lines.extend(["", "[ERRORS]"])
        lines.extend(f"• {error}" for error in result.errors)
    if result.warnings:
        lines.extend(["", "[WARNINGS]"])
        lines.extend(f"• {warning}" for warning in result.warnings)
    lines.append("=== End ABHI Validation ===")
    return "\n".join(lines)


def serialize_abhi_inspect(result: AbhiInspectResult) -> str:
    lines = [
        "=== ABHI Inspect ===",
        f"Tenant: {result.tenant_id or 'n/a'}",
        f"Nodes: {result.node_count}",
        f"Edges: {result.edge_count}",
        f"Schema version: {result.schema_version}",
        f"Spec version: {result.abhi_spec_version}",
        f"Constraints: {result.constraint_count}",
        f"Versions: {result.version_count}",
        f"Saved queries: {result.query_count}",
        f"Events: {result.event_count}",
        f"Chunks: {result.chunk_count}",
        f"Load strategy: {result.load_strategy}",
    ]
    if result.preload_chunks:
        lines.append(f"Preload chunks: {', '.join(result.preload_chunks)}")
    if result.node_types:
        lines.append(f"Node types: {', '.join(result.node_types)}")
    if result.edge_types:
        lines.append(f"Edge types: {', '.join(result.edge_types)}")
    if result.content_hash:
        lines.append(f"Content hash: {result.content_hash}")
    lines.append("=== End ABHI Inspect ===")
    return "\n".join(lines)


def serialize_abhi_diff(result: AbhiDiffResult) -> str:
    lines = [
        "=== ABHI Diff ===",
        f"File A: {result.input_path_a}",
        f"File B: {result.input_path_b}",
        f"Nodes added: {len(result.nodes_added)}",
        f"Nodes removed: {len(result.nodes_removed)}",
        f"Nodes updated: {len(result.nodes_updated)}",
        f"Edges added: {len(result.edges_added)}",
        f"Edges removed: {len(result.edges_removed)}",
        f"Edges updated: {len(result.edges_updated)}",
    ]
    if result.semantic_changes:
        lines.extend(["", "[SEMANTIC CHANGES]"])
        lines.extend(f"• {item}" for item in result.semantic_changes)
    lines.append("=== End ABHI Diff ===")
    return "\n".join(lines)


def serialize_abhi_merge(result: AbhiMergeResult) -> str:
    lines = [
        "=== ABHI Merge ===",
        f"Base: {result.base_input_path}",
        f"Left: {result.left_input_path}",
        f"Right: {result.right_input_path}",
        f"Output: {result.output_path}",
        f"Strategy: {result.merge_strategy}",
        f"Nodes merged: {result.nodes_merged}",
        f"Edges merged: {result.edges_merged}",
    ]
    if result.content_hash:
        lines.append(f"Content hash: {result.content_hash}")
    if result.conflicts:
        lines.extend(["", "[CONFLICTS]"])
        lines.extend(f"• {item}" for item in result.conflicts)
    lines.append("=== End ABHI Merge ===")
    return "\n".join(lines)


def serialize_abhi_query(result: AbhiQueryResult) -> str:
    lines = [
        "=== ABHI Query ===",
        f"Input: {result.input_path}",
        f"Query: {result.query}",
        f"Nodes matched: {result.node_count}",
        f"Edges matched: {result.edge_count}",
    ]
    if result.chunk_ids:
        lines.append(f"Chunks scanned: {', '.join(result.chunk_ids)}")
    elif result.scanned_chunk_count:
        lines.append(f"Chunks scanned: {result.scanned_chunk_count}")
    if result.summary:
        lines.append(result.summary)
    if result.executed_actions:
        lines.extend(["", "[EVENT ACTIONS]"])
        lines.extend(f"• {item}" for item in result.executed_actions)
    lines.append("=== End ABHI Query ===")
    return "\n".join(lines)


def serialize_abhi_chunk_load(result: AbhiChunkLoadResult) -> str:
    lines = [
        "=== ABHI Chunk Load ===",
        f"Input: {result.input_path}",
        f"Load strategy: {result.load_strategy}",
        f"Available chunks: {result.available_chunk_count}",
        f"Loaded chunks: {', '.join(result.chunk_ids) if result.chunk_ids else 'none'}",
        f"Nodes loaded: {result.node_count}",
        f"Edges loaded: {result.edge_count}",
    ]
    if result.query:
        lines.append(f"Query selector: {result.query}")
    lines.append("=== End ABHI Chunk Load ===")
    return "\n".join(lines)


def serialize_stats(stats: GraphStats) -> str:
    lines = [
        "=== Memory Graph Stats ===",
        f"Total nodes: {stats.total_nodes}",
        f"Total edges: {stats.total_edges}",
        f"Repos: {stats.total_repos}",
        f"Context windows: {stats.total_context_windows}",
        f"Cross-window edges: {stats.total_context_window_edges}",
        f"Windows with embeddings: {stats.windows_with_embeddings} (stale: {stats.windows_with_stale_embeddings})",
        "",
        "[NODE TYPES]",
    ]

    for node_type, count in stats.node_type_breakdown.items():
        lines.append(f"• {node_type}: {count}")

    lines.extend(["", "[CONTEXT WINDOWS]"])
    if stats.context_window_status_breakdown:
        for status, count in stats.context_window_status_breakdown.items():
            lines.append(f"• {status}: {count}")
    else:
        lines.append("• No context windows stored yet.")

    lines.extend(["", "[CROSS-WINDOW EDGE TYPES]"])
    if stats.context_window_edge_type_breakdown:
        for edge_type, count in stats.context_window_edge_type_breakdown.items():
            lines.append(f"• {edge_type}: {count}")
    else:
        lines.append("• No cross-window edges stored yet.")

    lines.extend(["", "[MOST CONNECTED]"])
    if stats.most_connected_nodes:
        for node in stats.most_connected_nodes:
            lines.append(f'• "{node.label}" ({node.node_type.value}) — {node.connection_count} connections')
    else:
        lines.append("• No nodes stored yet.")

    lines.extend(["", "[MOST RECENT]"])
    if stats.most_recent_nodes:
        for node in stats.most_recent_nodes:
            lines.append(
                f'• "{node.label}" ({node.node_type.value}) — updated {node.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC")}'
            )
    else:
        lines.append("• No nodes stored yet.")

    lines.append("=== End Stats ===")
    return "\n".join(lines)


def serialize_recent_nodes(nodes: list[Node]) -> str:
    if not nodes:
        return "=== Recent Memory Nodes: No nodes stored ==="

    lines = ["=== Recent Memory Nodes ==="]
    for node in nodes:
        lines.append(
            f'• (id: {node.id[:8]}) [{node.node_type.value}] "{node.label}" — '
            f"updated {node.updated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
    lines.append("=== End Recent Nodes ===")
    return "\n".join(lines)


def serialize_observation_result(result: ObservationResult) -> str:
    lines = [
        "=== Conversation Observation ===",
        f"Turn ID: {result.turn_id}",
        f"Verbatim stored: {result.verbatim_stored}",
        f"Stored nodes: {len(result.stored_nodes)}",
        f"Nodes extracted: {result.nodes_extracted}",
        f"Edges inferred: {result.edges_inferred}",
        f"Created: {result.created_count}",
        f"Reused: {result.reused_count}",
    ]
    if result.extraction_errors:
        lines.extend(["", "[EXTRACTION DIAGNOSTICS]"])
        for error in result.extraction_errors:
            lines.append(f"• {error}")
    if result.stored_nodes:
        lines.extend(["", "[STORED]"])
        for node in result.stored_nodes:
            lines.append(f'• [{node.node_type.value}] "{node.label}"')
    if result.conflicts:
        lines.extend(["", "[CONFLICTS]"])
        for conflict in result.conflicts:
            lines.append(f'• "{conflict.other_node_label}" — {conflict.reason}')
    lines.append("=== End Observation ===")
    return "\n".join(lines)


def serialize_node_history(result: NodeHistoryResult) -> str:
    node = result.node
    lines = [
        "=== Node History ===",
        f'Node: "{node.label}" [{node.node_type.value}]',
        f"Evidence records: {len(node.evidence_records)}",
    ]
    if node.valid_from or node.valid_to:
        lines.append(
            f"Validity: {node.valid_from.isoformat() if node.valid_from else 'open'} -> "
            f"{node.valid_to.isoformat() if node.valid_to else 'open'}"
        )
    if node.evidence_records:
        lines.extend(["", "[EVIDENCE]"])
        for record in node.evidence_records[:5]:
            lines.append(
                f"• ({record.source_role or 'unknown'} turn {record.turn_index}) {record.source_text or node.content}"
            )
    lines.extend(["", "[RELATED NODES]"])
    if result.related_nodes:
        for related in result.related_nodes:
            lines.append(f'• [{related.node_type.value}] "{related.label}"')
    else:
        lines.append("• No related nodes.")
    lines.append("=== End Node History ===")
    return "\n".join(lines)


def serialize_timeline(result: TimelineResult) -> str:
    lines = [
        "=== Timeline ===",
        f"Scope: {result.scope or 'tenant'}",
        f"Items: {len(result.items)}",
    ]
    if result.items:
        lines.extend(["", "[ITEMS]"])
        for item in result.items:
            anchor = f" node={item.node_id[:8]}" if item.node_id else ""
            edge = f" edge={item.edge_id[:8]}" if item.edge_id else ""
            recency = f" recency={item.recency_score:.3f}" if item.recency_score is not None else ""
            lines.append(
                f"• {item.timestamp.isoformat()} [{item.kind}] {item.label} — {item.summary}{anchor}{edge}{recency}"
            )
    else:
        lines.append("No timeline items.")
    lines.append("=== End Timeline ===")
    return "\n".join(lines)


def serialize_conflict_entry(entry: ConflictEntry) -> str:
    lines = [
        "=== Conflict Entry ===",
        f'Conflict: "{entry.source_node.label}" --[{entry.edge.relationship}]--> "{entry.target_node.label}"',
        f"Resolved: {'yes' if entry.resolved else 'no'}",
    ]
    if entry.resolution_note:
        lines.append(f"Resolution note: {entry.resolution_note}")
    if entry.resolved_at is not None:
        lines.append(f"Resolved at: {entry.resolved_at.isoformat()}")
    lines.append("=== End Conflict Entry ===")
    return "\n".join(lines)


def serialize_conflicts(result: ConflictListResult) -> str:
    lines = [
        "=== Conflicts ===",
        f"Include resolved: {'yes' if result.include_resolved else 'no'}",
        f"Conflicts: {len(result.conflicts)}",
    ]
    if result.conflicts:
        lines.extend(["", "[CONFLICTS]"])
        for entry in result.conflicts:
            resolved_suffix = " resolved" if entry.resolved else " unresolved"
            lines.append(
                f'• "{entry.source_node.label}" --[{entry.edge.relationship}]--> '
                f'"{entry.target_node.label}" ({resolved_suffix.strip()})'
            )
    else:
        lines.append("No matching conflicts.")
    lines.append("=== End Conflicts ===")
    return "\n".join(lines)


def serialize_context_bundle_export(result: ContextBundleExportResult) -> str:
    lines = [
        "=== Context Bundle Export ===",
        f"Mode: {result.mode}",
        f"Retrieval mode: {result.retrieval_mode}",
        f"Tenant: {result.tenant_id}",
        f"Project: {result.project or 'n/a'}",
        f"Query: {result.query or 'n/a'}",
        f"Nodes: {result.node_count}",
        f"Edges: {result.edge_count}",
    ]
    if result.markdown_path:
        lines.append(f"Markdown: {result.markdown_path}")
    if result.json_path:
        lines.append(f"JSON: {result.json_path}")
    if result.summary:
        lines.extend(["", result.summary])
    lines.append("=== End Context Bundle Export ===")
    return "\n".join(lines)


def serialize_graph_diff(result: GraphDiffResult) -> str:
    lines = [
        f"=== Graph Diff Since {result.since} ===",
        f"Nodes added: {len(result.added_nodes)}",
        f"Nodes updated: {len(result.updated_nodes)}",
        f"Edges created: {len(result.created_edges)}",
        f"Contradictions detected: {len(result.contradiction_edges)}",
    ]
    if result.added_nodes:
        lines.extend(["", "[ADDED NODES]"])
        for node in result.added_nodes:
            lines.append(f'• [{node.node_type.value}] "{node.label}"')
    if result.updated_nodes:
        lines.extend(["", "[UPDATED NODES]"])
        for node in result.updated_nodes:
            lines.append(f'• [{node.node_type.value}] "{node.label}"')
    if result.created_edges:
        lines.extend(["", "[CREATED EDGES]"])
        for edge in result.created_edges:
            lines.append(f"• {edge.source_id[:8]} --[{edge.relationship}]--> {edge.target_id[:8]}")
    if result.contradiction_edges:
        lines.extend(["", "[CONTRADICTIONS]"])
        for edge in result.contradiction_edges:
            lines.append(f"• {edge.source_id[:8]} contradicts {edge.target_id[:8]}")
    lines.append("=== End Diff ===")
    return "\n".join(lines)


def serialize_prime_context(result: PrimeContextResult) -> str:
    if not result.nodes:
        return "=== Prime Context: No memory available ==="

    lines = [
        "=== Prime Context ===",
        result.summary,
        "",
        "[NODES]",
    ]
    for node in result.nodes:
        lines.append(f'• [{node.node_type.value}] "{node.label}" — {node.content}')
    lines.extend(["", "[RELATIONSHIPS]"])
    if result.edges:
        label_map = {node.id: node.label for node in result.nodes}
        for edge in result.edges:
            source_label = label_map.get(edge.source_id, edge.source_id[:8])
            target_label = label_map.get(edge.target_id, edge.target_id[:8])
            lines.append(f'• "{source_label}" --[{edge.relationship}]--> "{target_label}"')
    else:
        lines.append("• No connecting relationships in this brief.")
    lines.append("=== End Prime Context ===")
    return "\n".join(lines)


def serialize_topics(result: TopicResult) -> str:
    if not result.clusters:
        return "=== Topics: No topics detected ==="

    lines = [
        f"=== Topics ({result.total_clusters} clusters) ===",
    ]
    for cluster in result.clusters:
        tag_suffix = f" tags:{cluster.top_tags}" if cluster.top_tags else ""
        lines.append(f'• Cluster {cluster.cluster_id}: "{cluster.label}" — {cluster.node_count} nodes{tag_suffix}')
        for node in cluster.nodes[:5]:
            lines.append(f'  - [{node.node_type.value}] "{node.label}"')
    lines.append("=== End Topics ===")
    return "\n".join(lines)
