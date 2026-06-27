from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import MagicMock

import numpy as np
import pytest

from waggle.neo4j_graph import Neo4jMemoryGraph


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        del text
        return np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    def cosine_similarity(self, left: np.ndarray, right: np.ndarray) -> float:
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        if denominator == 0:
            return 0.0
        return float(np.dot(left, right) / denominator)


def make_mock_graph(
    records: list[dict[str, object]],
) -> tuple[Neo4jMemoryGraph, MagicMock]:
    graph = object.__new__(Neo4jMemoryGraph)
    graph.tenant_id = "tenant-a"
    graph.embedding_model = FakeEmbeddingModel()

    graph._lock = MagicMock()
    graph._lock.__enter__ = MagicMock(return_value=None)
    graph._lock.__exit__ = MagicMock(return_value=None)

    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=None)
    session.run.return_value = records

    graph._session = MagicMock(return_value=session)

    return graph, session


def transcript_record(
    *,
    record_id: str,
    observed_at: datetime,
    turn_index: int,
    agent_id: str = "agent-a",
    project: str = "project-a",
    session_id: str = "session-a",
    role: str = "user",
    text: str = "deployment completed",
    embedding: list[float] | None = None,
) -> dict[str, object]:
    return {
        "t": {
            "id": record_id,
            "tenant_id": "tenant-a",
            "agent_id": agent_id,
            "project": project,
            "session_id": session_id,
            "observed_at": observed_at.isoformat(),
            "turn_index": turn_index,
            "role": role,
            "transcript_text": text,
            "embedding": (embedding if embedding is not None else [1.0, 0.0, 0.0, 0.0]),
            "metadata": "{}",
        }
    }


def test_verbatim_query_returns_real_neo4j_replay_hits() -> None:
    older = transcript_record(
        record_id="older",
        observed_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        turn_index=1,
    )
    newer = transcript_record(
        record_id="newer",
        observed_at=datetime(2026, 6, 2, 10, 0, tzinfo=UTC),
        turn_index=2,
    )
    graph, _ = make_mock_graph([newer, older])

    result = graph.query(
        query="latest deployment",
        retrieval_mode="verbatim",
        max_nodes=2,
        project="project-a",
        session_id="session-a",
    )

    assert result.retrieval_mode == "verbatim"
    assert [hit.turn_index for hit in result.replay_hits] == [2, 1]
    assert all(hit.session_id == "session-a" for hit in result.replay_hits)


def test_neo4j_replay_query_pushes_scope_filters_into_cypher() -> None:
    graph, session = make_mock_graph([])

    result = graph.query(
        query="deployment",
        retrieval_mode="verbatim",
        max_nodes=5,
        agent_id="ignored-agent",
        project="project-a",
        session_id="session-a",
    )

    assert result.replay_hits == []

    cypher = session.run.call_args.args[0]
    params = session.run.call_args.kwargs

    assert "t.tenant_id = $tenant_id" in cypher
    assert "t.embedding IS NOT NULL" in cypher
    assert "t.project = $project" in cypher
    assert "t.session_id = $session_id" in cypher
    assert "t.agent_id = $agent_id" not in cypher
    assert "ORDER BY t.observed_at" not in cypher
    assert "LIMIT $fetch_limit" not in cypher

    assert params["tenant_id"] == "tenant-a"
    assert params["project"] == "project-a"
    assert params["session_id"] == "session-a"
    assert "fetch_limit" not in params
    assert "agent_id" not in params


def test_neo4j_replay_ranks_all_scoped_transcripts_before_limiting() -> None:
    recent_irrelevant = [
        transcript_record(
            record_id=f"recent-{index}",
            observed_at=datetime(
                2026,
                6,
                18,
                10,
                0,
                tzinfo=UTC,
            ),
            turn_index=100 + index,
            text="unrelated conversation",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        for index in range(500)
    ]

    older_relevant = transcript_record(
        record_id="older-relevant",
        observed_at=datetime(
            2025,
            1,
            1,
            10,
            0,
            tzinfo=UTC,
        ),
        turn_index=1,
        text="deployment completed",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )

    all_records = [*recent_irrelevant, older_relevant]
    graph, session = make_mock_graph(all_records)

    def run_replay_query(
        cypher: str,
        **params: object,
    ) -> list[dict[str, object]]:
        # Reproduce the old bug when the query contains its recency limit:
        # the older relevant transcript is removed before semantic ranking.
        if "LIMIT $fetch_limit" in cypher:
            fetch_limit = int(params["fetch_limit"])
            return recent_irrelevant[:fetch_limit]
        return all_records

    session.run.side_effect = run_replay_query

    result = graph.query(
        query="deployment",
        retrieval_mode="verbatim",
        max_nodes=1,
        project="project-a",
        session_id="session-a",
    )

    cypher = session.run.call_args.args[0]
    params = session.run.call_args.kwargs

    assert "ORDER BY t.observed_at" not in cypher
    assert "LIMIT $fetch_limit" not in cypher
    assert "fetch_limit" not in params

    assert len(result.replay_hits) == 1
    assert result.replay_hits[0].turn_index == 1
    assert result.replay_hits[0].transcript_text == "deployment completed"


def test_neo4j_replay_query_uses_agent_when_session_is_absent() -> None:
    graph, session = make_mock_graph([])

    graph.query(
        query="deployment",
        retrieval_mode="verbatim",
        agent_id="agent-a",
    )

    cypher = session.run.call_args.args[0]
    params = session.run.call_args.kwargs

    assert "t.agent_id = $agent_id" in cypher
    assert "t.session_id = $session_id" not in cypher
    assert params["agent_id"] == "agent-a"


def test_neo4j_replay_results_respect_requested_limit() -> None:
    records = [
        transcript_record(
            record_id=f"record-{index}",
            observed_at=datetime(2026, 6, index + 1, 10, 0, tzinfo=UTC),
            turn_index=index,
        )
        for index in range(4)
    ]
    graph, _ = make_mock_graph(list(reversed(records)))

    result = graph.query(
        query="latest deployment",
        retrieval_mode="verbatim",
        max_nodes=2,
    )

    assert len(result.replay_hits) == 2
    assert [hit.turn_index for hit in result.replay_hits] == [3, 2]


def test_neo4j_replay_scope_matching_uses_sqlite_case_semantics() -> None:
    graph, _ = make_mock_graph(
        [
            transcript_record(
                record_id="case-mismatch",
                observed_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
                turn_index=1,
                project="Project-A",
            )
        ]
    )

    result = graph.query(
        query="deployment",
        retrieval_mode="verbatim",
        project="project-a",
    )

    assert result.replay_hits == []


def test_neo4j_temporal_validity_controls_are_explicitly_unsupported() -> None:
    signature = inspect.signature(Neo4jMemoryGraph.query)

    assert "as_of" not in signature.parameters
    assert "include_invalidated" not in signature.parameters


@pytest.mark.parametrize(
    "method_name",
    [
        "list_transcript_records",
        "search_transcript_records",
        "count_transcript_records",
    ],
)
def test_direct_transcript_collection_apis_are_explicitly_unsupported(
    method_name: str,
) -> None:
    assert not hasattr(Neo4jMemoryGraph, method_name)
