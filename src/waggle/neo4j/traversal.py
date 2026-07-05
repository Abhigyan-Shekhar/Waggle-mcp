from __future__ import annotations

import base64
import json
from collections import deque
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np

from waggle.abhi import (
    ABHI_ENCRYPTION_ALGORITHM,
    ABHI_SPEC_VERSION,
    abhi_to_snapshot,
    diff_abhi_files,
    dispatch_abhi_event,
    filter_snapshot_by_scope,
    inspect_abhi_document,
    load_abhi_chunk_file,
    load_abhi_document,
    merge_abhi_files,
    query_abhi_file,
    validate_abhi_document,
    write_abhi_document,
)
from waggle.context_bundle import build_context_bundle, build_query_summary, export_context_bundle_files
from waggle.errors import ValidationFailure
from waggle.graph.base import (
    MemoryGraphBase,
    _decode_evidence_records,
    _decode_metadata,
    _parse_datetime,
    _scope_matches,
)
from waggle.intelligence import (
    infer_label,
    infer_node_type,
    infer_relationship,
    infer_temporal_hints,
    lexical_overlap,
    parse_since_value,
    score_node,
    split_atomic_items,
    summarize_topic,
    temporal_score_adjustment,
    tokenize_text,
    within_time_window,
)
from waggle.markdown_vault import (
    evidence_from_lines,
    iter_vault_documents,
    render_node_document,
    slugify,
    vault_filename,
)
from waggle.models import (
    AbhiChunkLoadResult,
    AbhiDiffResult,
    AbhiExportResult,
    AbhiImportResult,
    AbhiInspectResult,
    AbhiMergeResult,
    AbhiQueryResult,
    AbhiValidationResult,
    BackupResult,
    ContextBundleExportResult,
    ContextScopeResult,
    ContextTimelineItem,
    ContextWindow,
    ContextWindowEdge,
    Edge,
    FusionHit,
    GraphDiffResult,
    ImportResult,
    MarkdownVaultExportResult,
    MarkdownVaultImportResult,
    Node,
    NodeHistoryResult,
    NodeType,
    PrimeContextResult,
    RelationType,
    ReplayHit,
    SubgraphResult,
    TimelineResult,
    TopicCluster,
    TopicResult,
    utc_now,
)

from .base import SCHEMA_VERSION


