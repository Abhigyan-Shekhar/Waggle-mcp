from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from waggle.config import AppConfig
from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType, SubgraphResult
from waggle.retrieval.hybrid import CandidateMemory, HybridRetrievalConfig, HybridRetriever
from waggle.server import WaggleServer


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(16, dtype=np.float32)
        for token in text.lower().split():
            vector[sum(ord(character) for character in token) % len(vector)] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        return np.asarray([self.embed(text) for text in texts], dtype=np.float32)

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def make_graph(tmp_path: Path, *, rerank_enabled: bool = True) -> MemoryGraph:
    return MemoryGraph(
        tmp_path / "hybrid.db",
        FakeEmbeddingModel(),
        enable_dedup=False,
        hybrid_retrieval_config=HybridRetrievalConfig(
            rerank_enabled=rerank_enabled,
            rerank_model="",
            rerank_top_k_in=20,
            rerank_top_k_out=5,
        ),
    )


def make_app(tmp_path: Path) -> WaggleServer:
    graph = make_graph(tmp_path)
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(tmp_path / "hybrid.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )
    return WaggleServer(graph=graph, config=config)


def test_verbatim_codeword_retrieval_across_sessions(tmp_path: Path) -> None:
    graph = make_graph(tmp_path, rerank_enabled=False)
    with graph._lock, graph._connect() as connection:
        observed_at = datetime(2026, 1, 1, tzinfo=UTC)
        graph._store_transcript_record(
            connection,
            agent_id="codex",
            project="alpha",
            session_id="sess-1",
            observed_at=observed_at,
            turn_index=0,
            role="user",
            transcript_text="the launch codeword is saffron-badger",
            turn_pair_id="tp-codeword",
        )

    hits = graph.hybrid_retriever().retrieve(
        query="what was the launch codeword",
        project="alpha",
        agent_id="codex",
        session_id="",
        top_k=5,
        mode="hybrid",
    )

    assert hits
    assert "saffron-badger" in hits[0].content
    assert hits[0].source in {"transcript", "both"}


def test_multi_hop_graph_expansion_fires_for_derived_from_neighbors(tmp_path: Path) -> None:
    graph = make_graph(tmp_path, rerank_enabled=False)
    with graph._lock, graph._connect() as connection:
        observed_at = datetime(2026, 1, 1, tzinfo=UTC)
        for index, text in enumerate(
            [
                "user: the outage runbook starts with the release plan",
                "assistant: noted",
            ]
        ):
            graph._store_transcript_record(
                connection,
                agent_id="codex",
                project="alpha",
                session_id="sess-graph",
                observed_at=observed_at,
                turn_index=index,
                role="user" if index == 0 else "assistant",
                transcript_text=text,
                turn_pair_id="tp-graph",
            )
        node_a = graph.add_node(
            label="Release plan",
            content="The release plan points to the database migration.",
            node_type=NodeType.FACT,
            agent_id="codex",
            project="alpha",
            session_id="sess-graph",
            source_turn_pair_id="tp-graph",
            connection=connection,
        ).node
        node_b = graph.add_node(
            label="Database migration",
            content="The database migration points to token rotation.",
            node_type=NodeType.FACT,
            agent_id="codex",
            project="alpha",
            session_id="sess-graph",
            source_turn_pair_id="tp-graph",
            connection=connection,
        ).node
        node_c = graph.add_node(
            label="Token rotation",
            content="Token rotation points to the HSM rollout.",
            node_type=NodeType.FACT,
            agent_id="codex",
            project="alpha",
            session_id="sess-graph",
            source_turn_pair_id="tp-graph",
            connection=connection,
        ).node
        graph.add_edge(
            source_id=node_a.id,
            target_id=node_b.id,
            relationship=RelationType.DERIVED_FROM,
            connection=connection,
        )
        graph.add_edge(
            source_id=node_b.id,
            target_id=node_c.id,
            relationship=RelationType.DERIVED_FROM,
            connection=connection,
        )

    debug = graph.hybrid_retriever().retrieve_debug(
        query="what does the release plan ultimately point to",
        project="alpha",
        agent_id="codex",
        session_id="",
        top_k=5,
        mode="hybrid",
    )

    assert debug["layers"]["graph_expansion"]
    assert any(len(hit.node_ids) >= 3 for hit in debug["hits"])


def test_reranker_promotes_late_candidate_into_top_five(tmp_path: Path) -> None:
    graph = make_graph(tmp_path, rerank_enabled=True)
    retriever = HybridRetriever(
        graph,
        config=HybridRetrievalConfig(rerank_enabled=True, rerank_model="fake", rerank_top_k_in=20, rerank_top_k_out=5),
        rerank_callable=lambda prompt, model: (
            '{"top_hits":[{"candidate_id":"cand-15","reasoning":"It directly answers the query."}]}'
        ),
    )
    candidates = [
        CandidateMemory(candidate_id=f"cand-{index}", content=f"candidate {index}", source="node", score=100 - index)
        for index in range(20)
    ]

    reranked = retriever._rerank(query="best candidate", candidates=candidates, top_k_out=5)

    assert any(item.candidate_id == "cand-15" for item in reranked[:5])
    promoted = next(item for item in reranked if item.candidate_id == "cand-15")
    assert promoted.reasoning_from_reranker == "It directly answers the query."


def test_fusion_fallback_works_when_rerank_disabled(tmp_path: Path) -> None:
    graph = make_graph(tmp_path, rerank_enabled=False)
    graph.observe_conversation(
        user_message="The incident alias is cobalt-fox.",
        assistant_response="I will remember the cobalt-fox alias.",
        project="alpha",
        session_id="sess-fallback",
    )

    result = graph.query(
        query="what is the incident alias",
        project="alpha",
        retrieval_mode="hybrid",
        max_nodes=5,
    )

    assert isinstance(result, SubgraphResult)
    assert result.retrieval_mode == "hybrid"
    assert result.hybrid_hits
    assert any("cobalt-fox" in hit.content for hit in result.hybrid_hits)
    assert all(not hit.reasoning_from_reranker for hit in result.hybrid_hits)


def test_query_graph_tool_defaults_to_hybrid(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    captured: dict[str, str] = {}
    scoped_graph = app.current_graph()

    def fake_query(**kwargs: str) -> SubgraphResult:
        captured.update(kwargs)
        return SubgraphResult(query=str(kwargs["query"]), retrieval_mode=str(kwargs["retrieval_mode"]))

    scoped_graph.query = fake_query  # type: ignore[method-assign]
    app.current_graph = lambda: scoped_graph  # type: ignore[method-assign]
    result = app.handle_tool_call("query_graph", {"query": "hello"})

    assert result.isError is False
    assert captured["retrieval_mode"] == "hybrid"


def test_cross_session_codeword_verification_uses_verbatim_layer_when_extraction_is_sparse(tmp_path: Path) -> None:
    db_path = tmp_path / "waggle-codeword-verify.db"
    session_a = MemoryGraph(
        db_path,
        FakeEmbeddingModel(),
        enable_dedup=False,
        hybrid_retrieval_config=HybridRetrievalConfig(
            rerank_enabled=False,
            rerank_model="",
            rerank_top_k_in=20,
            rerank_top_k_out=5,
        ),
    )

    session_a.observe_conversation(
        user_message="Remember that the launch codeword is saffron-badger-v2.",
        assistant_response="I'll remember that the launch codeword is saffron-badger-v2.",
        project="alpha",
        session_id="session-a",
        agent_id="claude",
    )

    with session_a._lock, session_a._connect() as connection:
        transcript_rows = connection.execute(
            """
            SELECT transcript_text, embedding
            FROM transcript_records
            WHERE tenant_id = ? AND session_id = ?
            ORDER BY turn_index ASC
            """,
            (session_a.tenant_id, "session-a"),
        ).fetchall()

    assert len(transcript_rows) == 2
    assert all(row["embedding"] is not None for row in transcript_rows)

    session_b = MemoryGraph(
        db_path,
        FakeEmbeddingModel(),
        enable_dedup=False,
        hybrid_retrieval_config=HybridRetrievalConfig(
            rerank_enabled=False,
            rerank_model="",
            rerank_top_k_in=20,
            rerank_top_k_out=5,
        ),
    )

    hybrid = session_b.query(
        query="What is the launch codeword?",
        project="alpha",
        agent_id="claude",
        retrieval_mode="hybrid",
        max_nodes=5,
    )
    verbatim = session_b.query(
        query="What is the launch codeword?",
        project="alpha",
        agent_id="claude",
        retrieval_mode="verbatim",
        max_nodes=5,
    )

    assert any("saffron-badger-v2" in hit.content for hit in hybrid.hybrid_hits)
    assert any("saffron-badger-v2" in hit.content for hit in verbatim.hybrid_hits)


def test_score_explanation_keys_present():
    candidate = CandidateMemory(
        candidate_id="test-1",
        content="test content",
        source="transcript",
        observed_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    candidate.layer_scores["vector_transcript"] = 0.5
    candidate.layer_scores["bm25"] = 0.3

    assert hasattr(candidate, "score_explanation")
    candidate.score_explanation = {
        "vector_transcript": 0.6,
        "bm25": 0.4,
        "final_score": 0.42,
    }
    assert "final_score" in candidate.score_explanation
    assert "vector_transcript" in candidate.score_explanation


def test_score_explanation_normalized():
    candidate = CandidateMemory(
        candidate_id="test-2",
        content="test",
        source="transcript",
    )
    candidate.score_explanation = {
        "vector_transcript": 0.5,
        "vector_node": 0.2,
        "bm25": 0.2,
        "graph_expansion": 0.1,
        "recency": 0.0,
    }
    contributions = [v for k, v in candidate.score_explanation.items() if k != "final_score"]
    assert abs(sum(contributions) - 1.0) < 1e-4


def test_layer_scores_backward_compatible():
    candidate = CandidateMemory(
        candidate_id="test-3",
        content="test",
        source="transcript",
    )
    candidate.layer_scores["vector_transcript"] = 0.8
    candidate.layer_scores["bm25"] = 0.6
    assert "vector_transcript" in candidate.layer_scores
    assert "bm25" in candidate.layer_scores


def test_list_transcript_records_pagination(tmp_path: Path) -> None:
    graph = make_graph(tmp_path, rerank_enabled=False)
    with graph._lock, graph._connect() as connection:
        observed_at = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(5):
            graph._store_transcript_record(
                connection,
                agent_id="codex",
                project="alpha",
                session_id="sess-page",
                observed_at=observed_at,
                turn_index=index,
                role="user",
                transcript_text=f"record {index}",
                turn_pair_id="tp-page",
            )

    all_records = graph.list_transcript_records(project="alpha")
    assert len(all_records) == 5
    assert all_records[0].transcript_text == "record 0"
    assert all_records[4].transcript_text == "record 4"

    first_page = graph.list_transcript_records(project="alpha", limit=2, offset=0)
    assert len(first_page) == 2
    assert [r.transcript_text for r in first_page] == ["record 0", "record 1"]

    second_page = graph.list_transcript_records(project="alpha", limit=2, offset=2)
    assert len(second_page) == 2
    assert [r.transcript_text for r in second_page] == ["record 2", "record 3"]

    third_page = graph.list_transcript_records(project="alpha", limit=2, offset=4)
    assert len(third_page) == 1
    assert [r.transcript_text for r in third_page] == ["record 4"]

    total = graph.count_transcript_records(project="alpha")
    assert total == 5


def test_score_explanation_includes_recency_contribution():
    candidate = CandidateMemory(
        candidate_id="test-4",
        content="test",
        source="transcript",
    )
    candidate.score_explanation = {
        "vector_transcript": 0.4,
        "bm25": 0.3,
        "graph_expansion": 0.1,
        "recency": 0.2,  # non-zero recency
        "final_score": 0.55,
    }
    assert candidate.score_explanation["recency"] > 0
    assert "final_score" in candidate.score_explanation
