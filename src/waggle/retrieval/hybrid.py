from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from waggle.intelligence import tokenize_text
from waggle.models import HybridHit, RelationType
from waggle.rlm import run_gemini_one_shot, run_groq_one_shot, run_ollama_one_shot

RRF_K = 60.0

HYBRID_RERANKER_PROMPT = """You are reranking memory retrieval candidates for a conversational memory system.

You will receive:
1. A user query.
2. Up to 20 candidate memories.

Candidates may be:
- verbatim transcript snippets
- extracted graph facts
- fused memories that contain both transcript evidence and graph facts

Return STRICT JSON only. No markdown. No commentary.

Rules:
- Pick the {top_k_out} most relevant candidates, in order.
- Favor direct answer-bearing evidence over vague topical overlap.
- Prefer candidates that resolve the exact entities, values, dates, or relationships in the query.
- For multi-hop questions, prefer candidates that collectively connect the needed facts.
- Do not fabricate candidate ids or reasoning.
- Each reasoning string must be exactly one sentence.

JSON schema:
{schema_json}

User query:
{query}

Candidates:
{candidates_json}
"""

HYBRID_RERANKER_SCHEMA = {
    "type": "object",
    "properties": {
        "top_hits": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["candidate_id", "reasoning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["top_hits"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class HybridRetrievalConfig:
    vector_weight: float = 1.0
    bm25_weight: float = 1.0
    graph_weight: float = 1.0
    recency_weight: float = 1.0
    rerank_enabled: bool = False
    rerank_model: str = "claude-3-5-sonnet-latest"
    rerank_top_k_in: int = 20
    rerank_top_k_out: int = 5
    recency_half_life_days: float = 30.0


@dataclass(slots=True)
class TurnPairCandidate:
    turn_pair_id: str
    session_id: str
    project: str
    agent_id: str
    transcript_text: str
    transcript_snippet: str
    observed_at: datetime
    turn_indices: list[int]
    roles: list[str]
    embeddings: list[np.ndarray]


@dataclass(slots=True)
class CandidateMemory:
    candidate_id: str
    content: str
    source: str
    turn_pair_id: str = ""
    node_ids: list[str] = field(default_factory=list)
    transcript_text: str = ""
    observed_at: datetime | None = None
    score: float = 0.0
    layer_scores: dict[str, float] = field(default_factory=dict)
    score_explanation: dict[str, float] = field(default_factory=dict)
    reasoning_from_reranker: str = ""


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


def _age_days(timestamp: datetime | None, *, now: datetime) -> float:
    if timestamp is None:
        return 0.0
    normalized = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
    return max((now - normalized.astimezone(UTC)).total_seconds() / 86400.0, 0.0)


def _recency_decay(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return math.exp(-age_days / half_life_days)


def _cosine(a: np.ndarray, b: np.ndarray, embedding_model: Any) -> float:
    return max(float(embedding_model.cosine_similarity(a, b)), 0.0)


def _safe_float(value: float) -> float:
    return float(max(0.0, value))


class SimpleBM25:
    def __init__(self, documents: dict[str, list[str]]) -> None:
        self.documents = documents
        self.doc_count = len(documents)
        self.avgdl = sum(len(tokens) for tokens in documents.values()) / self.doc_count if self.doc_count else 0.0
        self.doc_freq: Counter[str] = Counter()
        self.term_freqs: dict[str, Counter[str]] = {}
        for doc_id, tokens in documents.items():
            frequencies = Counter(tokens)
            self.term_freqs[doc_id] = frequencies
            for term in frequencies:
                self.doc_freq[term] += 1

    def score(self, query: str, *, k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
        query_terms = list(tokenize_text(query))
        if not query_terms or not self.documents:
            return {}
        scores: dict[str, float] = {}
        for doc_id, tokens in self.documents.items():
            dl = max(len(tokens), 1)
            total = 0.0
            freqs = self.term_freqs[doc_id]
            for term in query_terms:
                tf = freqs.get(term, 0)
                if tf <= 0:
                    continue
                df = self.doc_freq.get(term, 0)
                idf = math.log(1.0 + ((self.doc_count - df + 0.5) / (df + 0.5)))
                denom = tf + k1 * (1.0 - b + b * (dl / max(self.avgdl, 1.0)))
                total += idf * ((tf * (k1 + 1.0)) / max(denom, 1e-9))
            if total > 0.0:
                scores[doc_id] = total
        return scores


class HybridRetriever:
    def __init__(
        self,
        graph: Any,
        *,
        config: HybridRetrievalConfig | None = None,
        rerank_callable: Callable[[str, str], str] | None = None,
    ) -> None:
        self.graph = graph
        self.config = config or HybridRetrievalConfig(
            recency_half_life_days=getattr(graph, "recency_half_life_days", 30.0)
        )
        self.rerank_callable = rerank_callable

    def retrieve(
        self,
        query: str,
        project: str,
        agent_id: str,
        session_id: str,
        top_k: int = 5,
        *,
        mode: str = "hybrid",
    ) -> list[HybridHit]:
        payload = self.retrieve_debug(
            query=query,
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            top_k=top_k,
            mode=mode,
        )
        return payload["hits"]

    def retrieve_debug(
        self,
        query: str,
        project: str,
        agent_id: str,
        session_id: str,
        top_k: int = 5,
        *,
        mode: str = "hybrid",
    ) -> dict[str, Any]:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"hybrid", "verbatim"}:
            raise ValueError("HybridRetriever mode must be 'hybrid' or 'verbatim'.")

        turn_pairs = self._load_turn_pairs(project=project, agent_id=agent_id, session_id=session_id)
        query_embedding = self.graph.embedding_model.embed(self.graph._expand_query_aliases(query))
        now = datetime.now(UTC)

        transcript_vector_ranked = self._rank_turn_pairs(query_embedding, turn_pairs)[:20]
        node_vector_ranked = (
            []
            if normalized_mode == "verbatim"
            else self._rank_nodes(query_embedding, project=project, agent_id=agent_id, session_id=session_id)[:20]
        )
        lexical_ranked = self._rank_lexical(
            query=query,
            turn_pairs=turn_pairs,
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            include_nodes=normalized_mode != "verbatim",
        )[:20]
        graph_expanded_ranked = (
            []
            if normalized_mode == "verbatim"
            else self._expand_graph_candidates(
                node_vector_ranked, turn_pairs_by_id={pair.turn_pair_id: pair for pair in turn_pairs}
            )[:20]
        )

        unified = self._fuse_candidates(
            transcript_vector_ranked=transcript_vector_ranked,
            node_vector_ranked=node_vector_ranked,
            lexical_ranked=lexical_ranked,
            graph_expanded_ranked=graph_expanded_ranked,
            now=now,
        )
        fused_top = sorted(unified.values(), key=lambda item: item.score, reverse=True)[: self.config.rerank_top_k_in]

        reranked = self._rerank(query=query, candidates=fused_top, top_k_out=min(top_k, self.config.rerank_top_k_out))

        hits = [
            HybridHit(
                content=item.content,
                score=item.score,
                source=item.source,
                turn_pair_id=item.turn_pair_id,
                node_ids=item.node_ids,
                reasoning_from_reranker=item.reasoning_from_reranker,
                observed_at=item.observed_at,
                layer_scores=item.layer_scores,
                score_explanation=item.score_explanation,
            )
            for item in reranked
        ]
        return {
            "hits": hits,
            "layers": {
                "vector_transcript": self._summarize_layer(transcript_vector_ranked),
                "vector_node": self._summarize_layer(node_vector_ranked),
                "lexical": self._summarize_layer(lexical_ranked),
                "graph_expansion": self._summarize_layer(graph_expanded_ranked),
            },
            "fused_top20": [
                {
                    "candidate_id": item.candidate_id,
                    "source": item.source,
                    "turn_pair_id": item.turn_pair_id,
                    "node_ids": item.node_ids,
                    "score": item.score,
                    "layer_scores": item.layer_scores,
                }
                for item in fused_top
            ],
        }

    def _load_turn_pairs(self, *, project: str, agent_id: str, session_id: str) -> list[TurnPairCandidate]:
        with self.graph._lock.read(), self.graph._pool.checkout() as connection:
            filters = ["tenant_id = ?"]
            params: list[Any] = [self.graph.tenant_id]
            if project.strip():
                filters.append("project = ?")
                params.append(project.strip())
            if session_id.strip():
                filters.append("session_id = ?")
                params.append(session_id.strip())
            elif agent_id.strip():
                filters.append("agent_id = ?")
                params.append(agent_id.strip())
            rows = connection.execute(
                f"""
                SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role,
                       transcript_text, embedding, embedding_model_id, embedding_dim, content_hash, turn_pair_id, metadata
                FROM transcript_records
                WHERE {" AND ".join(filters)}
                ORDER BY observed_at DESC, turn_index DESC
                """,
                tuple(params),
            ).fetchall()

        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            record = self.graph._row_to_transcript_record(row)
            embedding = self.graph._decode_embedding(row["embedding"])
            turn_pair_id = str(record.turn_pair_id or "").strip()
            if not turn_pair_id:
                turn_pair_id = f"{record.session_id or 'session'}:{record.turn_index}"
            # Keep the row even if its embedding failed the checksum: its text
            # still belongs in the lexical (BM25) layer. The corrupt embedding is
            # excluded from the semantic side below.
            grouped[turn_pair_id].append((record, embedding))

        pairs: list[TurnPairCandidate] = []
        for turn_pair_id, items in grouped.items():
            ordered = sorted(items, key=lambda item: item[0].turn_index)
            transcript_text = "\n".join(f"{item[0].role}: {item[0].transcript_text}" for item in ordered)
            record0 = ordered[0][0]
            pairs.append(
                TurnPairCandidate(
                    turn_pair_id=turn_pair_id,
                    session_id=record0.session_id,
                    project=record0.project,
                    agent_id=record0.agent_id,
                    transcript_text=transcript_text,
                    transcript_snippet=transcript_text[:280],
                    observed_at=record0.observed_at,
                    turn_indices=[item[0].turn_index for item in ordered],
                    roles=[item[0].role for item in ordered],
                    embeddings=[item[1] for item in ordered if item[1] is not None],
                )
            )
        return pairs

    def _rank_turn_pairs(
        self, query_embedding: np.ndarray, turn_pairs: list[TurnPairCandidate]
    ) -> list[CandidateMemory]:
        ranked: list[CandidateMemory] = []
        for pair in turn_pairs:
            if not pair.embeddings:
                continue
            semantic = max(
                _cosine(query_embedding, embedding, self.graph.embedding_model) for embedding in pair.embeddings
            )
            ranked.append(
                CandidateMemory(
                    candidate_id=f"tp:{pair.turn_pair_id}",
                    content=pair.transcript_text,
                    source="transcript",
                    turn_pair_id=pair.turn_pair_id,
                    transcript_text=pair.transcript_text,
                    observed_at=pair.observed_at,
                    layer_scores={"vector_transcript": semantic},
                )
            )
        ranked.sort(key=lambda item: item.layer_scores["vector_transcript"], reverse=True)
        return ranked

    def _rank_nodes(
        self, query_embedding: np.ndarray, *, project: str, agent_id: str, session_id: str
    ) -> list[CandidateMemory]:
        with self.graph._lock.read(), self.graph._pool.checkout() as connection:
            filters = ["tenant_id = ?", "embedding IS NOT NULL"]
            params: list[Any] = [self.graph.tenant_id]
            if project.strip():
                filters.append("project = ?")
                params.append(project.strip())
            if session_id.strip():
                filters.append("session_id = ?")
                params.append(session_id.strip())
            elif agent_id.strip():
                filters.append("agent_id = ?")
                params.append(agent_id.strip())
            rows = connection.execute(
                f"""
                SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, source_prompt,
                       source_turn_pair_id, metadata, evidence_records, valid_from, valid_to, created_at, updated_at,
                       access_count, embedding, tenant_id, embedding_model_id, embedding_dim
                FROM nodes
                WHERE {" AND ".join(filters)}
                """,
                tuple(params),
            ).fetchall()
        ranked: list[CandidateMemory] = []
        for row in rows:
            node = self.graph._row_to_node(row)
            embedding = self.graph._decode_embedding(row["embedding"])
            if embedding is None:
                continue
            semantic = _cosine(query_embedding, embedding, self.graph.embedding_model)
            ranked.append(
                CandidateMemory(
                    candidate_id=f"node:{node.id}",
                    content=f"{node.label}: {node.content}",
                    source="node",
                    turn_pair_id=node.source_turn_pair_id,
                    node_ids=[node.id],
                    observed_at=node.updated_at,
                    layer_scores={"vector_node": semantic},
                )
            )
        ranked.sort(key=lambda item: item.layer_scores["vector_node"], reverse=True)
        return ranked

    def _rank_lexical(
        self,
        *,
        query: str,
        turn_pairs: list[TurnPairCandidate],
        project: str,
        agent_id: str,
        session_id: str,
        include_nodes: bool,
    ) -> list[CandidateMemory]:
        documents: dict[str, list[str]] = {}
        payloads: dict[str, CandidateMemory] = {}
        for pair in turn_pairs:
            doc_id = f"tp:{pair.turn_pair_id}"
            documents[doc_id] = list(tokenize_text(pair.transcript_text))
            payloads[doc_id] = CandidateMemory(
                candidate_id=doc_id,
                content=pair.transcript_text,
                source="transcript",
                turn_pair_id=pair.turn_pair_id,
                transcript_text=pair.transcript_text,
                observed_at=pair.observed_at,
            )
        if include_nodes:
            with self.graph._lock.read(), self.graph._pool.checkout() as connection:
                filters = ["tenant_id = ?"]
                params: list[Any] = [self.graph.tenant_id]
                if project.strip():
                    filters.append("project = ?")
                    params.append(project.strip())
                if session_id.strip():
                    filters.append("session_id = ?")
                    params.append(session_id.strip())
                elif agent_id.strip():
                    filters.append("agent_id = ?")
                    params.append(agent_id.strip())
                rows = connection.execute(
                    f"""
                    SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, source_prompt,
                           source_turn_pair_id, metadata, evidence_records, valid_from, valid_to, created_at, updated_at,
                           access_count, tenant_id, embedding_model_id, embedding_dim
                    FROM nodes
                    WHERE {" AND ".join(filters)}
                    """,
                    tuple(params),
                ).fetchall()
            for row in rows:
                node = self.graph._row_to_node(row)
                doc_id = f"node:{node.id}"
                documents[doc_id] = list(tokenize_text(f"{node.label} {node.content}"))
                payloads[doc_id] = CandidateMemory(
                    candidate_id=doc_id,
                    content=f"{node.label}: {node.content}",
                    source="node",
                    turn_pair_id=node.source_turn_pair_id,
                    node_ids=[node.id],
                    observed_at=node.updated_at,
                )
        bm25 = SimpleBM25(documents)
        scores = bm25.score(query)
        ranked: list[CandidateMemory] = []
        for doc_id, score in scores.items():
            candidate = payloads[doc_id]
            candidate.layer_scores = {"bm25": score}
            ranked.append(candidate)
        ranked.sort(key=lambda item: item.layer_scores["bm25"], reverse=True)
        return ranked

    def _expand_graph_candidates(
        self,
        ranked_nodes: list[CandidateMemory],
        *,
        turn_pairs_by_id: dict[str, TurnPairCandidate],
    ) -> list[CandidateMemory]:
        if not ranked_nodes:
            return []
        seed_node_ids = [candidate.node_ids[0] for candidate in ranked_nodes if candidate.node_ids]
        if not seed_node_ids:
            return []
        with self.graph._lock.read(), self.graph._pool.checkout() as connection:
            edge_rows = connection.execute(
                """
                SELECT id, tenant_id, source_id, target_id, relationship, weight, metadata, created_at
                FROM edges
                WHERE tenant_id = ? AND relationship = ?
                """,
                (self.graph.tenant_id, RelationType.DERIVED_FROM.value),
            ).fetchall()
        adjacency: dict[str, set[str]] = defaultdict(set)
        for row in edge_rows:
            edge = self.graph._row_to_edge(row)
            adjacency[edge.source_id].add(edge.target_id)
            adjacency[edge.target_id].add(edge.source_id)

        ranked: list[CandidateMemory] = []
        seed_rank_map = {
            candidate.node_ids[0]: index for index, candidate in enumerate(ranked_nodes, start=1) if candidate.node_ids
        }
        for seed_id in seed_node_ids:
            visited = {seed_id}
            frontier = {seed_id}
            for _depth in range(2):
                next_frontier: set[str] = set()
                for current in frontier:
                    for neighbor in adjacency.get(current, set()):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            next_frontier.add(neighbor)
                frontier = next_frontier
                if not frontier:
                    break
            if len(visited) <= 1:
                continue
            with self.graph._lock.read(), self.graph._pool.checkout() as connection:
                node_rows = connection.execute(
                    f"""
                    SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, source_prompt,
                           source_turn_pair_id, metadata, evidence_records, valid_from, valid_to, created_at, updated_at,
                           access_count, tenant_id, embedding_model_id, embedding_dim
                    FROM nodes
                    WHERE tenant_id = ? AND id IN ({", ".join("?" for _ in visited)})
                    """,
                    (self.graph.tenant_id, *visited),
                ).fetchall()
            nodes_by_id = {row["id"]: self.graph._row_to_node(row) for row in node_rows}
            visited_nodes = [nodes_by_id[node_id] for node_id in visited if node_id in nodes_by_id]
            turn_pair_id = next((node.source_turn_pair_id for node in visited_nodes if node.source_turn_pair_id), "")
            transcript_text = turn_pairs_by_id[turn_pair_id].transcript_text if turn_pair_id in turn_pairs_by_id else ""
            content_parts = [f"{node.label}: {node.content}" for node in visited_nodes]
            if transcript_text:
                content_parts.insert(0, transcript_text)
            ranked.append(
                CandidateMemory(
                    candidate_id=f"graph:{seed_id}",
                    content="\n".join(content_parts),
                    source="both" if transcript_text else "node",
                    turn_pair_id=turn_pair_id,
                    node_ids=sorted(node.id for node in visited_nodes),
                    transcript_text=transcript_text,
                    observed_at=max((node.updated_at for node in visited_nodes), default=None),
                    layer_scores={"graph_expansion": _rrf(seed_rank_map.get(seed_id, len(seed_rank_map) + 1))},
                )
            )
        ranked.sort(key=lambda item: item.layer_scores["graph_expansion"], reverse=True)
        return ranked

    def _fuse_candidates(
        self,
        *,
        transcript_vector_ranked: list[CandidateMemory],
        node_vector_ranked: list[CandidateMemory],
        lexical_ranked: list[CandidateMemory],
        graph_expanded_ranked: list[CandidateMemory],
        now: datetime,
    ) -> dict[str, CandidateMemory]:
        combined: dict[str, CandidateMemory] = {}

        def key_for(candidate: CandidateMemory) -> str:
            if candidate.turn_pair_id:
                return f"tp:{candidate.turn_pair_id}"
            if candidate.node_ids:
                return f"node:{candidate.node_ids[0]}"
            return candidate.candidate_id

        def merge(candidate: CandidateMemory) -> CandidateMemory:
            key = key_for(candidate)
            existing = combined.get(key)
            if existing is None:
                existing = CandidateMemory(
                    candidate_id=key,
                    content=candidate.content,
                    source=candidate.source,
                    turn_pair_id=candidate.turn_pair_id,
                    node_ids=list(candidate.node_ids),
                    transcript_text=candidate.transcript_text,
                    observed_at=candidate.observed_at,
                )
                combined[key] = existing
            else:
                if candidate.transcript_text and not existing.transcript_text:
                    existing.transcript_text = candidate.transcript_text
                if candidate.content and candidate.source == "both":
                    existing.content = candidate.content
                existing.node_ids = sorted(set(existing.node_ids + candidate.node_ids))
                if existing.source != candidate.source:
                    existing.source = "both" if {existing.source, candidate.source} != {"node"} else "node"
                if existing.observed_at is None or (
                    candidate.observed_at is not None and candidate.observed_at > existing.observed_at
                ):
                    existing.observed_at = candidate.observed_at
            return existing

        for index, candidate in enumerate(transcript_vector_ranked, start=1):
            merged = merge(candidate)
            merged.layer_scores["vector_transcript"] = _rrf(index)
        for index, candidate in enumerate(node_vector_ranked, start=1):
            merged = merge(candidate)
            merged.layer_scores["vector_node"] = _rrf(index)
        for index, candidate in enumerate(lexical_ranked, start=1):
            merged = merge(candidate)
            merged.layer_scores["bm25"] = _rrf(index)
        for index, candidate in enumerate(graph_expanded_ranked, start=1):
            merged = merge(candidate)
            merged.layer_scores["graph_expansion"] = _rrf(index)

        for item in combined.values():
            age_days = _age_days(item.observed_at, now=now)
            decay = _recency_decay(age_days, self.config.recency_half_life_days)
            item.layer_scores["recency"] = decay
            fused = (
                self.config.vector_weight
                * (item.layer_scores.get("vector_transcript", 0.0) + item.layer_scores.get("vector_node", 0.0))
                + self.config.bm25_weight * item.layer_scores.get("bm25", 0.0)
                + self.config.graph_weight * item.layer_scores.get("graph_expansion", 0.0)
            )
            recency_multiplier = (1.0 - self.config.recency_weight) + (self.config.recency_weight * decay)
            item.score = fused * recency_multiplier
            raw = {
                "vector_transcript": self.config.vector_weight
                * item.layer_scores.get("vector_transcript", 0.0)
                * recency_multiplier,
                "vector_node": self.config.vector_weight
                * item.layer_scores.get("vector_node", 0.0)
                * recency_multiplier,
                "bm25": self.config.bm25_weight * item.layer_scores.get("bm25", 0.0) * recency_multiplier,
                "graph_expansion": self.config.graph_weight
                * item.layer_scores.get("graph_expansion", 0.0)
                * recency_multiplier,
            }
            total = sum(raw.values()) or 1.0
            item.score_explanation = {k: round(v / total, 4) for k, v in raw.items()}
            item.score_explanation["recency_multiplier"] = round(recency_multiplier, 4)
            item.score_explanation["final_score"] = round(item.score, 4)
            if item.source == "node" and item.turn_pair_id and item.transcript_text:
                item.source = "both"
                item.content = f"{item.transcript_text}\n{item.content}"
        return combined

    def _rerank(self, *, query: str, candidates: list[CandidateMemory], top_k_out: int) -> list[CandidateMemory]:
        if not candidates:
            return []
        if not self.config.rerank_enabled:
            return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k_out]

        raw = self._invoke_reranker(query=query, candidates=candidates, top_k_out=top_k_out)
        if raw is None:
            return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k_out]
        try:
            payload = self._parse_reranker_json(raw)
        except Exception:
            return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k_out]

        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        selected: list[CandidateMemory] = []
        seen: set[str] = set()
        for item in payload.get("top_hits", [])[:top_k_out]:
            candidate_id = str(item.get("candidate_id", "")).strip()
            if not candidate_id or candidate_id in seen or candidate_id not in by_id:
                continue
            candidate = by_id[candidate_id]
            candidate.reasoning_from_reranker = str(item.get("reasoning", "")).strip()
            candidate.score += 1.0 / (len(selected) + 1)
            selected.append(candidate)
            seen.add(candidate_id)
        if not selected:
            return sorted(candidates, key=lambda item: item.score, reverse=True)[:top_k_out]
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            if len(selected) >= top_k_out:
                break
            if candidate.candidate_id in seen:
                continue
            selected.append(candidate)
            seen.add(candidate.candidate_id)
        return selected[:top_k_out]

    def _invoke_reranker(self, *, query: str, candidates: list[CandidateMemory], top_k_out: int) -> str | None:
        rendered_candidates = [
            {
                "candidate_id": candidate.candidate_id,
                "source": candidate.source,
                "turn_pair_id": candidate.turn_pair_id,
                "node_ids": candidate.node_ids,
                "content": candidate.content,
                "layer_scores": {key: round(float(value), 6) for key, value in candidate.layer_scores.items()},
            }
            for candidate in candidates[: self.config.rerank_top_k_in]
        ]
        prompt = HYBRID_RERANKER_PROMPT.format(
            top_k_out=top_k_out,
            schema_json=json.dumps(HYBRID_RERANKER_SCHEMA),
            query=query,
            candidates_json=json.dumps(rendered_candidates, ensure_ascii=True),
        )
        if self.rerank_callable is not None:
            return self.rerank_callable(prompt, self.config.rerank_model)

        model = self.config.rerank_model.strip()
        if not model:
            return None
        try:
            if model.startswith("gemini") and os.environ.get("GEMINI_API_KEY"):
                return run_gemini_one_shot(
                    prompt=prompt,
                    api_key=os.environ["GEMINI_API_KEY"],
                    model_name=model,
                    timeout_seconds=30.0,
                )
            if os.environ.get("GROQ_API_KEY"):
                return run_groq_one_shot(
                    prompt=prompt,
                    api_key=os.environ["GROQ_API_KEY"],
                    model_name=model,
                    max_tokens=600,
                    timeout_seconds=30.0,
                )
            if model.startswith("ollama:"):
                return run_ollama_one_shot(
                    prompt=prompt,
                    model_name=model.split(":", 1)[1],
                    timeout_seconds=30.0,
                )
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_reranker_json(raw: str) -> dict[str, Any]:
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if "\n" in stripped:
                stripped = stripped.split("\n", 1)[1]
            if stripped.endswith("```"):
                stripped = stripped[:-3]
        return json.loads(stripped)

    @staticmethod
    def _summarize_layer(candidates: list[CandidateMemory]) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate.candidate_id,
                "source": candidate.source,
                "turn_pair_id": candidate.turn_pair_id,
                "node_ids": candidate.node_ids,
                "score": max(candidate.layer_scores.values()) if candidate.layer_scores else 0.0,
            }
            for candidate in candidates[:20]
        ]