class Neo4jTraversalMixin(MemoryGraphBase):
    def get_node(self, node_id: str) -> Node:
        with self._lock, self._session() as session:
            node = self._fetch_node(session, node_id)
            if node is None:
                raise ValueError(f"Node not found: {node_id}")
            return node

    def ensure_repo(self, project: str = "") -> str:
        del project
        return "default"

    def ensure_context_window(self, session_id: str = "", repo_id: str | None = None) -> str:
        del repo_id
        return session_id.strip() or "default"

    def resolve_window_context(self, project: str | None = None, session_id: str | None = None) -> tuple[str, str]:
        return (self.ensure_repo(project or "default"), self.ensure_context_window(session_id or "default", "default"))

    def list_context_windows(
        self,
        *,
        project: str = "",
        status: str = "",
        limit: int = 20,
    ) -> list[ContextWindow]:
        del project, status, limit
        return []

    def get_context_window(self, window_id: str) -> ContextWindow:
        return ContextWindow(
            id=window_id,
            tenant_id=self.tenant_id,
            repo_id="default",
            session_id=window_id,
            title="",
            status="active",
            node_count=0,
        )

    def get_context_window_edges(self, window_id: str) -> list[ContextWindowEdge]:
        del window_id
        return []

    def close_context_window(self, window_id: str) -> ContextWindow:
        window = self.get_context_window(window_id)
        window.status = "closed"
        window.closed_at = utc_now()
        window.updated_at = window.closed_at
        return window

    def get_repo_windows(
        self,
        repo_id: str,
        *,
        exclude: str | None = None,
        include_archived: bool = False,
    ) -> list[ContextWindow]:
        del repo_id, exclude, include_archived
        return []

    def get_window_nodes(self, window_id: str, node_types: list[NodeType] | None = None) -> list[Node]:
        del window_id, node_types
        return []

    def compute_window_embedding(self, window_id: str) -> np.ndarray | None:
        del window_id
        return None

    def get_window_embedding(self, window_id: str) -> np.ndarray | None:
        del window_id
        return None

    def extract_window_entities(self, window_id: str) -> list[dict[str, str]]:
        del window_id
        return []

    def derive_context_window_edges(self, window_id: str, repo_id: str) -> list[ContextWindowEdge]:
        del window_id, repo_id
        return []

    def get_nodes_without_window(self) -> list[Node]:
        return []

    def assign_nodes_to_window(self, node_ids: list[str], window_id: str) -> int:
        del node_ids, window_id
        return 0

    def list_repos(self) -> list[dict[str, Any]]:
        return []

    def update_window_node_count(self, window_id: str) -> int:
        del window_id
        return 0

    def mark_window_embedding_stale(self, window_id: str) -> None:
        del window_id

    def tiered_query(
        self,
        *,
        query: str,
        project: str = "",
        repo_id: str | None = None,
        max_nodes: int = 20,
        max_depth: int = 2,
        top_k_windows: int | None = None,
    ) -> SubgraphResult:
        del repo_id, top_k_windows
        result = self.query(query=query, project=project, max_nodes=max_nodes, max_depth=max_depth)
        result.retrieval_mode = "flat_fallback"
        return result

    def debug_retrieval(
        self,
        *,
        query: str,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 10,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        result = self.query(
            query=query,
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            max_nodes=max_nodes,
            max_depth=max_depth,
        )
        return {
            "query": query.strip(),
            "repo_id": "default",
            "project": project,
            "agent_id": agent_id,
            "session_id": session_id,
            "retrieval_mode": "flat_fallback",
            "embedding_preview": [],
            "windows_evaluated": 0,
            "all_windows": [],
            "selected_windows": [],
            "flat_top_nodes": [
                {
                    "node_id": node.id,
                    "label": node.label,
                    "node_type": node.node_type.value,
                    "project": node.project,
                    "session_id": node.session_id,
                    "context_window_id": node.context_window_id,
                    "similarity_score": node.similarity_score,
                    "recency_score": node.recency_score,
                    "edge_score": node.edge_score,
                    "final_score": node.final_score,
                    "updated_at": node.updated_at.isoformat(),
                }
                for node in result.nodes[:max_nodes]
            ],
            "tiered_top_nodes": [],
            "tiered_result_mode": "flat_fallback",
        }

    def aggregate(
        self,
        *,
        query: str = "",
        node_types: list[str] | None = None,
        tags: list[str] | None = None,
        max_nodes: int = 1000,
        max_depth: int = 1,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> SubgraphResult:
        query_text = query.strip()
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._session() as session:
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            total_nodes = len(node_records)
            if total_nodes == 0:
                return SubgraphResult(query=query_text, total_nodes_in_graph=0)

            target_types = {t.lower() for t in node_types} if node_types else None
            target_tags = {t.lower() for t in tags} if tags else None

            candidates: list[Node] = []
            embeddings_by_id: dict[str, np.ndarray] = {}
            for props in node_records:
                node = self._node_from_props(props)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                    continue
                if target_types and node.node_type.value.lower() not in target_types:
                    continue
                if target_tags:
                    node_tags = {t.lower() for t in node.tags}
                    if not any(tag in node_tags for tag in target_tags):
                        continue
                candidates.append(node)
                if props.get("embedding"):
                    embeddings_by_id[node.id] = np.array(props["embedding"], dtype=np.float32)

            if not candidates:
                return SubgraphResult(query=query_text, total_nodes_in_graph=total_nodes)

            if query_text:
                query_embedding = self.embedding_model.embed(query_text)
                scored_candidates = []
                for node in candidates:
                    similarity = 0.0
                    emb = embeddings_by_id.get(node.id)
                    if emb is not None:
                        similarity = max(self.embedding_model.cosine_similarity(query_embedding, emb), 0.0)
                    scored_candidates.append((similarity, node))
                scored_candidates.sort(key=lambda item: item[0], reverse=True)
                selected_nodes = [node for _, node in scored_candidates[:max_nodes]]
            else:
                candidates.sort(key=lambda node: node.updated_at.timestamp(), reverse=True)
                selected_nodes = candidates[:max_nodes]

            if max_depth > 0 and selected_nodes:
                selected_ids = [node.id for node in selected_nodes]
                graph = self._load_graph(session)
                expanded_depths = self._expand_node_depths(graph, selected_ids, max_depth)
                expanded_ids = set(expanded_depths.keys())
                missing_ids = expanded_ids - {node.id for node in selected_nodes}
                if missing_ids:
                    for props in node_records:
                        if props["id"] in missing_ids:
                            selected_nodes.append(self._node_from_props(props))

            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(session, selected_ids)
            self._increment_access_counts(session, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="aggregate",
                query=query_text,
                total_nodes_in_graph=total_nodes,
            )

    def query(
        self,
        *,
        query: str,
        max_nodes: int = 20,
        max_depth: int = 2,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        retrieval_mode: str = "hybrid",
    ) -> SubgraphResult:
        query_text = query.strip()
        if not query_text:
            raise ValueError("Query cannot be empty.")
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")
        normalized_mode = {"replay": "verbatim", "fusion": "hybrid"}.get(
            retrieval_mode.strip().lower(), retrieval_mode.strip().lower()
        )
        # Accept "hybrid_no_rerank" as alias for "hybrid" (reranking is configurable via HybridRetrievalConfig)
        if normalized_mode == "hybrid_no_rerank":
            normalized_mode = "hybrid"
        if normalized_mode not in {"graph", "verbatim", "hybrid"}:
            raise ValidationFailure(
                "retrieval_mode must be one of: graph, verbatim, hybrid, hybrid_no_rerank (benchmark modes: graph_only, verbatim_only)."
            )

        graph_result = (
            self._query_graph_only(
                query=query_text,
                max_nodes=max_nodes,
                max_depth=max_depth,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            if normalized_mode in {"graph", "hybrid"}
            else None
        )
        replay_hits = (
            self._query_replay_hits(
                query=query_text,
                max_hits=max_nodes,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            if normalized_mode in {"verbatim", "hybrid"}
            else []
        )
        if normalized_mode == "graph":
            graph_result.retrieval_mode = "graph"
            return graph_result
        if normalized_mode == "verbatim":
            return SubgraphResult(
                replay_hits=replay_hits,
                retrieval_mode="verbatim",
                query=query_text,
                total_nodes_in_graph=graph_result.total_nodes_in_graph if graph_result is not None else 0,
            )
        fusion_hits = self._build_fusion_hits(graph_result or SubgraphResult(query=query_text), replay_hits)
        return SubgraphResult(
            nodes=graph_result.nodes if graph_result is not None else [],
            edges=graph_result.edges if graph_result is not None else [],
            replay_hits=replay_hits,
            fusion_hits=fusion_hits[:max_nodes],
            retrieval_mode="hybrid",
            query=query_text,
            total_nodes_in_graph=graph_result.total_nodes_in_graph if graph_result is not None else 0,
        )

    def _query_graph_only(
        self,
        *,
        query: str,
        max_nodes: int,
        max_depth: int,
        agent_id: str,
        project: str,
        session_id: str,
    ) -> SubgraphResult:
        with self._lock, self._session() as session:
            temporal_hints = infer_temporal_hints(query)
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            total_nodes = len(node_records)
            if total_nodes == 0:
                return SubgraphResult(query=query, total_nodes_in_graph=0)

            nodes_by_id = {
                props["id"]: node
                for props in node_records
                for node in [self._node_from_props(props)]
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            }
            if not nodes_by_id:
                return SubgraphResult(query=query, total_nodes_in_graph=total_nodes)
            embeddings_by_id = {
                props["id"]: np.array(props.get("embedding") or [], dtype=np.float32)
                for props in node_records
                if props.get("embedding") and props["id"] in nodes_by_id
            }

            query_embedding = self.embedding_model.embed(query)
            similarity_by_id = {
                node_id: max(self.embedding_model.cosine_similarity(query_embedding, embedding), 0.0)
                for node_id, embedding in embeddings_by_id.items()
            }
            lexical_by_id = {
                node_id: lexical_overlap(query, node.label, node.content) for node_id, node in nodes_by_id.items()
            }

            seed_count = min(total_nodes, max(1, max_nodes // 2))
            seed_candidates = [
                (
                    node_id,
                    (0.72 * similarity_by_id.get(node_id, 0.0)) + (0.28 * lexical_by_id.get(node_id, 0.0)),
                    self._seed_temporal_order(nodes_by_id[node_id], temporal_hints),
                )
                for node_id in nodes_by_id
            ]
            if temporal_hints.recency_mode in {"latest", "oldest"}:
                ranked_seed_ids = [
                    item[0]
                    for item in sorted(
                        seed_candidates,
                        key=lambda item: (item[2], -item[1], nodes_by_id[item[0]].label.lower()),
                    )[:seed_count]
                ]
            else:
                ranked_seed_ids = [
                    item[0]
                    for item in sorted(
                        seed_candidates,
                        key=lambda item: (-item[1], item[2], nodes_by_id[item[0]].label.lower()),
                    )[:seed_count]
                ]

            graph = self._load_graph(session)
            expanded_depths = self._expand_node_depths(graph, ranked_seed_ids, max_depth)
            candidate_nodes = [nodes_by_id[node_id] for node_id in expanded_depths]
            temporal_candidates = [node for node in candidate_nodes if within_time_window(node, temporal_hints)]
            if temporal_candidates:
                candidate_nodes = temporal_candidates
            max_access = max((node.access_count for node in candidate_nodes), default=0)
            degree_by_id = dict(graph.degree(expanded_depths.keys()))
            max_degree = max(degree_by_id.values(), default=0)
            scored_nodes = self._sort_scored_nodes(
                candidate_nodes,
                temporal_hints=temporal_hints,
                similarity_by_id=similarity_by_id,
                lexical_by_id=lexical_by_id,
                degree_by_id=degree_by_id,
                max_access=max_access,
                max_degree=max_degree,
                max_depth=max_depth,
                expanded_depths=expanded_depths,
            )
            selected_nodes = scored_nodes[:max_nodes]
            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(session, selected_ids)
            self._increment_access_counts(session, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="graph",
                query=query,
                total_nodes_in_graph=total_nodes,
            )

    def _query_replay_hits(
        self,
        *,
        query: str,
        max_hits: int,
        agent_id: str,
        project: str,
        session_id: str,
    ) -> list[ReplayHit]:
        predicates = ["t.tenant_id = $tenant_id", "t.embedding IS NOT NULL"]
        params: dict[str, Any] = {"tenant_id": self.tenant_id}
        if project.strip():
            predicates.append("t.project = $project")
            params["project"] = project.strip()
        if session_id.strip():
            predicates.append("t.session_id = $session_id")
            params["session_id"] = session_id.strip()
        elif agent_id.strip():
            predicates.append("t.agent_id = $agent_id")
            params["agent_id"] = agent_id.strip()

        with self._lock, self._session() as session:
            records = list(
                session.run(
                    f"""
                    MATCH (t:MemoryTranscript)
                    WHERE {" AND ".join(predicates)}
                    RETURN t
                    """,
                    **params,
                )
            )
        if not records:
            return []

        rows = [self._transcript_from_props(record["t"]) for record in records]
        query_embedding = self.embedding_model.embed(query)
        temporal_hints = infer_temporal_hints(query)
        timestamps = np.asarray([row.observed_at.timestamp() for row in rows], dtype=np.float64)
        max_timestamp = float(np.max(timestamps))
        min_timestamp = float(np.min(timestamps))
        span = max(max_timestamp - min_timestamp, 1.0)
        hits: list[tuple[float, ReplayHit]] = []
        for row, raw_timestamp, record in zip(rows, timestamps, records, strict=True):
            if (
                (project.strip() and row.project != project.strip())
                or (session_id.strip() and row.session_id != session_id.strip())
                or (agent_id.strip() and not session_id.strip() and row.agent_id != agent_id.strip())
            ):
                continue
            embedding = np.asarray(record["t"].get("embedding") or [], dtype=np.float32)
            semantic_score = max(self.embedding_model.cosine_similarity(query_embedding, embedding), 0.0)
            lexical_score = lexical_overlap(query, row.role, row.transcript_text)
            temporal_score = 0.0
            if temporal_hints.recency_mode == "latest":
                temporal_score = float((raw_timestamp - min_timestamp) / span)
            elif temporal_hints.recency_mode == "oldest":
                temporal_score = float((max_timestamp - raw_timestamp) / span)
            role_score = 1.0 if row.role == "user" else 0.8
            score = (0.6 * semantic_score) + (0.2 * lexical_score) + (0.1 * temporal_score) + (0.1 * role_score)
            hits.append(
                (
                    score,
                    ReplayHit(
                        score=score,
                        session_id=row.session_id,
                        turn_index=row.turn_index,
                        role=row.role,
                        transcript_text=row.transcript_text,
                        transcript_snippet=row.transcript_text[:280],
                        observed_at=row.observed_at,
                    ),
                )
            )
        hits.sort(key=lambda item: (-item[0], -item[1].observed_at.timestamp(), item[1].turn_index))
        return [hit for _, hit in hits[:max_hits]]

    def _build_fusion_hits(
        self,
        graph_result: SubgraphResult,
        replay_hits: list[ReplayHit],
    ) -> list[FusionHit]:
        combined: dict[str, FusionHit] = {}
        graph_edge_map: dict[str, list[dict[str, Any]]] = {}
        graph_nodes_by_session = {node.session_id: node for node in graph_result.nodes if node.session_id}
        replay_by_session = {hit.session_id for hit in replay_hits if hit.session_id}
        for edge in graph_result.edges:
            payload = {
                "id": edge.id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "relationship": edge.relationship,
                "weight": edge.weight,
            }
            graph_edge_map.setdefault(edge.source_id, []).append(payload)
            graph_edge_map.setdefault(edge.target_id, []).append(payload)
        for index, node in enumerate(graph_result.nodes, start=1):
            key = f"graph:{node.id}"
            source_lane = "both" if node.session_id and node.session_id in replay_by_session else "graph"
            combined[key] = FusionHit(
                content=node.content,
                score=1.0 / (60 + index),
                source_lane=source_lane,
                graph_rank=index,
                fused_rank=index,
                node_id=node.id,
                node_type=node.node_type.value,
                edges=graph_edge_map.get(node.id, []),
                session_id=node.session_id or None,
            )
        for index, hit in enumerate(replay_hits, start=1):
            key = f"replay:{hit.session_id}:{hit.turn_index}:{hit.role}"
            matching_graph = graph_nodes_by_session.get(hit.session_id) if hit.session_id else None
            if matching_graph is not None:
                existing = combined.get(f"graph:{matching_graph.id}")
                if existing is not None:
                    existing.score += 1.0 / (60 + index)
                    existing.source_lane = "both"
                    existing.replay_rank = index
                    existing.session_id = hit.session_id
                    existing.transcript_snippet = hit.transcript_snippet
                    existing.turn_index = hit.turn_index
                    continue
                key = f"both:{matching_graph.id}:{hit.session_id}:{hit.turn_index}"
            existing = combined.get(key)
            contribution = 1.0 / (60 + index)
            if existing is None:
                combined[key] = FusionHit(
                    content=hit.transcript_text,
                    score=contribution,
                    source_lane="replay" if matching_graph is None else "both",
                    replay_rank=index,
                    fused_rank=index,
                    node_id=matching_graph.id if matching_graph is not None else None,
                    node_type=matching_graph.node_type.value if matching_graph is not None else None,
                    edges=graph_edge_map.get(matching_graph.id, []) if matching_graph is not None else None,
                    session_id=hit.session_id,
                    transcript_snippet=hit.transcript_snippet,
                    turn_index=hit.turn_index,
                )
                continue
            existing.score += contribution
            existing.source_lane = "both"
            existing.replay_rank = index
            existing.session_id = hit.session_id
            existing.transcript_snippet = hit.transcript_snippet
            existing.turn_index = hit.turn_index

        ordered = sorted(
            combined.values(),
            key=lambda hit: (-hit.score, hit.graph_rank or 10**6, hit.replay_rank or 10**6, hit.content.lower()),
        )
        for index, hit in enumerate(ordered, start=1):
            hit.fused_rank = index
        return ordered

    def get_related(self, *, node_id: str, max_depth: int = 2) -> SubgraphResult:
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._session() as session:
            self._require_node(session, node_id)
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            nodes_by_id = {props["id"]: self._node_from_props(props) for props in node_records}
            graph = self._load_graph(session)
            related_ids = list(self._expand_node_depths(graph, [node_id], max_depth))

            ordered_nodes: list[Node] = []
            seen: set[str] = set()
            for related_id in [node_id, *related_ids]:
                if related_id in seen:
                    continue
                seen.add(related_id)
                ordered_nodes.append(nodes_by_id[related_id])

            edges = self._fetch_edges_for_nodes(session, [node.id for node in ordered_nodes])
            self._increment_access_counts(session, [node.id for node in ordered_nodes])
            for node in ordered_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=ordered_nodes,
                edges=edges,
                query=f"related:{node_id}",
                total_nodes_in_graph=len(nodes_by_id),
            )

    def list_recent_nodes(
        self,
        limit: int = 10,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> list[Node]:
        with self._lock, self._session() as session:
            selected: list[Node] = []
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                RETURN n
                ORDER BY n.updated_at DESC, n.created_at DESC
                """,
                tenant_id=self.tenant_id,
            ):
                node = self._node_from_props(record["n"])
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                    continue
                selected.append(node)
                if len(selected) >= max(1, limit):
                    break
            return selected

    def list_context_scopes(self) -> ContextScopeResult:
        with self._lock, self._session() as session:
            nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
        return ContextScopeResult(
            agent_ids=sorted({node.agent_id for node in nodes if node.agent_id}),
            projects=sorted({node.project for node in nodes if node.project}),
            session_ids=sorted({node.session_id for node in nodes if node.session_id}),
        )

    def export_graph_html(
        self,
        *,
        output_path: str | Path | None = None,
        include_physics: bool = True,
    ) -> Path:
        try:
            from pyvis.network import Network
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyvis is not installed. Install the project dependencies again.") from exc

        with self._lock, self._session() as session:
            nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            edges = [
                Edge(
                    id=record["id"],
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at ASC
                    """,
                    tenant_id=self.tenant_id,
                )
            ]

        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-{timestamp}.html"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)

        network = Network(
            height="800px",
            width="100%",
            directed=True,
            bgcolor="#0f172a",
            font_color="#e2e8f0",
        )
        network.barnes_hut()
        if not include_physics:
            network.toggle_physics(False)

        palette = {
            NodeType.FACT: "#38bdf8",
            NodeType.ENTITY: "#34d399",
            NodeType.CONCEPT: "#fbbf24",
            NodeType.PREFERENCE: "#fb7185",
            NodeType.DECISION: "#c084fc",
            NodeType.QUESTION: "#f97316",
            NodeType.NOTE: "#94a3b8",
        }
        for node in nodes:
            title_lines = [
                f"<b>{node.label}</b>",
                f"Type: {node.node_type.value}",
                f"Created: {node.created_at.isoformat()}",
                f"Updated: {node.updated_at.isoformat()}",
                f"Access Count: {node.access_count}",
                "",
                node.content,
            ]
            if node.tags:
                title_lines.insert(4, f"Tags: {', '.join(node.tags)}")
            network.add_node(
                node.id,
                label=node.label,
                title="<br>".join(title_lines),
                color=palette[node.node_type],
                shape="dot",
                size=18 + min(node.access_count, 8) * 2,
            )
        for edge in edges:
            network.add_edge(
                edge.source_id,
                edge.target_id,
                label=edge.relationship,
                title=f"weight={edge.weight}",
                value=max(edge.weight, 0.1),
                arrows="to",
            )
        destination.write_text(network.generate_html(notebook=False), encoding="utf-8")
        return destination

    def export_window_graph_html(
        self,
        *,
        project: str = "",
        output_path: str | Path | None = None,
        include_physics: bool = True,
    ) -> Path:
        del project, include_physics
        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-window-graph-{timestamp}.html"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "<!doctype html><html><body><p>Neo4j context-window graph visualization is not implemented yet.</p></body></html>",
            encoding="utf-8",
        )
        return destination

    def export_graph_backup(self, *, output_path: str | Path | None = None) -> BackupResult:
        with self._lock, self._session() as session:
            snapshot = self._build_backup_snapshot(session)

        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-backup-{timestamp}.json"
        else:
            destination = Path(output_path).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)

        destination.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return BackupResult(
            output_path=str(destination),
            tenant_id=self.tenant_id,
            schema_version=SCHEMA_VERSION,
            node_count=len(snapshot["nodes"]),
            edge_count=len(snapshot["edges"]),
        )

    def export_abhi(
        self,
        *,
        output_path: str | Path | None = None,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        include_embeddings: bool = False,
        passphrase: str = "",
    ) -> AbhiExportResult:
        with self._lock, self._session() as session:
            snapshot = self._build_backup_snapshot(session, include_embeddings=include_embeddings)
        snapshot["ui"] = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        filtered = filter_snapshot_by_scope(snapshot, project=project, agent_id=agent_id, session_id=session_id)
        if output_path is None:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
            destination = self.export_dir / f"waggle-memory-{timestamp}.abhi"
        else:
            destination = Path(output_path).expanduser()
        return write_abhi_document(filtered, output_path=destination, passphrase=passphrase)

    def get_graph_snapshot(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._session() as session:
            snapshot = self._build_backup_snapshot(session)
        filtered = filter_snapshot_by_scope(snapshot, project=project, agent_id=agent_id, session_id=session_id)
        filtered["ui"] = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        return filtered

    def export_context_bundle(
        self,
        *,
        mode: str = "prime",
        query: str = "",
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 25,
        max_depth: int = 2,
        retrieval_mode: str = "graph",
        format: str = "both",
        output_path: str | Path | None = None,
        include_edges: bool = True,
        include_timestamps: bool = True,
        include_source_prompt: bool = False,
        audience: str = "llm",
    ) -> ContextBundleExportResult:
        normalized_mode = mode.strip().lower()
        normalized_format = format.strip().lower()
        normalized_audience = audience.strip().lower()
        normalized_retrieval_mode = {"replay": "verbatim", "fusion": "hybrid"}.get(
            retrieval_mode.strip().lower(), retrieval_mode.strip().lower()
        )
        if normalized_mode not in {"prime", "query", "graph"}:
            raise ValidationFailure("mode must be one of: prime, query, graph.")
        if normalized_format not in {"markdown", "json", "both"}:
            raise ValidationFailure("format must be one of: markdown, json, both.")
        if normalized_audience not in {"llm", "human"}:
            raise ValidationFailure("audience must be one of: llm, human.")
        if normalized_retrieval_mode not in {"graph", "verbatim", "hybrid"}:
            raise ValidationFailure("retrieval_mode must be one of: graph, verbatim, hybrid.")
        if normalized_mode == "query" and not query.strip():
            raise ValidationFailure("query is required when mode='query'.")
        if normalized_mode != "query" and normalized_retrieval_mode != "graph":
            raise ValidationFailure("retrieval_mode is only supported when mode='query'.")

        replay_hits: list[ReplayHit] = []
        if normalized_mode == "prime":
            selected = self.prime_context(project=project, agent_id=agent_id, session_id=session_id)
            selected_nodes = selected.nodes[:max_nodes]
            selected_edges = selected.edges if include_edges else []
            summary = selected.summary
        elif normalized_mode == "query":
            selected = self.query(
                query=query,
                max_nodes=max_nodes,
                max_depth=max_depth,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                retrieval_mode=normalized_retrieval_mode,
            )
            selected_nodes = selected.nodes
            selected_edges = selected.edges if include_edges else []
            replay_hits = selected.replay_hits
            summary = build_query_summary(
                query=query,
                nodes=selected_nodes,
                edges=selected_edges,
                replay_hits=replay_hits,
                retrieval_mode=normalized_retrieval_mode,
            )
        else:
            with self._lock, self._session() as session:
                selected_nodes = [
                    node
                    for record in session.run(
                        "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n ORDER BY n.updated_at DESC, n.created_at DESC",
                        tenant_id=self.tenant_id,
                    )
                    for node in [self._node_from_props(record["n"])]
                    if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
                ]
                selected_edges = (
                    [
                        Edge(
                            id=record["id"],
                            source_id=record["source_id"],
                            target_id=record["target_id"],
                            relationship=record["relationship"],
                            weight=float(record["weight"]),
                            metadata=_decode_metadata(record["metadata"]),
                            created_at=_parse_datetime(record["created_at"]),
                        )
                        for record in session.run(
                            """
                        MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                        RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                               r.relationship AS relationship, r.weight AS weight,
                               r.metadata AS metadata, r.created_at AS created_at
                        ORDER BY r.created_at ASC
                        """,
                            tenant_id=self.tenant_id,
                        )
                    ]
                    if include_edges
                    else []
                )
            if include_edges:
                selected_ids = {node.id for node in selected_nodes}
                selected_edges = [
                    edge for edge in selected_edges if edge.source_id in selected_ids and edge.target_id in selected_ids
                ]
            summary = (
                f"Full graph export for tenant '{self.tenant_id}' with {len(selected_nodes)} nodes and "
                f"{len(selected_edges)} edges."
            )

        bundle = build_context_bundle(
            tenant_id=self.tenant_id,
            project=project,
            mode=normalized_mode,
            retrieval_mode=normalized_retrieval_mode if normalized_mode == "query" else "graph",
            audience=normalized_audience,
            query=query,
            summary=summary,
            nodes=selected_nodes,
            edges=selected_edges,
            replay_hits=replay_hits,
            stats=self.get_stats(),
        )
        return export_context_bundle_files(
            bundle,
            output_path=output_path,
            export_dir=self.export_dir,
            format=normalized_format,
            include_edges=include_edges,
            include_timestamps=include_timestamps,
            include_source_prompt=include_source_prompt,
        )

    def export_markdown_vault(
        self,
        *,
        root_path: str | Path,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> MarkdownVaultExportResult:
        root = Path(root_path).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        with self._lock, self._session() as session:
            selected_nodes = [
                node
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n ORDER BY n.updated_at DESC, n.created_at DESC",
                    tenant_id=self.tenant_id,
                )
                for node in [self._node_from_props(record["n"])]
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            ]
            selected_ids = {node.id for node in selected_nodes}
            selected_edges = [
                Edge(
                    id=record["id"],
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at ASC
                    """,
                    tenant_id=self.tenant_id,
                )
                if record["source_id"] in selected_ids and record["target_id"] in selected_ids
            ]
        node_by_id = {node.id: node for node in selected_nodes}
        files_written: list[str] = []
        for node in selected_nodes:
            project_dir = slugify(node.project or project or "default")
            node_type_dir = slugify(node.node_type.value)
            destination = root / project_dir / node_type_dir / vault_filename(node)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(render_node_document(node, selected_edges, node_by_id), encoding="utf-8")
            files_written.append(str(destination.relative_to(root)))
        return MarkdownVaultExportResult(
            root_path=str(root),
            tenant_id=self.tenant_id,
            project=project,
            node_count=len(selected_nodes),
            edge_count=len(selected_edges),
            files_written=files_written,
        )

    def import_markdown_vault(
        self,
        *,
        root_path: str | Path,
    ) -> MarkdownVaultImportResult:
        documents = iter_vault_documents(root_path)
        result = MarkdownVaultImportResult(root_path=str(Path(root_path).expanduser()), tenant_id=self.tenant_id)
        if not documents:
            return result

        nodes_by_id: dict[str, Node] = {}
        label_index: dict[str, list[Node]] = {}
        with self._lock, self._session() as session:
            for record in session.run(
                "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                tenant_id=self.tenant_id,
            ):
                node = self._node_from_props(record["n"])
                nodes_by_id[node.id] = node
                label_index.setdefault(node.label.strip().lower(), []).append(node)

        imported_id_map: dict[str, str] = {}
        for document in documents:
            node_id = str(document.frontmatter.get("node_id", "")).strip()
            raw_type = str(document.frontmatter.get("node_type", "note") or "note")
            try:
                node_type = NodeType(raw_type)
            except ValueError:
                node_type = NodeType.NOTE
            if node_id in nodes_by_id:
                updated = self.update_node(
                    node_id=node_id,
                    label=document.label,
                    content=document.content.strip() or document.label,
                    tags=[str(tag) for tag in document.frontmatter.get("tags", []) or []],
                )
                nodes_by_id[node_id] = updated
                if node_id:
                    imported_id_map[node_id] = updated.id
                    nodes_by_id[node_id] = updated
                label_index.setdefault(updated.label.strip().lower(), []).append(updated)
                result.nodes_updated += 1
            else:
                created = self.add_node(
                    label=document.label,
                    content=document.content.strip() or document.label,
                    node_type=node_type,
                    tags=[str(tag) for tag in document.frontmatter.get("tags", []) or []],
                    agent_id=str(document.frontmatter.get("agent_id", "") or ""),
                    project=str(document.frontmatter.get("project", "") or ""),
                    session_id=str(document.frontmatter.get("session_id", "") or ""),
                    evidence_records=evidence_from_lines(document.evidence_lines),
                    valid_from=self._parse_optional_datetime(document.frontmatter.get("valid_from")),
                    valid_to=self._parse_optional_datetime(document.frontmatter.get("valid_to")),
                ).node
                nodes_by_id[created.id] = created
                if node_id:
                    imported_id_map[node_id] = created.id
                    nodes_by_id[node_id] = created
                label_index.setdefault(created.label.strip().lower(), []).append(created)
                result.nodes_created += 1

        for document in documents:
            source_lookup_id = str(document.frontmatter.get("node_id", "")).strip()
            source_node = nodes_by_id.get(imported_id_map.get(source_lookup_id, source_lookup_id))
            if source_node is None:
                result.conflicts.append(f"Missing source node for {document.path.name}.")
                continue
            for relation in document.relations:
                target_lookup_id = imported_id_map.get(relation.target_node_id, relation.target_node_id)
                target = nodes_by_id.get(target_lookup_id) if target_lookup_id else None
                if target is None and relation.target_label:
                    candidates = label_index.get(relation.target_label.strip().lower(), [])
                    for candidate in candidates:
                        if _scope_matches(
                            candidate,
                            agent_id=source_node.agent_id,
                            project=source_node.project,
                            session_id=source_node.session_id,
                        ):
                            target = candidate
                            break
                if target is None and relation.target_label:
                    target = self.add_node(
                        label=relation.target_label,
                        content=f"Stub node imported from vault for {relation.target_label}.",
                        node_type=NodeType.NOTE,
                        tags=["stub", "vault-import"],
                        project=source_node.project,
                        agent_id=source_node.agent_id,
                        session_id=source_node.session_id,
                    ).node
                    nodes_by_id[target.id] = target
                    label_index.setdefault(target.label.strip().lower(), []).append(target)
                    result.stub_nodes_created += 1
                if target is None:
                    result.conflicts.append(
                        f"Could not resolve relation target '{relation.target_label}' in {document.path.name}."
                    )
                    continue
                if relation.deleted:
                    if self._delete_edge_record(
                        source_id=source_node.id,
                        target_id=target.id,
                        relationship=relation.relationship,
                    ):
                        result.edges_deleted += 1
                    continue
                with self._lock, self._session() as session:
                    existing_edge = self._find_existing_edge(
                        session,
                        source_id=source_node.id,
                        target_id=target.id,
                        relationship=relation.relationship,
                    )
                if existing_edge is None:
                    self.add_edge(source_id=source_node.id, target_id=target.id, relationship=relation.relationship)
                    result.edges_created += 1
        return result

    def import_graph_backup(self, *, input_path: str | Path) -> ImportResult:
        source = Path(input_path).expanduser()
        snapshot = json.loads(source.read_text(encoding="utf-8"))

        with self._lock, self._session() as session:
            snapshot_tenant = str(snapshot.get("tenant_id") or self.tenant_id)
            result = ImportResult(
                input_path=str(source),
                tenant_id=self.tenant_id,
                schema_version=int(snapshot.get("schema_version", 1)),
            )
            for raw_node in snapshot.get("nodes", []):
                raw_node = {**raw_node, "tenant_id": raw_node.get("tenant_id") or snapshot_tenant}
                if raw_node["tenant_id"] != self.tenant_id:
                    raw_node["tenant_id"] = self.tenant_id
                if self._fetch_node(session, raw_node["id"]) is None:
                    self._insert_snapshot_node(session, raw_node)
                    result.nodes_created += 1
                else:
                    self._update_snapshot_node(session, raw_node)
                    result.nodes_updated += 1

            for raw_edge in snapshot.get("edges", []):
                raw_edge = {**raw_edge, "tenant_id": raw_edge.get("tenant_id") or snapshot_tenant}
                if raw_edge["tenant_id"] != self.tenant_id:
                    raw_edge["tenant_id"] = self.tenant_id
                if self._fetch_edge_by_id(session, raw_edge["id"]) is None:
                    self._insert_snapshot_edge(session, raw_edge)
                    result.edges_created += 1
                else:
                    self._update_snapshot_edge(session, raw_edge)
                    result.edges_updated += 1
        self.save_ui_state(
            positions=snapshot.get("ui", {}).get("positions", {}),
            zoom=snapshot.get("ui", {}).get("zoom", 1.0),
            viewport=snapshot.get("ui", {}).get("viewport", {"center_x": 0, "center_y": 0}),
            groups=snapshot.get("ui", {}).get("groups", []),
            collapsed_groups=snapshot.get("ui", {}).get("collapsed_groups", []),
            selected_nodes=snapshot.get("ui", {}).get("selected_nodes", []),
        )
        return result

    def validate_abhi(self, *, input_path: str | Path, passphrase: str = "") -> AbhiValidationResult:
        document = load_abhi_document(input_path, passphrase=passphrase)
        return validate_abhi_document(document, input_path=input_path)

    def inspect_abhi(self, *, input_path: str | Path, passphrase: str = "") -> AbhiInspectResult:
        document = load_abhi_document(input_path, passphrase=passphrase)
        return inspect_abhi_document(document, input_path=input_path)

    def diff_abhi(self, *, input_path_a: str | Path, input_path_b: str | Path) -> AbhiDiffResult:
        return diff_abhi_files(input_path_a=input_path_a, input_path_b=input_path_b)

    def query_abhi(
        self, *, input_path: str | Path, query_id: str = "", query_text: str = "", passphrase: str = ""
    ) -> AbhiQueryResult:
        return query_abhi_file(input_path=input_path, query_id=query_id, query_text=query_text, passphrase=passphrase)

    def load_abhi_chunks(
        self,
        *,
        input_path: str | Path,
        chunk_ids: list[str] | None = None,
        query_id: str = "",
        query_text: str = "",
        passphrase: str = "",
    ) -> AbhiChunkLoadResult:
        return load_abhi_chunk_file(
            input_path=input_path,
            chunk_ids=chunk_ids or [],
            query_id=query_id,
            query_text=query_text,
            passphrase=passphrase,
        )

    def merge_abhi(
        self,
        *,
        base_input_path: str | Path,
        left_input_path: str | Path,
        right_input_path: str | Path,
        output_path: str | Path,
        merge_strategy: str = "prefer_right",
    ) -> AbhiMergeResult:
        return merge_abhi_files(
            base_input_path=base_input_path,
            left_input_path=left_input_path,
            right_input_path=right_input_path,
            output_path=output_path,
            merge_strategy=merge_strategy,
        )

    def import_abhi(self, *, input_path: str | Path, passphrase: str = "") -> AbhiImportResult:
        source = Path(input_path).expanduser()
        document = load_abhi_document(source, passphrase=passphrase)
        validation = validate_abhi_document(document, input_path=source)
        if not validation.valid:
            raise ValidationFailure("Invalid .abhi file: " + "; ".join(validation.errors))
        executed_actions = dispatch_abhi_event(document, event_name="on_import", persist=False, input_path=source)
        snapshot = abhi_to_snapshot(document, fallback_tenant_id=self.tenant_id)

        with self._lock, self._session() as session:
            snapshot_tenant = str(snapshot.get("tenant_id") or self.tenant_id)
            result = AbhiImportResult(
                input_path=str(source),
                tenant_id=self.tenant_id,
                schema_version=int(snapshot.get("schema_version", 1)),
                abhi_spec_version=validation.abhi_spec_version or ABHI_SPEC_VERSION,
                hash_verified=True,
                embedding_count=validation.embedding_count,
                encrypted=bool(passphrase),
                encryption_algorithm=ABHI_ENCRYPTION_ALGORITHM if passphrase else "",
                executed_actions=executed_actions,
            )
            for raw_node in snapshot.get("nodes", []):
                raw_node = {**raw_node, "tenant_id": raw_node.get("tenant_id") or snapshot_tenant}
                if raw_node["tenant_id"] != self.tenant_id:
                    raw_node["tenant_id"] = self.tenant_id
                if self._fetch_node(session, raw_node["id"]) is None:
                    self._insert_snapshot_node(session, raw_node)
                    result.nodes_created += 1
                else:
                    self._update_snapshot_node(session, raw_node)
                    result.nodes_updated += 1

            for raw_edge in snapshot.get("edges", []):
                raw_edge = {**raw_edge, "tenant_id": raw_edge.get("tenant_id") or snapshot_tenant}
                if raw_edge["tenant_id"] != self.tenant_id:
                    raw_edge["tenant_id"] = self.tenant_id
                if self._fetch_edge_by_id(session, raw_edge["id"]) is None:
                    self._insert_snapshot_edge(session, raw_edge)
                    result.edges_created += 1
                else:
                    self._update_snapshot_edge(session, raw_edge)
                    result.edges_updated += 1
        self.save_ui_state(
            positions=snapshot.get("ui", {}).get("positions", {}),
            zoom=snapshot.get("ui", {}).get("zoom", 1.0),
            viewport=snapshot.get("ui", {}).get("viewport", {"center_x": 0, "center_y": 0}),
            groups=snapshot.get("ui", {}).get("groups", []),
            collapsed_groups=snapshot.get("ui", {}).get("collapsed_groups", []),
            selected_nodes=snapshot.get("ui", {}).get("selected_nodes", []),
        )
        return result

    def decompose_and_store(self, *, content: str, context: str = "") -> SubgraphResult:
        trimmed_content = content.strip()
        if not trimmed_content:
            raise ValueError("Content cannot be empty.")
        created_nodes: list[Node] = []
        created_ids: set[str] = set()
        context_node: Node | None = None
        if context.strip():
            context_result = self.add_node(
                label=infer_label(context),
                content=context.strip(),
                node_type=NodeType.CONCEPT,
                tags=["decomposition-context"],
                source_prompt=trimmed_content,
            )
            context_node = context_result.node
            created_nodes.append(context_node)
            created_ids.add(context_node.id)

        item_nodes: list[Node] = []
        for item in split_atomic_items(trimmed_content):
            store_result = self.add_node(
                label=infer_label(item),
                content=item,
                node_type=infer_node_type(item),
                tags=["decomposed"],
                source_prompt=context.strip() or trimmed_content,
            )
            node = store_result.node
            item_nodes.append(node)
            if node.id not in created_ids:
                created_nodes.append(node)
                created_ids.add(node.id)
            if context_node is not None:
                self.add_edge(
                    source_id=node.id,
                    target_id=context_node.id,
                    relationship=RelationType.PART_OF,
                    metadata={"origin": "decomposition"},
                )

        for index, node in enumerate(item_nodes):
            if index == 0:
                continue
            previous = item_nodes[index - 1]
            shared_tokens = tokenize_text(previous.content) & tokenize_text(node.content)
            if shared_tokens or previous.node_type == node.node_type:
                self.add_edge(
                    source_id=previous.id,
                    target_id=node.id,
                    relationship=infer_relationship(previous, node, shared_tokens=shared_tokens),
                    metadata={"origin": "decomposition"},
                )

        node_ids = [node.id for node in created_nodes]
        with self._lock, self._session() as session:
            edges = self._fetch_edges_for_nodes(session, node_ids)
            total_nodes = session.run(
                "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN count(n) AS count",
                tenant_id=self.tenant_id,
            ).single()["count"]
        return SubgraphResult(
            nodes=created_nodes,
            edges=edges,
            query=f"decomposition:{context.strip() or infer_label(trimmed_content)}",
            total_nodes_in_graph=int(total_nodes),
        )

    def get_node_history(self, *, node_id: str, max_depth: int = 2) -> NodeHistoryResult:
        node = self.get_node(node_id)
        related = self.get_related(node_id=node_id, max_depth=max_depth)
        related_nodes = [item for item in related.nodes if item.id != node_id]
        return NodeHistoryResult(node=node, related_nodes=related_nodes, edges=related.edges)

    def timeline(
        self,
        *,
        node_id: str = "",
        query: str = "",
        limit: int = 25,
        max_depth: int = 2,
        include_evidence: bool = True,
    ) -> TimelineResult:
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")
        if node_id.strip() and query.strip():
            raise ValueError("Provide either node_id or query, not both.")

        if node_id.strip():
            related = self.get_related(node_id=node_id, max_depth=max_depth)
            nodes = related.nodes
            edges = related.edges
            scope = f"node:{node_id.strip()}"
        elif query.strip():
            subgraph = self.query(query=query, max_nodes=max(limit, 10), max_depth=max_depth)
            nodes = subgraph.nodes
            edges = subgraph.edges
            scope = f"query:{query.strip()}"
        else:
            with self._lock, self._session() as session:
                nodes = self.list_recent_nodes(limit=max(limit, 10))
                edges = self._fetch_edges_for_nodes(session, [node.id for node in nodes])
            scope = "tenant"

        items = self._build_timeline_items(
            nodes=nodes,
            edges=edges,
            include_evidence=include_evidence,
            limit=limit,
        )
        return TimelineResult(scope=scope, items=items)

    def graph_diff(self, *, since: str = "24h") -> GraphDiffResult:
        cutoff = parse_since_value(since).isoformat()
        with self._lock, self._session() as session:
            added_nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id})
                    WHERE n.created_at >= $cutoff
                    RETURN n
                    ORDER BY n.created_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    cutoff=cutoff,
                )
            ]
            updated_nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id})
                    WHERE n.updated_at >= $cutoff AND n.created_at < $cutoff
                    RETURN n
                    ORDER BY n.updated_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    cutoff=cutoff,
                )
            ]
            created_edges = [
                Edge(
                    id=record["id"],
                    tenant_id=self.tenant_id,
                    source_id=record["source_id"],
                    target_id=record["target_id"],
                    relationship=record["relationship"],
                    weight=float(record["weight"]),
                    metadata=_decode_metadata(record["metadata"]),
                    created_at=_parse_datetime(record["created_at"]),
                )
                for record in session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                    WHERE r.created_at >= $cutoff
                    RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                           r.relationship AS relationship, r.weight AS weight,
                           r.metadata AS metadata, r.created_at AS created_at
                    ORDER BY r.created_at DESC
                    """,
                    tenant_id=self.tenant_id,
                    cutoff=cutoff,
                )
            ]
        return GraphDiffResult(
            since=since,
            added_nodes=added_nodes,
            updated_nodes=updated_nodes,
            created_edges=created_edges,
            contradiction_edges=[edge for edge in created_edges if edge.relationship == RelationType.CONTRADICTS],
        )

    def prime_context(self, *, project: str = "", agent_id: str = "", session_id: str = "") -> PrimeContextResult:
        with self._lock, self._session() as session:
            total_nodes = int(
                session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN count(n) AS count",
                    tenant_id=self.tenant_id,
                ).single()["count"]
            )
            if total_nodes == 0:
                return PrimeContextResult(project=project, summary="No stored memory is available yet.")

            selected_ids: list[str] = []
            selected_ids.extend(
                self._most_connected_node_ids(
                    session,
                    limit=5,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                )
            )
            selected_ids.extend(
                node.id
                for node in self.list_recent_nodes(
                    limit=5,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                )
            )
            if project.strip():
                selected_ids.extend(
                    self._find_project_node_ids(
                        session,
                        project=project,
                        agent_id=agent_id,
                        session_id=session_id,
                        limit=8,
                    )
                )
            unique_ids = list(dict.fromkeys(selected_ids))
            nodes = [
                node
                for node in self._fetch_nodes_by_ids(session, unique_ids)
                if _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id)
            ]
            edges = self._fetch_edges_for_nodes(session, [node.id for node in nodes])

        summary = (
            f"Prime context for '{project}' with {len(nodes)} nodes selected from {total_nodes} total nodes."
            if project.strip()
            else f"Prime context with {len(nodes)} nodes selected from {total_nodes} total nodes."
        )
        return PrimeContextResult(
            project=project,
            summary=summary,
            nodes=nodes,
            edges=edges,
            total_nodes_in_graph=total_nodes,
        )

    def get_topics(self) -> TopicResult:
        with self._lock, self._session() as session:
            nodes = [
                self._node_from_props(record["n"])
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            if not nodes:
                return TopicResult(clusters=[], total_clusters=0)
            graph = self._load_graph(session).to_undirected()
            partition = self._build_topic_partition(graph, nodes)

        nodes_by_id = {node.id: node for node in nodes}
        clusters_by_id: dict[int, list[Node]] = {}
        for node_id, cluster_id in partition.items():
            clusters_by_id.setdefault(int(cluster_id), []).append(nodes_by_id[node_id])

        clusters: list[TopicCluster] = []
        for cluster_id, cluster_nodes in sorted(
            clusters_by_id.items(),
            key=lambda item: (-len(item[1]), item[0]),
        ):
            label, top_tags = summarize_topic(cluster_nodes)
            ordered_nodes = sorted(
                cluster_nodes,
                key=lambda node: (-node.access_count, -node.updated_at.timestamp(), node.label.lower()),
            )
            clusters.append(
                TopicCluster(
                    cluster_id=cluster_id,
                    label=label,
                    node_count=len(cluster_nodes),
                    top_tags=top_tags,
                    nodes=ordered_nodes,
                )
            )
        return TopicResult(clusters=clusters, total_clusters=len(clusters))

    def _build_timeline_items(
        self,
        *,
        nodes: list[Node],
        edges: list[Edge],
        include_evidence: bool,
        limit: int,
    ) -> list[ContextTimelineItem]:
        items: list[ContextTimelineItem] = []
        for node in nodes:
            items.append(
                ContextTimelineItem(
                    kind="node_created",
                    timestamp=node.created_at,
                    label=node.label,
                    summary=node.content,
                    node_id=node.id,
                )
            )
            if node.updated_at != node.created_at:
                items.append(
                    ContextTimelineItem(
                        kind="node_updated",
                        timestamp=node.updated_at,
                        label=node.label,
                        summary=node.content,
                        node_id=node.id,
                    )
                )
            if include_evidence:
                for record in node.evidence_records:
                    items.append(
                        ContextTimelineItem(
                            kind="evidence",
                            timestamp=record.observed_at,
                            label=node.label,
                            summary=f"{record.source_role or 'unknown'} turn {record.turn_index}: {record.source_text or node.content}",
                            node_id=node.id,
                        )
                    )
        node_by_id = {node.id: node for node in nodes}
        for edge in edges:
            source_label = node_by_id.get(edge.source_id).label if edge.source_id in node_by_id else edge.source_id[:8]
            target_label = node_by_id.get(edge.target_id).label if edge.target_id in node_by_id else edge.target_id[:8]
            items.append(
                ContextTimelineItem(
                    kind=f"edge_{edge.relationship}",
                    timestamp=edge.created_at,
                    label=f"{source_label} -> {target_label}",
                    summary=edge.relationship,
                    edge_id=edge.id,
                )
            )
        return sorted(
            items,
            key=lambda item: (item.timestamp, item.kind, item.label),
            reverse=True,
        )[:limit]

    def _temporal_sort_value(self, node: Node, hints: Any) -> float:
        if hints.recency_mode == "latest":
            return -node.updated_at.timestamp()
        if hints.recency_mode == "oldest":
            return node.created_at.timestamp()
        return -node.updated_at.timestamp()

    def _seed_temporal_order(self, node: Node, hints: Any) -> float:
        if hints.recency_mode == "latest":
            return -node.updated_at.timestamp()
        if hints.recency_mode == "oldest":
            return node.created_at.timestamp()
        return 0.0

    def _sort_scored_nodes(
        self,
        candidate_nodes: list[Node],
        *,
        temporal_hints: Any,
        similarity_by_id: dict[str, float],
        lexical_by_id: dict[str, float],
        degree_by_id: dict[str, int],
        max_access: int,
        max_degree: int,
        max_depth: int,
        expanded_depths: dict[str, int],
    ) -> list[Node]:
        def combined_score(node: Node) -> float:
            return score_node(
                node=node,
                semantic_similarity=similarity_by_id.get(node.id, 0.0),
                lexical_score=lexical_by_id.get(node.id, 0.0),
                max_access=max_access,
                degree_score=(degree_by_id.get(node.id, 0) / max_degree if max_degree > 0 else 0.0),
                depth=expanded_depths.get(node.id, max_depth + 1),
            ) + temporal_score_adjustment(node, temporal_hints)

        if temporal_hints.recency_mode == "latest":
            return sorted(
                candidate_nodes,
                key=lambda node: (-node.updated_at.timestamp(), -combined_score(node), node.label.lower()),
            )
        if temporal_hints.recency_mode == "oldest":
            return sorted(
                candidate_nodes,
                key=lambda node: (node.created_at.timestamp(), -combined_score(node), node.label.lower()),
            )
        return sorted(
            candidate_nodes,
            key=lambda node: (-combined_score(node), -node.updated_at.timestamp(), node.label.lower()),
        )

    def _expand_node_depths(self, graph: nx.DiGraph, seed_ids: list[str], max_depth: int) -> dict[str, int]:
        ordered: dict[str, int] = {}
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque((seed_id, 0) for seed_id in seed_ids)

        while queue:
            node_id, depth = queue.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            ordered[node_id] = depth
            if depth >= max_depth:
                continue
            neighbors = list(graph.predecessors(node_id)) + list(graph.successors(node_id))
            for neighbor in neighbors:
                if neighbor not in seen:
                    queue.append((neighbor, depth + 1))
        return ordered

    def _fetch_edges_for_nodes(self, session: Any, node_ids: list[str]) -> list[Edge]:
        if not node_ids:
            return []
        return [
            Edge(
                id=record["id"],
                tenant_id=self.tenant_id,
                source_id=record["source_id"],
                target_id=record["target_id"],
                relationship=record["relationship"],
                weight=float(record["weight"]),
                metadata=_decode_metadata(record["metadata"]),
                created_at=_parse_datetime(record["created_at"]),
            )
            for record in session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                WHERE source.id IN $node_ids AND target.id IN $node_ids
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                ORDER BY r.created_at ASC
                """,
                tenant_id=self.tenant_id,
                node_ids=node_ids,
            )
        ]

    def _increment_access_counts(self, session: Any, node_ids: list[str]) -> None:
        if not node_ids:
            return
        session.run(
            """
            UNWIND $node_ids AS node_id
            MATCH (n:MemoryNode {tenant_id: $tenant_id, id: node_id})
            SET n.access_count = coalesce(n.access_count, 0) + 1
            """,
            tenant_id=self.tenant_id,
            node_ids=node_ids,
        ).consume()

    def _most_connected_node_ids(
        self,
        session: Any,
        *,
        limit: int,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> list[str]:
        selected: list[str] = []
        for record in session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $tenant_id})
            OPTIONAL MATCH (n)-[r:MEMORY_EDGE {tenant_id: $tenant_id}]-()
            WITH n, count(r) AS connection_count
            RETURN n, connection_count
            ORDER BY connection_count DESC, n.updated_at DESC
            """,
            tenant_id=self.tenant_id,
        ):
            node = self._node_from_props(record["n"])
            if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                continue
            selected.append(node.id)
            if len(selected) >= limit:
                break
        return selected

    def _find_project_node_ids(
        self,
        session: Any,
        *,
        project: str,
        agent_id: str = "",
        session_id: str = "",
        limit: int,
    ) -> list[str]:
        project_lower = project.strip().lower()
        scored: list[tuple[str, float, float]] = []
        for record in session.run(
            "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
            tenant_id=self.tenant_id,
        ):
            node = self._node_from_props(record["n"])
            if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                continue
            tag_match = 1.0 if any(project_lower == tag.lower() for tag in node.tags) else 0.0
            lexical = lexical_overlap(project, node.label, node.content)
            score = max(tag_match, lexical)
            if score <= 0.0:
                continue
            scored.append((node.id, score, node.updated_at.timestamp()))
        scored.sort(key=lambda item: (-item[1], -item[2]))
        return [node_id for node_id, _, _ in scored[:limit]]

    def _build_topic_partition(self, graph: nx.Graph, nodes: list[Node]) -> dict[str, int]:
        if graph.number_of_edges() == 0:
            return {node.id: index for index, node in enumerate(nodes)}
        try:
            import community  # type: ignore[import-not-found]

            return community.best_partition(graph)
        except ImportError:  # pragma: no cover
            communities = nx.algorithms.community.greedy_modularity_communities(graph)
            partition: dict[str, int] = {}
            for cluster_id, members in enumerate(communities):
                for member in members:
                    partition[str(member)] = cluster_id
            return partition

    def _fetch_edge_by_id(self, session: Any, edge_id: str) -> dict[str, Any] | None:
        return session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(target:MemoryNode {tenant_id: $tenant_id})
            RETURN r.id AS id
            """,
            tenant_id=self.tenant_id,
            id=edge_id,
        ).single()

    def _build_backup_snapshot(self, session: Any, *, include_embeddings: bool = False) -> dict[str, Any]:
        nodes = [
            {
                "id": props["id"],
                "tenant_id": props.get("tenant_id") or self.tenant_id,
                "agent_id": props.get("agent_id") or "",
                "project": props.get("project") or "",
                "session_id": props.get("session_id") or "",
                "context_window_id": props.get("context_window_id"),
                "label": props["label"],
                "content": props["content"],
                "node_type": props["node_type"],
                "tags": list(props.get("tags") or []),
                "source_prompt": props.get("source_prompt") or "",
                "metadata": _decode_metadata(props.get("metadata")),
                "evidence_records": [
                    record.model_dump(mode="json") for record in _decode_evidence_records(props.get("evidence_records"))
                ],
                "valid_from": props.get("valid_from"),
                "valid_to": props.get("valid_to"),
                "created_at": props["created_at"],
                "updated_at": props["updated_at"],
                "access_count": int(props.get("access_count") or 0),
                **(
                    {
                        "embedding": base64.b64encode(
                            np.array(props.get("embedding") or [], dtype=np.float32).astype(np.float32).tobytes()
                        ).decode("ascii")
                    }
                    if include_embeddings and props.get("embedding")
                    else {}
                ),
            }
            for props in (
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n ORDER BY n.created_at ASC",
                    tenant_id=self.tenant_id,
                )
            )
        ]
        edges = [
            {
                "id": record["id"],
                "tenant_id": self.tenant_id,
                "source_id": record["source_id"],
                "target_id": record["target_id"],
                "relationship": record["relationship"],
                "weight": float(record["weight"]),
                "metadata": _decode_metadata(record["metadata"]),
                "created_at": record["created_at"],
            }
            for record in session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                ORDER BY r.created_at ASC
                """,
                tenant_id=self.tenant_id,
            )
        ]
        snapshot = {"schema_version": SCHEMA_VERSION, "tenant_id": self.tenant_id, "nodes": nodes, "edges": edges}
        if include_embeddings:
            snapshot["embeddings"] = {node["id"]: node["embedding"] for node in nodes if node.get("embedding")}
            for node in nodes:
                node.pop("embedding", None)
        return snapshot
