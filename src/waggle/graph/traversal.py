# ruff: noqa: F401
from __future__ import annotations

import contextlib
import hashlib
import heapq
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    validate_abhi_signature,
    write_abhi_document,
)
from waggle.auth import api_key_prefix, generate_api_key, hash_api_key, verify_api_key
from waggle.connection_pool import DEFAULT_POOL_SIZE, SQLiteConnectionPool
from waggle.context_bundle import build_context_bundle, build_query_summary, export_context_bundle_files
from waggle.embeddings import EmbeddingModel
from waggle.errors import AuthenticationError, ValidationFailure
from waggle.evidence import merge_evidence_records, merge_validity_windows
from waggle.intelligence import (
    canonical_concept_overlap,
    compatible_node_types,
    contains_conflicting_months,
    contains_conflicting_numbers,
    content_token_jaccard,
    describes_rejected_or_limited_option,
    detect_conflict_reason,
    extract_choice_entity,
    infer_label,
    infer_node_type,
    infer_relationship,
    infer_temporal_hints,
    is_acronym_match,
    label_similarity,
    lexical_overlap,
    normalize_text,
    paraphrase_dedup_score,
    parse_since_value,
    split_atomic_items,
    summarize_topic,
    temporal_score_adjustment,
    tokenize_text,
    type_aware_dedup_threshold,
    within_time_window,
)
from waggle.intelligence import (
    extract_conversation_candidates as extract_conversation_candidates,
)
from waggle.locks import ProcessLock
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
    ApiKeyCreateResult,
    ApiKeyRecord,
    AuditEventRecord,
    BackupResult,
    CanonicalizeResult,
    ClearScopeResult,
    ConflictEntry,
    ConflictListResult,
    ConflictRecord,
    ConnectedNodeStat,
    ContextBundleExportResult,
    ContextScopeResult,
    ContextTimelineItem,
    ContextWindow,
    ContextWindowEdge,
    DedupCandidatePair,
    DedupCandidatesResult,
    Edge,
    EvidenceRecord,
    FusionHit,
    GraphDiffResult,
    GraphStats,
    HybridHit,
    ImportResult,
    MarkdownVaultExportResult,
    MarkdownVaultImportResult,
    Node,
    NodeHistoryResult,
    NodeStoreResult,
    NodeType,
    PrimeContextResult,
    RecentNodeStat,
    RelationType,
    ReplayHit,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
    ScoredNodeView,
    SubgraphResult,
    TenantRecord,
    TimelineResult,
    TopicCluster,
    TopicResult,
    normalize_relationship,
    utc_now,
)
from waggle.retrieval.hybrid import HybridRetrievalConfig, HybridRetriever

from .base import (
    MUST_PAIR_RELATIONS,
    NEGATION_NODE_TERMS,
    NEGATION_QUERY_TERMS,
    NEGATION_SCORE_BOOST,
    QUERY_ALIAS_TERMS,
    RELATION_SCORE_BOOST,
    RELATION_WEIGHTS,
    TEMPORAL_TOPIC_MARGIN,
    TOPIC_RELEVANCE_THRESHOLD,
    TOPIC_SEMANTIC_ONLY_THRESHOLD,
    ExpansionMeta,
    MemoryGraphBase,
    _decode_metadata,
    _encode_metadata,
    _filter_valid_nodes,
    _normalized_content_hash,
    _parse_datetime,
    _retrieval_session_scope,
    _scope_matches,
    _valid_to_enforcement_enabled,
    recency_weight,
    score_node,
)
from .transcript import TranscriptMixin


