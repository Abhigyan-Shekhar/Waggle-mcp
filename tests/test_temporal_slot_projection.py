from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from waggle.graph import MemoryGraph
from waggle.models import FactKind, NodeType, NormalizedClaim, RelationType


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:temporal-slots-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(16, dtype=np.float32)
        for token in text.lower().split():
            vector[sum(ord(char) for char in token) % len(vector)] += 1.0
        norm = np.linalg.norm(vector)
        return vector if norm == 0.0 else vector / norm

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        return np.stack([self.embed(text) for text in texts])

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, left: np.ndarray, right: np.ndarray) -> float:
        left_norm = np.linalg.norm(left)
        right_norm = np.linalg.norm(right)
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return float(np.dot(left, right) / (left_norm * right_norm))


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel(), enable_dedup=False)


def state_claim(*, value: str, effective_at: datetime, confidence: float = 0.98) -> NormalizedClaim:
    return NormalizedClaim(
        subject_key="user",
        relation_key="postcard_count",
        value_normalized=value,
        fact_kind=FactKind.STATE_SNAPSHOT,
        scope_key="collection",
        effective_at=effective_at,
        observed_at=effective_at,
        confidence=confidence,
        source_span=f"I now have {value} postcards.",
    )


def test_newer_state_becomes_head_and_closes_previous_version(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    old_time = datetime(2026, 1, 1, tzinfo=UTC)
    new_time = old_time + timedelta(days=10)

    old = graph.resolve_claim(
        claim=state_claim(value="17", effective_at=old_time),
        label="Postcard count",
        content="I have 17 postcards.",
    ).node
    new = graph.resolve_claim(
        claim=state_claim(value="25", effective_at=new_time),
        label="Postcard count",
        content="I now have 25 postcards.",
    ).node

    refreshed_old = graph.get_node(old.id)
    assert refreshed_old.valid_to == new_time
    assert refreshed_old.metadata["superseded_by"] == new.id

    with graph._lock, graph._connect() as connection:
        head = connection.execute(
            "SELECT current_node_id FROM fact_heads WHERE tenant_id = ? AND relation_key = ?",
            (graph.tenant_id, "postcard_count"),
        ).fetchone()
        edge = connection.execute(
            "SELECT relationship FROM edges WHERE source_id = ? AND target_id = ?",
            (new.id, old.id),
        ).fetchone()
    assert head["current_node_id"] == new.id
    assert edge["relationship"] == RelationType.UPDATES.value
    assert graph.get_state_fact(
        subject_key="user",
        relation_key="postcard_count",
        scope_key="collection",
    ).id == new.id
    assert graph.get_state_fact(
        subject_key="user",
        relation_key="postcard_count",
        scope_key="collection",
        as_of=old_time + timedelta(days=1),
    ).id == old.id

    current = graph.query(query="postcard count", max_nodes=10, max_depth=0)
    assert new.id in {node.id for node in current.nodes}
    assert old.id not in {node.id for node in current.nodes}

    historical = graph.query(
        query="postcard count",
        max_nodes=10,
        max_depth=0,
        as_of=old_time + timedelta(days=1),
    )
    assert old.id in {node.id for node in historical.nodes}
    assert new.id not in {node.id for node in historical.nodes}


def test_backfilled_state_does_not_replace_newer_head(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    current_time = datetime(2026, 2, 1, tzinfo=UTC)
    historical_time = current_time - timedelta(days=30)
    current = graph.resolve_claim(
        claim=state_claim(value="25", effective_at=current_time),
        label="Postcard count",
        content="I have 25 postcards.",
    ).node
    historical = graph.resolve_claim(
        claim=state_claim(value="17", effective_at=historical_time),
        label="Postcard count",
        content="I had 17 postcards last month.",
    ).node

    assert graph.get_node(historical.id).valid_to == current_time
    with graph._lock, graph._connect() as connection:
        head = connection.execute(
            "SELECT current_node_id FROM fact_heads WHERE tenant_id = ? AND relation_key = ?",
            (graph.tenant_id, "postcard_count"),
        ).fetchone()
    assert head["current_node_id"] == current.id


def test_events_are_append_only_even_with_same_relation(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    first_time = datetime(2026, 3, 1, tzinfo=UTC)
    second_time = first_time + timedelta(days=7)

    def event_claim(value: str, effective_at: datetime) -> NormalizedClaim:
        return NormalizedClaim(
            subject_key="user",
            relation_key="feed_purchase",
            value_normalized=value,
            fact_kind=FactKind.EVENT,
            scope_key="farm",
            effective_at=effective_at,
            observed_at=effective_at,
            confidence=0.99,
        )

    first = graph.resolve_claim(
        claim=event_claim("50 lb", first_time),
        label="Feed purchase",
        content="I bought 50 pounds of feed.",
    ).node
    second = graph.resolve_claim(
        claim=event_claim("20 lb", second_time),
        label="Feed purchase",
        content="I later bought 20 pounds of feed.",
    ).node

    assert graph.get_node(first.id).valid_to is None
    assert graph.get_node(second.id).valid_to is None
    with graph._lock, graph._connect() as connection:
        head_count = connection.execute(
            "SELECT COUNT(*) FROM fact_heads WHERE tenant_id = ? AND relation_key = ?",
            (graph.tenant_id, "feed_purchase"),
        ).fetchone()[0]
    assert head_count == 0


def test_low_confidence_state_defaults_to_preservation(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    first_time = datetime(2026, 4, 1, tzinfo=UTC)
    second_time = first_time + timedelta(days=1)
    first = graph.resolve_claim(
        claim=state_claim(value="17", effective_at=first_time, confidence=0.6),
        label="Postcard count",
        content="I may have 17 postcards.",
    ).node
    second = graph.resolve_claim(
        claim=state_claim(value="25", effective_at=second_time, confidence=0.6),
        label="Postcard count",
        content="I may have 25 postcards.",
    ).node

    assert graph.get_node(first.id).valid_to is None
    assert graph.get_node(second.id).valid_to is None
    with graph._lock, graph._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM fact_heads").fetchone()[0] == 0


def test_hybrid_node_candidates_exclude_superseded_state(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    old_time = datetime(2026, 5, 1, tzinfo=UTC)
    new_time = old_time + timedelta(days=1)
    graph.resolve_claim(
        claim=state_claim(value="17", effective_at=old_time),
        label="Postcard count",
        content="The user's postcard count is 17.",
    )
    graph.resolve_claim(
        claim=state_claim(value="25", effective_at=new_time),
        label="Postcard count",
        content="The user's postcard count is 25.",
    )

    hits = graph.hybrid_retriever().retrieve(
        query="current postcard count",
        project="",
        agent_id="",
        session_id="",
        top_k=10,
    )
    combined = "\n".join(hit.content for hit in hits)
    assert "25" in combined
    assert "17" not in combined


def test_observation_path_projects_explicit_location_updates(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message="I live in Chicago.",
        assistant_response="Thanks for sharing.",
        project="travel",
        session_id="s1",
    )
    graph.observe_conversation(
        user_message="I live in Boston.",
        assistant_response="Understood.",
        project="travel",
        session_id="s2",
    )

    with graph._lock, graph._connect() as connection:
        head = connection.execute(
            """
            SELECT n.value_normalized
            FROM fact_heads fh JOIN nodes n ON n.id = fh.current_node_id
            WHERE fh.tenant_id = ? AND fh.relation_key = 'residence'
            """,
            (graph.tenant_id,),
        ).fetchone()
        historical = connection.execute(
            "SELECT value_normalized, valid_to FROM nodes WHERE relation_key = 'residence' ORDER BY valid_from",
        ).fetchall()
    assert head["value_normalized"] == "Boston"
    assert len(historical) == 2
    assert historical[0]["valid_to"] is not None


def test_observation_source_timestamp_prevents_backfill_from_replacing_current_state(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    newer = datetime(2026, 7, 10, tzinfo=UTC)
    older = newer - timedelta(days=30)
    graph.observe_conversation(
        user_message="I live in Boston.",
        assistant_response="Understood.",
        project="travel",
        session_id="newer-session",
        observed_at=newer,
    )
    graph.observe_conversation(
        user_message="I live in Chicago.",
        assistant_response="Thanks for sharing.",
        project="travel",
        session_id="older-session",
        observed_at=older,
    )

    current = graph.get_state_fact(
        subject_key="user",
        relation_key="residence",
        scope_key="personal",
    )
    historical = graph.get_state_fact(
        subject_key="user",
        relation_key="residence",
        scope_key="personal",
        as_of=older + timedelta(days=1),
    )
    assert current is not None and current.value_normalized == "Boston"
    assert historical is not None and historical.value_normalized == "Chicago"


def test_graph_exposes_compact_slot_complete_event_sum(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    for value, observed_at in (("50", first_time), ("20", first_time + timedelta(days=1))):
        graph.resolve_claim(
            claim=NormalizedClaim(
                subject_key="user",
                relation_key="feed_purchase",
                value_normalized=value,
                fact_kind=FactKind.EVENT,
                scope_key="farm",
                effective_at=observed_at,
                observed_at=observed_at,
                confidence=0.99,
            ),
            label="Feed purchase",
            content=f"Bought {value} pounds of feed.",
            project="farm",
        )

    result = graph.temporal_slot_retriever().retrieve(
        query="How much feed did I buy altogether?",
        project="farm",
        max_context_tokens=200,
    )
    assert result.assembled.calculation is not None
    assert result.assembled.calculation.result == 70
    assert set(result.assembled.calculation.operands) == {20.0, 50.0}
    assert "= 70" in result.context.text
    assert result.context.estimated_tokens <= 200