class TraversalMixin(MemoryGraphBase):
    """Mixin class for MemoryGraph handling search, BM25, semantic retrieval, and timeline query logic."""

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
            with self._lock, self._pool.checkout() as connection:
                nodes = self.list_recent_nodes(limit=max(limit, 10))
                edges = self._fetch_edges_for_nodes(connection, [node.id for node in nodes])
            scope = "tenant"

        items = self._build_timeline_items(
            nodes=nodes,
            edges=edges,
            include_evidence=include_evidence,
            limit=limit,
        )
        return TimelineResult(scope=scope, items=items)

    def query(
        self,
        *,
        query: str,
        max_nodes: int = 20,
        max_depth: int = 2,
        expand_depth: int = 0,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        retrieval_mode: str = "graph",
        include_invalidated: bool = False,
        as_of: datetime | None = None,
    ) -> SubgraphResult:
        query_text = query.strip()
        if not query_text:
            raise ValueError("Query cannot be empty.")
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")
        if expand_depth < 0:
            raise ValueError("expand_depth cannot be negative.")
        normalized_mode = retrieval_mode.strip().lower()
        normalized_mode = {"replay": "verbatim", "fusion": "hybrid"}.get(normalized_mode, normalized_mode)
        # Accept "hybrid_no_rerank" as alias for "hybrid" (reranking is configurable via HybridRetrievalConfig)
        if normalized_mode == "hybrid_no_rerank":
            normalized_mode = "hybrid"
        if normalized_mode not in {"graph", "verbatim", "hybrid"}:
            raise ValueError(
                "retrieval_mode must be one of: graph, verbatim, hybrid, hybrid_no_rerank (benchmark modes: graph_only, verbatim_only)."
            )

        if normalized_mode in {"verbatim", "hybrid"}:
            hybrid = self.hybrid_retriever()
            debug = hybrid.retrieve_debug(
                query=query_text,
                project=project,
                agent_id=agent_id,
                session_id=session_id,
                top_k=max_nodes,
                mode=normalized_mode,
            )
            result = self._subgraph_from_hybrid_hits(
                query=query_text,
                retrieval_mode=normalized_mode,
                hybrid_hits=debug["hits"],
            )
            result.nodes = _filter_valid_nodes(
                result.nodes,
                include_invalidated=include_invalidated,
                as_of=as_of,
            )
            return result

        graph_result = (
            self.tiered_query(
                query=query_text,
                project=project,
                max_nodes=max_nodes,
                max_depth=max_depth,
                top_k_windows=self.tiered_retrieval_top_k_windows,
                agent_id=agent_id,
                session_id=session_id,
                include_invalidated=include_invalidated,
                as_of=as_of,
            )
            if self.tiered_retrieval and project.strip()
            else self._query_graph_only(
                query=query_text,
                max_nodes=max_nodes,
                max_depth=max_depth,
                expand_depth=expand_depth,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                include_invalidated=include_invalidated,
                as_of=as_of,
            )
            if normalized_mode in {"graph", "fusion"}
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
            if graph_result.retrieval_mode not in {"tiered", "flat_fallback"}:
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

    def _subgraph_from_hybrid_hits(
        self,
        *,
        query: str,
        retrieval_mode: str,
        hybrid_hits: list[HybridHit],
    ) -> SubgraphResult:
        node_ids = sorted({node_id for hit in hybrid_hits for node_id in hit.node_ids})
        with self._lock, self._pool.checkout() as connection:
            nodes = self._fetch_nodes_by_ids(connection, node_ids)
            edges = self._fetch_edges_for_nodes(connection, node_ids) if node_ids else []
            total_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchone()[0]
            )
        replay_hits = [
            ReplayHit(
                score=hit.score,
                session_id="",
                turn_index=0,
                turn_pair_id=hit.turn_pair_id,
                role="",
                transcript_text=hit.content,
                transcript_snippet=hit.content[:280],
                observed_at=hit.observed_at or utc_now(),
            )
            for hit in hybrid_hits
            if hit.source in {"transcript", "both"}
        ]
        return SubgraphResult(
            nodes=nodes,
            edges=edges,
            replay_hits=replay_hits,
            hybrid_hits=hybrid_hits,
            retrieval_mode=retrieval_mode,
            query=query,
            total_nodes_in_graph=total_nodes,
        )

    def tiered_query(
        self,
        *,
        query: str,
        project: str = "",
        repo_id: str | None = None,
        max_nodes: int = 20,
        max_depth: int = 2,
        top_k_windows: int | None = None,
        agent_id: str = "",
        session_id: str = "",
        include_invalidated: bool = False,
        as_of: datetime | None = None,
    ) -> SubgraphResult:
        query_text = query.strip()
        if not query_text:
            raise ValueError("Query cannot be empty.")
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        resolved_repo_id = repo_id or self.ensure_repo(project or "default")
        query_embedding = self.embedding_model.embed(self._expand_query_aliases(query_text))
        windows = self.get_repo_windows(resolved_repo_id)
        now = time.time()
        replay_session_scores = self._query_replay_session_scores(
            query=query_text,
            query_embedding=query_embedding,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )
        window_scores: list[tuple[float, ContextWindow]] = []
        for window in windows:
            window_embedding = self.get_window_embedding(window.id)
            if window_embedding is None:
                continue
            similarity = max(self.embedding_model.cosine_similarity(query_embedding, window_embedding), 0.0)
            similarity = self._blend_session_signal(
                base_similarity=similarity,
                session_signal=replay_session_scores.get(window.session_id, 0.0),
            )
            recency = recency_weight(
                window.updated_at.timestamp(),
                now=now,
                half_life_days=self.recency_half_life_days,
            )
            window_scores.append(((0.6 * similarity) + (0.4 * recency), window))

        if not window_scores:
            fallback = self._query_graph_only(
                query=query_text,
                max_nodes=max_nodes,
                max_depth=max_depth,
                expand_depth=0,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                include_invalidated=include_invalidated,
                as_of=as_of,
            )
            fallback.retrieval_mode = "flat_fallback"
            return fallback

        window_scores.sort(key=lambda item: (item[0], item[1].updated_at.timestamp()), reverse=True)
        selected_windows = [
            window for _, window in window_scores[: max(1, top_k_windows or self.tiered_retrieval_top_k_windows)]
        ]
        selected_window_ids = {window.id for window in selected_windows}

        with self._lock, self._pool.checkout() as connection:
            candidate_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type,
                       tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                       created_at, updated_at, access_count, embedding, tenant_id
                FROM nodes
                WHERE tenant_id = ? AND context_window_id IN ({})
                  AND embedding IS NOT NULL
                """.format(", ".join("?" for _ in selected_window_ids)),
                (self.tenant_id, *selected_window_ids),
            ).fetchall()
            total_nodes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM nodes WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchone()[0]
            )

            if not candidate_rows:
                fallback = self._query_graph_only(
                    query=query_text,
                    max_nodes=max_nodes,
                    max_depth=max_depth,
                    expand_depth=0,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                    include_invalidated=include_invalidated,
                    as_of=as_of,
                )
                fallback.retrieval_mode = "flat_fallback"
                return fallback

            candidates: list[Node] = []
            similarity_by_id: dict[str, float] = {}
            active_session_id = _retrieval_session_scope(
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            for row in candidate_rows:
                node = self._row_to_node(row)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=active_session_id):
                    continue
                candidates.append(node)

            # Apply temporal validity filtering
            candidates = _filter_valid_nodes(
                candidates,
                include_invalidated=include_invalidated,
                as_of=as_of,
            )

            if not candidates:
                fallback = self._query_graph_only(
                    query=query_text,
                    max_nodes=max_nodes,
                    max_depth=max_depth,
                    expand_depth=0,
                    agent_id=agent_id,
                    project=project,
                    session_id=session_id,
                    include_invalidated=include_invalidated,
                    as_of=as_of,
                )
                fallback.retrieval_mode = "flat_fallback"
                return fallback

            final_candidates: list[Node] = []
            for node in candidates:
                row = next(r for r in candidate_rows if r["id"] == node.id)
                semantic = max(
                    self.embedding_model.cosine_similarity(
                        query_embedding, self.embedding_model.from_bytes(row["embedding"])
                    ),
                    0.0,
                )
                lexical = self._lexical_score_for_node(query_text, node)
                similarity = max(0.0, min(1.0, (0.8 * semantic) + (0.2 * lexical)))
                similarity = self._blend_session_signal(
                    base_similarity=similarity,
                    session_signal=replay_session_scores.get(node.session_id, 0.0),
                )
                final_candidates.append(node)
                similarity_by_id[node.id] = similarity

            candidates = final_candidates
            candidate_ids = [node.id for node in candidates]
            edges = self._fetch_edges_for_nodes(connection, candidate_ids)
            scored_nodes = [
                self._apply_node_score(
                    node,
                    similarity=similarity_by_id.get(node.id, 0.0),
                    edge_weight=self._strongest_edge_weight(node.id, edges),
                    now=now,
                )
                for node in candidates
            ]
            scored_nodes.sort(
                key=lambda node: (
                    node.final_score if node.final_score is not None else 0.0,
                    node.updated_at.timestamp(),
                    node.label.lower(),
                ),
                reverse=True,
            )
            selected_nodes = scored_nodes[:max_nodes]
            selected_ids = [node.id for node in selected_nodes]
            selected_edges = self._fetch_edges_for_nodes(connection, selected_ids)
            self._increment_access_counts(connection, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

        return SubgraphResult(
            nodes=selected_nodes,
            edges=selected_edges,
            retrieval_mode="tiered",
            query=query_text,
            total_nodes_in_graph=total_nodes,
        )

    def debug_retrieval(
        self,
        *,
        query: str,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 10,
        max_depth: int = 2,
        retrieval_mode: str = "graph",
    ) -> dict[str, Any]:
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
        if normalized_mode in {"hybrid", "verbatim"}:
            debug = self.hybrid_retriever().retrieve_debug(
                query=query_text,
                project=project,
                agent_id=agent_id,
                session_id=session_id,
                top_k=max_nodes,
                mode=normalized_mode,
            )
            return {
                "query": query_text,
                "project": project,
                "agent_id": agent_id,
                "session_id": session_id,
                "retrieval_mode": normalized_mode,
                "layers": debug["layers"],
                "hybrid_top_hits": [
                    {
                        "content": hit.content,
                        "score": hit.score,
                        "source": hit.source,
                        "turn_pair_id": hit.turn_pair_id,
                        "node_ids": hit.node_ids,
                        "reasoning_from_reranker": hit.reasoning_from_reranker,
                        "layer_scores": hit.layer_scores,
                    }
                    for hit in debug["hits"]
                ],
                "fused_top20": debug["fused_top20"],
            }

        expanded_query = self._expand_query_aliases(query_text)
        query_embedding = self.embedding_model.embed(expanded_query)
        repo_id = self.ensure_repo(project or "default")
        now = time.time()

        replay_session_scores = self._query_replay_session_scores(
            query=expanded_query,
            query_embedding=query_embedding,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )
        windows = self.get_repo_windows(repo_id)
        window_details: list[dict[str, Any]] = []
        for window in windows:
            recency = recency_weight(
                window.updated_at.timestamp(),
                now=now,
                half_life_days=self.recency_half_life_days,
            )
            detail: dict[str, Any] = {
                "window_id": window.id,
                "repo_id": window.repo_id,
                "session_id": window.session_id,
                "title": window.title,
                "status": window.status,
                "node_count": window.node_count,
                "embedding": "missing",
                "embedding_stale": window.embedding_stale,
                "similarity": None,
                "recency": round(float(recency), 4),
                "routing_score": None,
                "updated_at": window.updated_at.isoformat(),
            }
            window_embedding = self.get_window_embedding(window.id)
            if window_embedding is not None:
                similarity = max(self.embedding_model.cosine_similarity(query_embedding, window_embedding), 0.0)
                similarity = self._blend_session_signal(
                    base_similarity=similarity,
                    session_signal=replay_session_scores.get(window.session_id, 0.0),
                )
                routing_score = (0.6 * similarity) + (0.4 * recency)
                detail.update(
                    {
                        "embedding": "ok",
                        "similarity": round(float(similarity), 4),
                        "routing_score": round(float(routing_score), 4),
                    }
                )
            window_details.append(detail)

        window_details.sort(
            key=lambda item: (
                item["routing_score"] if item["routing_score"] is not None else -1.0,
                item["updated_at"],
            ),
            reverse=True,
        )

        flat_result = self._query_graph_only(
            query=query_text,
            max_nodes=max_nodes,
            max_depth=max_depth,
            expand_depth=0,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )
        tiered_result = self.tiered_query(
            query=query_text,
            project=project,
            repo_id=repo_id,
            max_nodes=max_nodes,
            max_depth=max_depth,
            top_k_windows=self.tiered_retrieval_top_k_windows,
            agent_id=agent_id,
            session_id=session_id,
        )

        def summarize_node(node: Node) -> dict[str, Any]:
            return {
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

        return {
            "query": query_text,
            "expanded_query": expanded_query,
            "repo_id": repo_id,
            "project": project,
            "agent_id": agent_id,
            "session_id": session_id,
            "retrieval_mode": "tiered" if self.tiered_retrieval else "flat",
            "embedding_preview": [round(float(value), 6) for value in query_embedding[:5]],
            "windows_evaluated": len(window_details),
            "all_windows": window_details,
            "selected_windows": [window for window in window_details if window["routing_score"] is not None][
                : max(1, self.tiered_retrieval_top_k_windows)
            ],
            "flat_top_nodes": [summarize_node(node) for node in flat_result.nodes[:max_nodes]],
            "tiered_top_nodes": [summarize_node(node) for node in tiered_result.nodes[:max_nodes]],
            "tiered_result_mode": tiered_result.retrieval_mode,
        }

    def _query_graph_only(
        self,
        *,
        query: str,
        max_nodes: int,
        max_depth: int,
        expand_depth: int,
        agent_id: str,
        project: str,
        session_id: str,
        include_invalidated: bool = False,
        as_of: datetime | None = None,
    ) -> SubgraphResult:
        with self._lock, self._pool.checkout() as connection:
            temporal_hints = infer_temporal_hints(query)
            active_session_id = _retrieval_session_scope(
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )
            filters = ["tenant_id = ?", "embedding IS NOT NULL"]
            params: list[Any] = [self.tenant_id]
            if project.strip():
                filters.append("project = ?")
                params.append(project.strip())
            if active_session_id.strip():
                filters.append("session_id = ?")
                params.append(active_session_id.strip())
            elif agent_id.strip():
                filters.append("agent_id = ?")
                params.append(agent_id.strip())
            node_rows = connection.execute(
                f"""
                SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags,
                       source_prompt, metadata, evidence_records, valid_from, valid_to, created_at,
                       updated_at, access_count, embedding, tenant_id
                FROM nodes
                WHERE {" AND ".join(filters)}
                """,
                tuple(params),
            ).fetchall()
            total_nodes = len(node_rows)
            if total_nodes == 0:
                return SubgraphResult(query=query, total_nodes_in_graph=0)

            def collect_scoped_nodes(active_session_id: str) -> tuple[dict[str, Node], dict[str, np.ndarray]]:
                scoped_nodes: dict[str, Node] = {}
                scoped_embeddings: dict[str, np.ndarray] = {}
                for row in node_rows:
                    node = self._row_to_node(row)
                    if not _scope_matches(node, agent_id=agent_id, project=project, session_id=active_session_id):
                        continue
                    scoped_nodes[node.id] = node
                    scoped_embeddings[node.id] = self.embedding_model.from_bytes(row["embedding"])
                return scoped_nodes, scoped_embeddings

            nodes_by_id, embeddings_by_id = collect_scoped_nodes(active_session_id)

            # Apply temporal validity filtering
            valid_node_ids = {
                node.id
                for node in _filter_valid_nodes(
                    list(nodes_by_id.values()),
                    include_invalidated=include_invalidated,
                    as_of=as_of,
                )
            }
            nodes_by_id = {nid: node for nid, node in nodes_by_id.items() if nid in valid_node_ids}
            embeddings_by_id = {nid: emb for nid, emb in embeddings_by_id.items() if nid in valid_node_ids}

            if not nodes_by_id:
                return SubgraphResult(query=query, total_nodes_in_graph=total_nodes)

            expanded_query = self._expand_query_aliases(query)
            query_embedding = self.embedding_model.embed(expanded_query)
            similarity_by_id = {
                node_id: max(self.embedding_model.cosine_similarity(query_embedding, embedding), 0.0)
                for node_id, embedding in embeddings_by_id.items()
            }
            replay_session_scores = self._query_replay_session_scores(
                query=expanded_query,
                query_embedding=query_embedding,
                agent_id=agent_id,
                project=project,
                session_id=active_session_id,
            )
            similarity_by_id = {
                node_id: self._blend_session_signal(
                    base_similarity=similarity,
                    session_signal=replay_session_scores.get(nodes_by_id[node_id].session_id, 0.0),
                )
                for node_id, similarity in similarity_by_id.items()
            }
            lexical_by_id = {
                node_id: self._lexical_score_for_node(expanded_query, node) for node_id, node in nodes_by_id.items()
            }
            negation_intent = self._has_negation_intent(query)
            negation_boost_by_id = {
                node_id: self._negation_boost(node) if negation_intent else 0.0 for node_id, node in nodes_by_id.items()
            }

            seed_count = min(total_nodes, max(1, max_nodes // 2))
            seed_candidates = [
                (
                    node_id,
                    (0.7 * similarity_by_id.get(node_id, 0.0)) + (0.3 * lexical_by_id.get(node_id, 0.0)),
                    negation_boost_by_id.get(node_id, 0.0),
                    self._seed_temporal_order(nodes_by_id[node_id], temporal_hints),
                )
                for node_id in nodes_by_id
            ]
            if temporal_hints.recency_mode in {"latest", "oldest"}:
                temporal_seed_candidates = [
                    item
                    for item in seed_candidates
                    if item[1] >= TOPIC_RELEVANCE_THRESHOLD
                    and (
                        lexical_by_id.get(item[0], 0.0) > 0.0
                        or similarity_by_id.get(item[0], 0.0) >= TOPIC_SEMANTIC_ONLY_THRESHOLD
                    )
                ]
                if not temporal_seed_candidates:
                    temporal_seed_candidates = sorted(
                        seed_candidates,
                        key=lambda item: (-(item[1] + item[2]), nodes_by_id[item[0]].label.lower()),
                    )[: max_nodes * 2]
                ranked_seed_ids = [
                    item[0]
                    for item in sorted(
                        temporal_seed_candidates,
                        key=lambda item: (item[3], -(item[1] + item[2]), nodes_by_id[item[0]].label.lower()),
                    )[:seed_count]
                ]
            else:
                ranked_seed_ids = [
                    item[0]
                    for item in sorted(
                        seed_candidates,
                        key=lambda item: (-(item[1] + item[2]), item[3], nodes_by_id[item[0]].label.lower()),
                    )[:seed_count]
                ]
            if len(self._split_query_intents(query)) >= 2:
                ranked_seed_ids = self._add_clause_seed_ids(
                    query=query,
                    ranked_seed_ids=ranked_seed_ids,
                    nodes_by_id=nodes_by_id,
                    embeddings_by_id=embeddings_by_id,
                    max_seeds=max_nodes,
                )

            graph = self._load_graph(connection, node_ids=nodes_by_id.keys())
            expanded_depths, expansion_metadata = self._expand_node_depths_with_context(
                graph, ranked_seed_ids, max_depth
            )
            candidate_nodes = [nodes_by_id[node_id] for node_id in expanded_depths if node_id in nodes_by_id]
            temporal_candidates = [node for node in candidate_nodes if within_time_window(node, temporal_hints)]
            if temporal_candidates:
                candidate_nodes = temporal_candidates

            max_access = max((node.access_count for node in candidate_nodes), default=0)
            degree_by_id = dict(graph.degree(expanded_depths.keys()))
            max_degree = max(degree_by_id.values(), default=0)
            candidate_edges = self._fetch_edges_for_nodes(connection, [node.id for node in candidate_nodes])
            scored_nodes = self._sort_scored_nodes(
                candidate_nodes,
                max_nodes=max_nodes,
                temporal_hints=temporal_hints,
                similarity_by_id=similarity_by_id,
                lexical_by_id=lexical_by_id,
                negation_boost_by_id=negation_boost_by_id,
                degree_by_id=degree_by_id,
                max_access=max_access,
                max_degree=max_degree,
                max_depth=max_depth,
                expanded_depths=expanded_depths,
                edges=candidate_edges,
                expansion_metadata=expansion_metadata,
            )
            scored_nodes = self._diversify_multi_intent_nodes(
                query=query,
                ranked_nodes=scored_nodes,
                embeddings_by_id=embeddings_by_id,
                max_nodes=max_nodes,
            )
            result_limit = max_nodes if expand_depth == 0 else max_nodes + max(1, max_nodes // 2)
            selected_nodes = self._enforce_clause_coverage(
                query=query,
                selected_nodes=scored_nodes[:result_limit],
                ranked_nodes=scored_nodes,
                embeddings_by_id=embeddings_by_id,
                max_nodes=result_limit,
            )
            candidate_pool = {node.id: node for node in candidate_nodes}
            selected_nodes = self._ensure_support_coverage(selected_nodes, candidate_pool, graph, result_limit)
            selected_ids = [node.id for node in selected_nodes]

            edges = self._fetch_edges_for_nodes(connection, selected_ids)
            self._increment_access_counts(connection, selected_ids)
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
        with self._lock.read(), self._pool.checkout() as connection:
            filters = ["tenant_id = ?", "embedding IS NOT NULL"]
            params: list[Any] = [self.tenant_id]
            if project.strip():
                filters.append("project = ?")
                params.append(project.strip())
            if session_id.strip():
                filters.append("session_id = ?")
                params.append(session_id.strip())
            elif agent_id.strip():
                filters.append("agent_id = ?")
                params.append(agent_id.strip())
            # Cap the rows fetched so we don't scan and score the entire tenant.
            # The downstream scorer uses up to max_hits results; read a generous
            # multiple so semantic ranking still has a good pool to draw from.
            fetch_limit = min(max(500, max_hits * 4), 5000)
            rows = connection.execute(
                f"""
                SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role, transcript_text, embedding, metadata
                FROM transcript_records
                WHERE {" AND ".join(filters)}
                ORDER BY observed_at DESC, turn_index DESC
                LIMIT ?
                """,
                (*params, fetch_limit),
            ).fetchall()
        if not rows:
            return []
        query_embedding = self.embedding_model.embed(query)
        temporal_hints = infer_temporal_hints(query)
        timestamps = np.asarray([_parse_datetime(row["observed_at"]).timestamp() for row in rows], dtype=np.float64)
        max_timestamp = float(np.max(timestamps))
        min_timestamp = float(np.min(timestamps))
        span = max(max_timestamp - min_timestamp, 1.0)

        def build_hits(active_session_id: str) -> list[tuple[float, ReplayHit]]:
            hits: list[tuple[float, ReplayHit]] = []
            for row, raw_timestamp in zip(rows, timestamps, strict=True):
                record = self._row_to_transcript_record(row)
                embedding = self.embedding_model.from_bytes(row["embedding"])
                semantic_score = max(self.embedding_model.cosine_similarity(query_embedding, embedding), 0.0)
                lexical_score = lexical_overlap(query, record.role, record.transcript_text)
                temporal_score = 0.0
                if temporal_hints.recency_mode == "latest":
                    temporal_score = float((raw_timestamp - min_timestamp) / span)
                elif temporal_hints.recency_mode == "oldest":
                    temporal_score = float((max_timestamp - raw_timestamp) / span)
                role_score = 1.0 if record.role == "user" else 0.8
                score = (0.6 * semantic_score) + (0.2 * lexical_score) + (0.1 * temporal_score) + (0.1 * role_score)
                hits.append(
                    (
                        score,
                        ReplayHit(
                            score=score,
                            session_id=record.session_id,
                            turn_index=record.turn_index,
                            role=record.role,
                            transcript_text=record.transcript_text,
                            transcript_snippet=record.transcript_text[:280],
                            observed_at=record.observed_at,
                        ),
                    )
                )
            return hits

        active_session_id = _retrieval_session_scope(
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )
        hits = build_hits(active_session_id)
        return [
            item[1]
            for item in sorted(hits, key=lambda item: (-item[0], -item[1].observed_at.timestamp(), item[1].turn_index))[
                :max_hits
            ]
        ]

    def _query_replay_session_scores(
        self,
        *,
        query: str,
        query_embedding: np.ndarray | None = None,
        agent_id: str,
        project: str,
        session_id: str,
    ) -> dict[str, float]:
        with self._lock.read(), self._pool.checkout() as connection:
            filters = ["tenant_id = ?", "embedding IS NOT NULL"]
            params: list[Any] = [self.tenant_id]
            if project.strip():
                filters.append("project = ?")
                params.append(project.strip())
            if session_id.strip():
                filters.append("session_id = ?")
                params.append(session_id.strip())
            elif agent_id.strip():
                filters.append("agent_id = ?")
                params.append(agent_id.strip())
            fetch_limit = 5000
            rows = connection.execute(
                f"""
                SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role, transcript_text, embedding, metadata
                FROM transcript_records
                WHERE {" AND ".join(filters)}
                ORDER BY observed_at DESC, turn_index DESC
                LIMIT ?
                """,
                (*params, fetch_limit),
            ).fetchall()
        if not rows:
            return {}

        query_vector = query_embedding if query_embedding is not None else self.embedding_model.embed(query)
        _retrieval_session_scope(
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )
        scores_by_session: dict[str, float] = {}
        for row in rows:
            record = self._row_to_transcript_record(row)
            scoped_session_id = record.session_id.strip()
            if not scoped_session_id:
                continue
            embedding = self.embedding_model.from_bytes(row["embedding"])
            semantic_score = max(self.embedding_model.cosine_similarity(query_vector, embedding), 0.0)
            lexical_score = lexical_overlap(query, record.role, record.transcript_text)
            role_score = 1.0 if record.role == "user" else 0.8
            score = max(0.0, min(1.0, (0.65 * semantic_score) + (0.25 * lexical_score) + (0.10 * role_score)))
            previous = scores_by_session.get(scoped_session_id, 0.0)
            if score > previous:
                scores_by_session[scoped_session_id] = score
        return scores_by_session

    def _recent_transcript_session_scores(
        self,
        *,
        agent_id: str,
        project: str,
        session_id: str,
    ) -> dict[str, float]:
        with self._lock.read(), self._pool.checkout() as connection:
            rows = connection.execute(
                """
                SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role, transcript_text, metadata
                FROM transcript_records
                WHERE tenant_id = ?
                ORDER BY observed_at DESC, turn_index DESC
                """,
                (self.tenant_id,),
            ).fetchall()
        if not rows:
            return {}

        active_session_id = _retrieval_session_scope(
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )
        timestamps = [
            self._row_to_transcript_record(row).observed_at.timestamp()
            for row in rows
            if self._transcript_scope_matches(
                self._row_to_transcript_record(row),
                agent_id=agent_id,
                project=project,
                session_id=active_session_id,
            )
        ]
        if not timestamps:
            return {}
        now = max(timestamps)
        scores_by_session: dict[str, float] = {}
        for row in rows:
            record = self._row_to_transcript_record(row)
            if not self._transcript_scope_matches(
                record, agent_id=agent_id, project=project, session_id=active_session_id
            ):
                continue
            scoped_session_id = record.session_id.strip()
            if not scoped_session_id:
                continue
            score = recency_weight(
                record.observed_at.timestamp(),
                now=now,
                half_life_days=self.recency_half_life_days,
            )
            previous = scores_by_session.get(scoped_session_id, 0.0)
            if score > previous:
                scores_by_session[scoped_session_id] = score
        return scores_by_session

    def _blend_session_signal(
        self,
        *,
        base_similarity: float,
        session_signal: float,
        session_weight: float = 0.25,
    ) -> float:
        base = max(0.0, min(1.0, base_similarity))
        session = max(0.0, min(1.0, session_signal))
        return max(0.0, min(1.0, ((1.0 - session_weight) * base) + (session_weight * session)))

    def _build_fusion_hits(self, graph_result: SubgraphResult, replay_hits: list[ReplayHit]) -> list[FusionHit]:
        rrf_k = 60.0
        replay_by_session = {hit.session_id for hit in replay_hits if hit.session_id}
        graph_edge_map: dict[str, list[dict[str, Any]]] = {}
        graph_nodes_by_session = {node.session_id: node for node in graph_result.nodes if node.session_id}
        combined: dict[str, FusionHit] = {}

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
            source_lane = "both" if node.session_id and node.session_id in replay_by_session else "graph"
            combined[f"graph:{node.id}"] = FusionHit(
                content=node.content,
                score=1.0 / (rrf_k + index),
                source_lane=source_lane,
                graph_rank=index,
                replay_rank=None,
                fused_rank=0,
                node_id=node.id,
                node_type=node.node_type.value,
                edges=graph_edge_map.get(node.id, []),
                session_id=node.session_id or None,
            )

        for index, hit in enumerate(replay_hits, start=1):
            contribution = 1.0 / (rrf_k + index)
            matching_graph = graph_nodes_by_session.get(hit.session_id) if hit.session_id else None
            if matching_graph is not None:
                existing = combined.get(f"graph:{matching_graph.id}")
                if existing is not None:
                    existing.score += contribution
                    existing.source_lane = "both"
                    existing.replay_rank = index
                    existing.session_id = hit.session_id or None
                    continue
                key = f"both:{matching_graph.id}:{hit.session_id}:{hit.turn_index}"
                source_lane = "both"
            else:
                key = f"replay:{hit.session_id}:{hit.turn_index}:{index}"
                source_lane = "replay"
            combined[key] = FusionHit(
                content=hit.transcript_text,
                score=contribution,
                source_lane=source_lane,
                graph_rank=None,
                replay_rank=index,
                fused_rank=0,
                session_id=hit.session_id or None,
                transcript_snippet=hit.transcript_snippet,
                turn_index=hit.turn_index,
            )

        ordered = sorted(
            combined.values(),
            key=lambda item: (-item.score, 0 if item.source_lane in {"both", "graph"} else 1, item.content.lower()),
        )
        for index, item in enumerate(ordered, start=1):
            item.fused_rank = index
        return ordered

    def get_related(self, *, node_id: str, max_depth: int = 2) -> SubgraphResult:
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._pool.checkout() as connection:
            self._require_node(connection, node_id)
            node_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata, evidence_records, valid_from, valid_to,
                       created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                """,
                (self.tenant_id,),
            ).fetchall()
            nodes_by_id = {row["id"]: self._row_to_node(row) for row in node_rows}
            graph = self._load_graph(connection, node_ids=nodes_by_id.keys())
            related_ids = list(self._expand_node_depths(graph, [node_id], max_depth))

            ordered_nodes: list[Node] = []
            seen: set[str] = set()
            for related_id in [node_id, *related_ids]:
                if related_id in seen:
                    continue
                seen.add(related_id)
                ordered_nodes.append(nodes_by_id[related_id])

            edges = self._fetch_edges_for_nodes(connection, [node.id for node in ordered_nodes])
            now = time.time()
            for node in ordered_nodes:
                distance = 0 if node.id == node_id else nx.shortest_path_length(graph, source=node_id, target=node.id)
                edge_weight = self._strongest_edge_weight(node.id, edges)
                similarity = max(0.0, 1.0 - (0.25 * distance))
                self._apply_node_score(node, similarity=similarity, edge_weight=edge_weight, now=now)
            ordered_nodes.sort(
                key=lambda node: (
                    -(node.final_score if node.final_score is not None else 0.0),
                    0 if node.id == node_id else 1,
                    -node.updated_at.timestamp(),
                    node.label.lower(),
                )
            )
            self._increment_access_counts(connection, [node.id for node in ordered_nodes])
            for node in ordered_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=ordered_nodes,
                edges=edges,
                query=f"related:{node_id}",
                total_nodes_in_graph=len(nodes_by_id),
            )

    def get_stats(self) -> GraphStats:
        with self._lock, self._pool.checkout() as connection:
            total_nodes = int(
                connection.execute("SELECT COUNT(*) FROM nodes WHERE tenant_id = ?", (self.tenant_id,)).fetchone()[0]
            )
            total_edges = int(
                connection.execute("SELECT COUNT(*) FROM edges WHERE tenant_id = ?", (self.tenant_id,)).fetchone()[0]
            )
            total_repos = int(
                connection.execute("SELECT COUNT(*) FROM repos WHERE tenant_id = ?", (self.tenant_id,)).fetchone()[0]
            )
            total_context_windows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_windows WHERE tenant_id = ?", (self.tenant_id,)
                ).fetchone()[0]
            )
            total_context_window_edges = int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_window_edges WHERE tenant_id = ?", (self.tenant_id,)
                ).fetchone()[0]
            )
            windows_with_embeddings = int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_windows WHERE tenant_id = ? AND embedding IS NOT NULL",
                    (self.tenant_id,),
                ).fetchone()[0]
            )
            windows_with_stale_embeddings = int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_windows WHERE tenant_id = ? AND embedding_stale = 1",
                    (self.tenant_id,),
                ).fetchone()[0]
            )

            counts = {node_type.value: 0 for node_type in NodeType}
            for row in connection.execute(
                "SELECT node_type, COUNT(*) AS count FROM nodes WHERE tenant_id = ? GROUP BY node_type",
                (self.tenant_id,),
            ).fetchall():
                counts[str(row["node_type"])] = int(row["count"])
            window_status_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM context_windows WHERE tenant_id = ? GROUP BY status",
                    (self.tenant_id,),
                ).fetchall()
            }
            window_edge_type_counts = {
                str(row["edge_type"]): int(row["count"])
                for row in connection.execute(
                    "SELECT edge_type, COUNT(*) AS count FROM context_window_edges WHERE tenant_id = ? GROUP BY edge_type",
                    (self.tenant_id,),
                ).fetchall()
            }

            most_connected_rows = connection.execute(
                """
                SELECT n.id, n.label, n.node_type,
                       COUNT(e.id) AS connection_count
                FROM nodes AS n
                LEFT JOIN edges AS e
                    ON (n.id = e.source_id OR n.id = e.target_id) AND e.tenant_id = ?
                WHERE n.tenant_id = ?
                GROUP BY n.id
                ORDER BY connection_count DESC, n.updated_at DESC
                LIMIT 5
                """,
                (self.tenant_id, self.tenant_id),
            ).fetchall()

            most_recent_rows = connection.execute(
                """
                SELECT id, label, node_type, updated_at
                FROM nodes
                WHERE tenant_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 5
                """,
                (self.tenant_id,),
            ).fetchall()

            return GraphStats(
                total_nodes=total_nodes,
                total_edges=total_edges,
                total_repos=total_repos,
                total_context_windows=total_context_windows,
                context_window_status_breakdown=window_status_counts,
                total_context_window_edges=total_context_window_edges,
                context_window_edge_type_breakdown=window_edge_type_counts,
                windows_with_embeddings=windows_with_embeddings,
                windows_with_stale_embeddings=windows_with_stale_embeddings,
                node_type_breakdown=counts,
                most_connected_nodes=[
                    ConnectedNodeStat(
                        id=row["id"],
                        label=row["label"],
                        node_type=NodeType(row["node_type"]),
                        connection_count=int(row["connection_count"]),
                    )
                    for row in most_connected_rows
                ],
                most_recent_nodes=[
                    RecentNodeStat(
                        id=row["id"],
                        label=row["label"],
                        node_type=NodeType(row["node_type"]),
                        updated_at=_parse_datetime(row["updated_at"]),
                    )
                    for row in most_recent_rows
                ],
            )

    def get_topics(self) -> TopicResult:
        with self._lock, self._pool.checkout() as connection:
            node_rows = connection.execute(
                """
                SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata,
                       evidence_records, valid_from, valid_to, created_at, updated_at, access_count, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                """,
                (self.tenant_id,),
            ).fetchall()
            if not node_rows:
                return TopicResult(clusters=[], total_clusters=0)
            nodes = [self._row_to_node(row) for row in node_rows]
            graph = self._load_graph(connection, node_ids=[node.id for node in nodes]).to_undirected()
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

    def _build_prime_summary(
        self,
        *,
        selected_nodes: list[Node],
        edges: list[Edge],
        total_nodes_in_graph: int,
        project: str = "",
    ) -> str:
        """Build a structured summary of prime context with type and relationship counts."""
        # Count node types
        type_counts: dict[str, int] = {}
        for node in selected_nodes:
            type_counts[node.node_type.value] = type_counts.get(node.node_type.value, 0) + 1

        # Count edge relationships
        relationship_counts: dict[str, int] = {}
        for edge in edges:
            rel = edge.relationship
            relationship_counts[rel] = relationship_counts.get(rel, 0) + 1

        # Build type breakdown
        type_breakdown = (
            ", ".join(f"{count} {ttype}" for ttype, count in sorted(type_counts.items())) if type_counts else "no nodes"
        )

        # Build relationship breakdown
        relationship_breakdown = (
            ", ".join(f"{count} {rel}" for rel, count in sorted(relationship_counts.items()))
            if relationship_counts
            else "no edges"
        )

        # Check for contradictions
        has_contradictions = "contradicts" in relationship_counts
        contradiction_warning = " [⚠ Contradictions present]" if has_contradictions else ""

        # Check for questions
        has_questions = "question" in type_counts
        question_warning = " [?]" if has_questions else ""

        # Build base summary
        if project.strip():
            base = f"Prime context for project '{project}': {len(selected_nodes)} nodes ({type_breakdown}) with {len(edges)} edges ({relationship_breakdown})"
        else:
            base = f"Prime context: {len(selected_nodes)} nodes ({type_breakdown}) with {len(edges)} edges ({relationship_breakdown})"

        base += f" from {total_nodes_in_graph} total nodes"
        base += contradiction_warning + question_warning

        return base

    def _build_timeline_items(
        self,
        *,
        nodes: list[Node],
        edges: list[Edge],
        include_evidence: bool,
        limit: int,
    ) -> list[ContextTimelineItem]:
        items: list[ContextTimelineItem] = []
        now = time.time()
        for node in nodes:
            node_recency = recency_weight(
                node.updated_at.timestamp(),
                now=now,
                half_life_days=self.recency_half_life_days,
            )
            items.append(
                ContextTimelineItem(
                    kind="node_created",
                    timestamp=node.created_at,
                    label=node.label,
                    summary=node.content,
                    node_id=node.id,
                    recency_score=node_recency,
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
                        recency_score=node_recency,
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
                            recency_score=node_recency,
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
                    recency_score=recency_weight(
                        edge.created_at.timestamp(),
                        now=now,
                        half_life_days=self.recency_half_life_days,
                    ),
                )
            )
        return sorted(
            items,
            key=lambda item: (item.timestamp, item.kind, item.label),
            reverse=True,
        )[:limit]

    def _relation_priority(self, relationship: str) -> float:
        return RELATION_WEIGHTS.get(relationship, 0.50)

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

    def _strongest_edge_weight(self, node_id: str, edges: list[Edge]) -> float:
        strongest = 0.0
        for edge in edges:
            if edge.source_id == node_id or edge.target_id == node_id:
                strongest = max(strongest, max(0.0, min(1.0, float(edge.weight))))
        return strongest

    def _apply_node_score(
        self,
        node: Node,
        *,
        similarity: float,
        edge_weight: float,
        now: float | None = None,
    ) -> Node:
        recency = recency_weight(
            node.updated_at.timestamp(),
            now=now,
            half_life_days=self.recency_half_life_days,
        )
        final = score_node(
            similarity,
            node.updated_at.timestamp(),
            edge_weight=edge_weight,
            now=now,
            half_life_days=self.recency_half_life_days,
            superseded=self._node_is_superseded(node),
        )
        node.similarity_score = max(0.0, min(1.0, similarity))
        node.recency_score = recency
        node.edge_score = max(0.0, min(1.0, edge_weight))
        node.final_score = final
        return node

    def _sort_scored_nodes(
        self,
        candidate_nodes: list[Node],
        *,
        max_nodes: int,
        temporal_hints: Any,
        similarity_by_id: dict[str, float],
        lexical_by_id: dict[str, float],
        negation_boost_by_id: dict[str, float],
        degree_by_id: dict[str, int],
        max_access: int,
        max_degree: int,
        max_depth: int,
        expanded_depths: dict[str, int],
        edges: list[Edge] | None = None,
        expansion_metadata: dict[str, ExpansionMeta] | None = None,
    ) -> list[Node]:
        edges = edges or []
        now = time.time()

        def combined_score(node: Node) -> float:
            semantic = similarity_by_id.get(node.id, 0.0)
            lexical = lexical_by_id.get(node.id, 0.0)
            similarity = max(0.0, min(1.0, (0.8 * semantic) + (0.2 * lexical)))
            base_edge_weight = self._strongest_edge_weight(node.id, edges)
            degree_component = degree_by_id.get(node.id, 0) / max_degree if max_degree > 0 else 0.0
            depth_component = 1.0 / (1.0 + expanded_depths.get(node.id, max_depth + 1))
            edge_weight = max(base_edge_weight, (0.6 * degree_component) + (0.4 * depth_component))
            base = (
                score_node(
                    similarity,
                    node.updated_at.timestamp(),
                    edge_weight=edge_weight,
                    now=now,
                    half_life_days=self.recency_half_life_days,
                    superseded=self._node_is_superseded(node),
                )
                + temporal_score_adjustment(node, temporal_hints)
                + negation_boost_by_id.get(node.id, 0.0)
            )

            if expansion_metadata is not None and node.id in expansion_metadata:
                meta = expansion_metadata[node.id]
                base += RELATION_SCORE_BOOST.get(meta.via_relation, 0.0)

            self._apply_node_score(node, similarity=similarity, edge_weight=edge_weight, now=now)
            node.final_score = base
            return base

        if temporal_hints.recency_mode in {"latest", "oldest"}:
            topic_scores = {
                node.id: (0.7 * similarity_by_id.get(node.id, 0.0))
                + (0.3 * lexical_by_id.get(node.id, 0.0))
                + negation_boost_by_id.get(node.id, 0.0)
                for node in candidate_nodes
            }
            topical_nodes = [
                node
                for node in candidate_nodes
                if topic_scores.get(node.id, 0.0) >= TOPIC_RELEVANCE_THRESHOLD
                and (
                    lexical_by_id.get(node.id, 0.0) > 0.0
                    or similarity_by_id.get(node.id, 0.0) >= TOPIC_SEMANTIC_ONLY_THRESHOLD
                )
            ]
            if not topical_nodes:
                topical_nodes = sorted(
                    candidate_nodes,
                    key=lambda node: (-topic_scores.get(node.id, 0.0), node.label.lower()),
                )[: max_nodes * 2]
            else:
                best_topic_score = max(topic_scores.get(node.id, 0.0) for node in topical_nodes)
                narrowed_topical_nodes = [
                    node
                    for node in topical_nodes
                    if topic_scores.get(node.id, 0.0) >= best_topic_score - TEMPORAL_TOPIC_MARGIN
                ]
                if narrowed_topical_nodes:
                    topical_nodes = narrowed_topical_nodes
            if temporal_hints.recency_mode == "latest":
                return sorted(
                    topical_nodes,
                    key=lambda node: (
                        -node.updated_at.timestamp(),
                        -topic_scores.get(node.id, 0.0),
                        node.label.lower(),
                    ),
                )
            return sorted(
                topical_nodes,
                key=lambda node: (
                    node.created_at.timestamp(),
                    -topic_scores.get(node.id, 0.0),
                    node.label.lower(),
                ),
            )
        # Pair each Node with a lightweight ScoredNodeView built once. Sorting on
        # the pre-computed slot fields avoids calling .timestamp() and .lower()
        # inside the comparator on every pair comparison (Timsort is O(N log N)).
        # Pairing keeps the Node→view association 1:1 so we don't need a dict
        # round-trip or duplicate-id safety nets in the result construction.

        view_node_pairs: list[tuple[ScoredNodeView, Node]] = [
            (
                ScoredNodeView(
                    node_id=node.id,
                    updated_at_ts=node.updated_at.timestamp(),
                    final_score=combined_score(node),
                    label_lower=node.label.lower(),
                ),
                node,
            )
            for node in candidate_nodes
        ]
        view_node_pairs.sort(key=lambda pair: (-pair[0].final_score, -pair[0].updated_at_ts, pair[0].label_lower))
        return [node for _, node in view_node_pairs]

    def _add_clause_seed_ids(
        self,
        *,
        query: str,
        ranked_seed_ids: list[str],
        nodes_by_id: dict[str, Node],
        embeddings_by_id: dict[str, np.ndarray],
        max_seeds: int,
    ) -> list[str]:
        clauses = [
            clause.strip(" ?,.;:")
            for clause in re.split(r"\b(?:and|with|plus)\b", query, flags=re.IGNORECASE)
            if len(clause.strip(" ?,.;:")) >= 4
        ]
        if len(clauses) < 2:
            return ranked_seed_ids

        expanded = list(ranked_seed_ids)
        seen = set(expanded)
        for clause in clauses[:4]:
            expanded_clause = self._expand_intent_query(clause, query)
            clause_embedding = self.embedding_model.embed(expanded_clause)
            lexical_candidates: list[tuple[float, str]] = []
            semantic_candidates: list[tuple[float, str]] = []
            for node_id, node in nodes_by_id.items():
                semantic = max(
                    self.embedding_model.cosine_similarity(clause_embedding, embeddings_by_id[node_id]),
                    0.0,
                )
                lexical = self._lexical_score_for_node(expanded_clause, node)
                score = (0.45 * semantic) + (0.55 * lexical)
                if lexical > 0.0:
                    lexical_candidates.append((score, node_id))
                elif semantic >= 0.75:
                    semantic_candidates.append((score, node_id))
            best_id = ""
            if lexical_candidates:
                best_id = max(lexical_candidates, key=lambda item: item[0])[1]
            elif semantic_candidates:
                best_id = max(semantic_candidates, key=lambda item: item[0])[1]
            if best_id and best_id not in seen:
                expanded.append(best_id)
                seen.add(best_id)
            if len(expanded) >= max_seeds:
                break

        return expanded

    def _has_negation_intent(self, query: str) -> bool:
        lowered = normalize_text(query)
        return any(term in lowered for term in NEGATION_QUERY_TERMS)

    def _negation_boost(self, node: Node) -> float:
        text = normalize_text(" ".join([node.label, node.content, *node.tags]))
        return NEGATION_SCORE_BOOST if any(term in text for term in NEGATION_NODE_TERMS) else 0.0

    def _split_query_intents(self, query: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", query.strip())
        parts = [
            part.strip(" ?,.;:")
            for part in re.split(
                r"\b(?:and|plus|with|or|because|justified by|supported by|due to)\b", normalized, flags=re.IGNORECASE
            )
            if len(part.strip(" ?,.;:")) >= 4
        ]
        if len(parts) < 2:
            return []
        return parts[:4]

    def _expand_query_aliases(self, query: str) -> str:
        normalized = query.lower()
        aliases = [alias for trigger, alias in QUERY_ALIAS_TERMS if trigger in normalized]
        if not aliases:
            return query
        return " ".join([query, *aliases])

    def _expand_intent_query(self, intent: str, full_query: str) -> str:
        return self._expand_query_aliases(f"{intent} {full_query}".strip())

    def _lexical_score_for_node(self, query: str, node: Node) -> float:
        tag_text = " ".join(tag.replace(":", " ").replace("_", " ").replace("-", " ") for tag in node.tags)
        content_score = lexical_overlap(query, node.label, node.content)
        if not tag_text:
            return content_score
        tag_score = lexical_overlap(query, tag_text, "")
        return max(content_score, tag_score)

    def _diversify_multi_intent_nodes(
        self,
        *,
        query: str,
        ranked_nodes: list[Node],
        embeddings_by_id: dict[str, np.ndarray],
        max_nodes: int,
    ) -> list[Node]:
        intents = self._split_query_intents(query)
        if len(intents) < 2 or max_nodes < 2:
            return ranked_nodes

        selected: list[Node] = []
        selected_ids: set[str] = set()
        for intent in intents:
            expanded_intent = self._expand_intent_query(intent, query)
            intent_embedding = self.embedding_model.embed(expanded_intent)
            lexical_scored: list[tuple[float, Node]] = []
            semantic_scored: list[tuple[float, Node]] = []
            for node in ranked_nodes:
                embedding = embeddings_by_id.get(node.id)
                if embedding is None:
                    continue
                semantic = max(self.embedding_model.cosine_similarity(intent_embedding, embedding), 0.0)
                lexical = self._lexical_score_for_node(expanded_intent, node)
                score = (0.35 * semantic) + (0.65 * lexical)
                if lexical > 0.0:
                    lexical_scored.append((score, node))
                elif semantic >= 0.75:
                    semantic_scored.append((score, node))
            scored = lexical_scored or semantic_scored
            if not scored:
                continue
            score, node = max(scored, key=lambda item: (item[0], item[1].updated_at.timestamp()))
            if score >= 0.18 and node.id not in selected_ids:
                selected.append(node)
                selected_ids.add(node.id)
            if len(selected) >= max_nodes:
                return selected

        for node in ranked_nodes:
            if node.id not in selected_ids:
                selected.append(node)
                selected_ids.add(node.id)
            if len(selected) >= len(ranked_nodes):
                break
        return selected

    def _enforce_clause_coverage(
        self,
        *,
        query: str,
        selected_nodes: list[Node],
        ranked_nodes: list[Node],
        embeddings_by_id: dict[str, np.ndarray],
        max_nodes: int,
    ) -> list[Node]:
        intents = self._split_query_intents(query)
        if len(intents) < 2 or not ranked_nodes:
            return selected_nodes[:max_nodes]

        selected = list(selected_nodes[:max_nodes])
        selected_ids = {node.id for node in selected}
        if not selected:
            return selected

        clause_candidates: list[Node] = []
        for intent in intents:
            expanded_intent = self._expand_intent_query(intent, query)
            intent_embedding = self.embedding_model.embed(expanded_intent)
            lexical_scored: list[tuple[float, Node]] = []
            semantic_scored: list[tuple[float, Node]] = []
            for node in ranked_nodes:
                embedding = embeddings_by_id.get(node.id)
                if embedding is None:
                    continue
                semantic = max(self.embedding_model.cosine_similarity(intent_embedding, embedding), 0.0)
                lexical = self._lexical_score_for_node(expanded_intent, node)
                score = (0.35 * semantic) + (0.65 * lexical)
                if lexical > 0.0:
                    lexical_scored.append((score, node))
                elif semantic >= 0.75:
                    semantic_scored.append((score, node))
            scored = lexical_scored or semantic_scored
            if not scored:
                continue
            score, node = max(scored, key=lambda item: (item[0], item[1].updated_at.timestamp()))
            if score >= 0.20:
                clause_candidates.append(node)

        for node in clause_candidates:
            if node.id in selected_ids:
                continue
            if len(selected) < max_nodes:
                selected.append(node)
                selected_ids.add(node.id)
                continue
            replacement_index = len(selected) - 1
            if replacement_index < 0:
                break
            selected_ids.remove(selected[replacement_index].id)
            selected[replacement_index] = node
            selected_ids.add(node.id)

        return selected[:max_nodes]

    def _expand_node_depths_with_context(
        self,
        graph: nx.DiGraph,
        seed_ids: list[str],
        max_depth: int,
        *,
        min_priority: float = 0.20,
        decay: float = 0.70,
    ) -> tuple[dict[str, int], dict[str, ExpansionMeta]]:
        ordered: dict[str, int] = {}
        metadata: dict[str, ExpansionMeta] = {}
        seen: set[str] = set()

        # Heap entries: (neg_priority, tiebreaker, node_id, depth, via_relation, from_node, effective_priority)
        _counter = 0
        heap: list[tuple[float, int, str, int, str, str, float]] = []

        for seed_id in seed_ids:
            heapq.heappush(heap, (0.0, _counter, seed_id, 0, "seed", "", 0.0))
            _counter += 1

        while heap:
            _neg_pri, _, node_id, depth, via_relation, from_node, effective_priority = heapq.heappop(heap)

            if node_id in seen:
                continue
            seen.add(node_id)
            ordered[node_id] = depth
            if via_relation != "seed":
                metadata[node_id] = ExpansionMeta(
                    via_relation=via_relation,
                    from_node=from_node,
                    effective_priority=effective_priority,
                )

            if depth >= max_depth:
                continue

            neighbors_with_data: list[tuple[str, dict]] = []

            if graph.has_node(node_id):
                for _, neighbor, data in graph.edges(node_id, data=True):
                    if neighbor not in seen:
                        neighbors_with_data.append((neighbor, data))

                for predecessor, _, data in graph.in_edges(node_id, data=True):
                    if predecessor not in seen:
                        neighbors_with_data.append((predecessor, data))

            for neighbor, data in neighbors_with_data:
                relationship = data.get("relationship", "relates_to")
                weight = float(data.get("weight", 1.0))

                effective = self._relation_priority(relationship) * weight * (decay**depth)

                if effective < min_priority:
                    continue

                heapq.heappush(
                    heap,
                    (-effective, _counter, neighbor, depth + 1, relationship, node_id, effective),
                )
                _counter += 1

        return ordered, metadata

    def _expand_node_depths(
        self,
        graph: nx.DiGraph,
        seed_ids: list[str],
        max_depth: int,
        *,
        min_priority: float = 0.20,
        decay: float = 0.70,
    ) -> dict[str, int]:
        ordered, _ = self._expand_node_depths_with_context(
            graph, seed_ids, max_depth, min_priority=min_priority, decay=decay
        )
        return ordered

    def _ensure_support_coverage(
        self,
        selected_nodes: list[Node],
        candidate_pool: dict[str, Node],
        graph: nx.DiGraph,
        max_nodes: int,
    ) -> list[Node]:
        """Augment selected nodes with supporting context for contradictions, updates, and dependencies."""
        if len(selected_nodes) >= max_nodes:
            return selected_nodes

        coverage_nodes: list[Node] = []
        seen = {node.id for node in selected_nodes}

        for node in selected_nodes:
            if len(selected_nodes) + len(coverage_nodes) >= max_nodes:
                break

            if graph.has_node(node.id):
                # Find supporting edges via MUST_PAIR_RELATIONS
                for _, neighbor, data in graph.edges(node.id, data=True):
                    if neighbor in seen or neighbor not in candidate_pool:
                        continue
                    relationship = data.get("relationship", "relates_to")
                    if relationship in MUST_PAIR_RELATIONS:
                        support_node = candidate_pool[neighbor]
                        if support_node not in coverage_nodes:
                            coverage_nodes.append(support_node)
                            seen.add(neighbor)
                        if len(selected_nodes) + len(coverage_nodes) >= max_nodes:
                            break

                # Find incoming edges (predecessors) with strong relationships
                for predecessor, _, data in graph.in_edges(node.id, data=True):
                    if predecessor in seen or predecessor not in candidate_pool:
                        continue
                    relationship = data.get("relationship", "relates_to")
                    if relationship in MUST_PAIR_RELATIONS:
                        support_node = candidate_pool[predecessor]
                        if support_node not in coverage_nodes:
                            coverage_nodes.append(support_node)
                            seen.add(predecessor)
                        if len(selected_nodes) + len(coverage_nodes) >= max_nodes:
                            break

        return selected_nodes + coverage_nodes[: max_nodes - len(selected_nodes)]

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

    def _fetch_edges_for_nodes(
        self,
        connection: sqlite3.Connection,
        node_ids: list[str],
    ) -> list[Edge]:
        if not node_ids:
            return []
        placeholders = ", ".join("?" for _ in node_ids)
        # Use OR so the graph walk can traverse outward: return any edge
        # whose source OR target is in the current seed set.
        rows = connection.execute(
            f"""
            SELECT id, source_id, target_id, relationship, weight, metadata, created_at, tenant_id
            FROM edges
            WHERE tenant_id = ?
              AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
            ORDER BY created_at ASC
            """,
            (self.tenant_id, *node_ids, *node_ids),
        ).fetchall()
        return [self._row_to_edge(row) for row in rows]

    def _most_connected_node_ids(
        self,
        connection: sqlite3.Connection,
        *,
        limit: int,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT n.id, n.agent_id, n.project, n.session_id, n.label, n.content, n.node_type, n.tags, n.source_prompt, n.metadata,
                   n.evidence_records, n.valid_from, n.valid_to, n.created_at, n.updated_at, n.access_count, n.tenant_id,
                   COUNT(e.id) AS connection_count
            FROM nodes AS n
            LEFT JOIN edges AS e ON (n.id = e.source_id OR n.id = e.target_id) AND e.tenant_id = ?
            WHERE n.tenant_id = ?
            GROUP BY n.id
            ORDER BY connection_count DESC, n.updated_at DESC
            """,
            (self.tenant_id, self.tenant_id),
        ).fetchall()
        selected: list[str] = []
        for row in rows:
            node = self._row_to_node(row)
            if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                continue
            selected.append(str(row["id"]))
            if len(selected) >= limit:
                break
        return selected

    def _find_project_node_ids(
        self,
        connection: sqlite3.Connection,
        *,
        project: str,
        agent_id: str = "",
        session_id: str = "",
        limit: int,
    ) -> list[str]:
        project_lower = project.strip().lower()
        rows = connection.execute(
            """
            SELECT id, agent_id, project, session_id, label, content, node_type, tags, source_prompt, metadata,
                   evidence_records, valid_from, valid_to, created_at, updated_at, access_count, tenant_id
            FROM nodes
            WHERE tenant_id = ?
            ORDER BY updated_at DESC
            """,
            (self.tenant_id,),
        ).fetchall()
        scored: list[tuple[str, float, str]] = []
        for row in rows:
            node = self._row_to_node(row)
            if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                continue

            tags = json.loads(row["tags"] or "[]")
            tag_match = (
                1.0 if any(project_lower in {str(tag).lower(), f"project:{str(tag).lower()}"} for tag in tags) else 0.0
            )
            explicit_match = 1.0 if str(row["project"] or "").strip().lower() == project_lower else 0.0
            lexical = lexical_overlap(project, row["label"], row["content"])
            score = max(explicit_match, tag_match, lexical)
            if score <= 0.0:
                continue
            scored.append((row["id"], score, row["updated_at"]))
        scored.sort(key=lambda item: (-item[1], item[2]), reverse=False)
        return [node_id for node_id, _, _ in scored[:limit]]
