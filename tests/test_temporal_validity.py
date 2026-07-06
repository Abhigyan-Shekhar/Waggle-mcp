"""Tests for temporal validity window enforcement in query_graph and aggregate_graph.

Covers:
- Default queries exclude expired nodes (valid_to in the past).
- include_invalidated=True returns expired nodes.
- as_of=<datetime> returns nodes valid at that point in time.
- resolve_conflict(winner=...) sets valid_to on the losing node.
- resolve_conflict with an invalid winner raises ValueError.
- WAGGLE_ENFORCE_VALID_TO=false restores legacy behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(c) for c in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

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


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


def _utc(dt: datetime) -> datetime:
    """Ensure *dt* is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime.now(UTC)
T0 = NOW - timedelta(hours=2)  # two hours ago
T1 = NOW - timedelta(hours=1)  # one hour ago  (valid_to in the past)
T_HALF = NOW - timedelta(minutes=90)  # between T0 and T1
T2 = NOW + timedelta(hours=1)  # one hour in the future


# ---------------------------------------------------------------------------
# Tests: default query excludes expired nodes
# ---------------------------------------------------------------------------


def test_expired_node_excluded_by_default(tmp_path: Path) -> None:
    """A node with valid_to in the past must NOT appear in default query results."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    # Manually expire the node by setting valid_to to the past
    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T1.isoformat(), node_id),
        )

    subgraph = graph.query(query="sky colour", max_nodes=10, max_depth=0, retrieval_mode="graph")
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id not in returned_ids, "Expired node should be excluded from default query"


def test_expired_node_excluded_from_aggregate_by_default(tmp_path: Path) -> None:
    """A node with valid_to in the past must NOT appear in default aggregate results."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T1.isoformat(), node_id),
        )

    subgraph = graph.aggregate(query="sky colour", max_nodes=100)
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id not in returned_ids, "Expired node should be excluded from default aggregate"


# ---------------------------------------------------------------------------
# Tests: include_invalidated=True returns expired nodes
# ---------------------------------------------------------------------------


def test_expired_node_returned_with_include_invalidated(tmp_path: Path) -> None:
    """include_invalidated=True must return nodes whose valid_to has passed."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T1.isoformat(), node_id),
        )

    subgraph = graph.query(
        query="sky colour",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
        include_invalidated=True,
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Expired node should be returned when include_invalidated=True"


def test_expired_node_returned_with_include_invalidated_aggregate(tmp_path: Path) -> None:
    """include_invalidated=True must return expired nodes in aggregate."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T1.isoformat(), node_id),
        )

    subgraph = graph.aggregate(query="sky colour", max_nodes=100, include_invalidated=True)
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Expired node should be returned when include_invalidated=True"


# ---------------------------------------------------------------------------
# Tests: as_of parameter
# ---------------------------------------------------------------------------


def test_as_of_within_validity_window_returns_node(tmp_path: Path) -> None:
    """as_of between valid_from and valid_to must return the node."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    # valid_from=T0, valid_to=T1 — node was valid between T0 and T1
    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_from = ?, valid_to = ? WHERE id = ?",
            (T0.isoformat(), T1.isoformat(), node_id),
        )

    # Query at T_HALF (between T0 and T1) — should return the node
    subgraph = graph.query(
        query="sky colour",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
        as_of=T_HALF,
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Node should be returned when as_of is within its validity window"


def test_as_of_after_valid_to_excludes_node(tmp_path: Path) -> None:
    """as_of after valid_to must exclude the node."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_from = ?, valid_to = ? WHERE id = ?",
            (T0.isoformat(), T1.isoformat(), node_id),
        )

    # Query at NOW (after T1) — should NOT return the node
    subgraph = graph.query(
        query="sky colour",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
        as_of=NOW,
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id not in returned_ids, "Node should be excluded when as_of is after valid_to"


def test_as_of_aggregate_within_window(tmp_path: Path) -> None:
    """as_of within validity window returns node in aggregate."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_from = ?, valid_to = ? WHERE id = ?",
            (T0.isoformat(), T1.isoformat(), node_id),
        )

    subgraph = graph.aggregate(query="sky colour", max_nodes=100, as_of=T_HALF)
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Node should be returned when as_of is within its validity window"


# ---------------------------------------------------------------------------
# Tests: resolve_conflict sets valid_to on the losing node
# ---------------------------------------------------------------------------


def test_resolve_conflict_with_winner_sets_valid_to_on_loser(tmp_path: Path) -> None:
    """resolve_conflict(winner=A) must set valid_to on B (the loser), not on A."""
    graph = make_graph(tmp_path)

    node_a = graph.add_node(
        content="We use PostgreSQL for production",
        label="DB choice: Postgres",
        node_type=NodeType.DECISION,
    ).node

    node_b = graph.add_node(
        content="We use MySQL for production",
        label="DB choice: MySQL",
        node_type=NodeType.DECISION,
    ).node

    edge = graph.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship=RelationType.CONTRADICTS.value,
    )

    before = datetime.now(UTC)
    graph.resolve_conflict(edge_id=edge.id, winner=node_a.id)
    after = datetime.now(UTC)

    # Reload nodes from DB
    refreshed_a = graph.get_node(node_a.id)
    refreshed_b = graph.get_node(node_b.id)

    # Winner (A) must NOT have valid_to set
    assert refreshed_a.valid_to is None, "Winner node must not have valid_to set"

    # Loser (B) must have valid_to set to approximately now
    assert refreshed_b.valid_to is not None, "Loser node must have valid_to set"
    assert before <= refreshed_b.valid_to <= after, "Loser valid_to must be close to resolution time"


def test_resolve_conflict_winner_is_target(tmp_path: Path) -> None:
    """resolve_conflict(winner=target_id) must set valid_to on source (the loser)."""
    graph = make_graph(tmp_path)

    node_a = graph.add_node(
        content="We use PostgreSQL for production",
        label="DB choice: Postgres",
        node_type=NodeType.DECISION,
    ).node

    node_b = graph.add_node(
        content="We use MySQL for production",
        label="DB choice: MySQL",
        node_type=NodeType.DECISION,
    ).node

    edge = graph.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship=RelationType.CONTRADICTS.value,
    )

    graph.resolve_conflict(edge_id=edge.id, winner=node_b.id)

    refreshed_a = graph.get_node(node_a.id)
    refreshed_b = graph.get_node(node_b.id)

    assert refreshed_b.valid_to is None, "Winner (target) must not have valid_to set"
    assert refreshed_a.valid_to is not None, "Loser (source) must have valid_to set"


def test_resolve_conflict_loser_excluded_from_default_query(tmp_path: Path) -> None:
    """After resolve_conflict with winner, the loser must not appear in default queries."""
    graph = make_graph(tmp_path)

    node_a = graph.add_node(
        content="We use PostgreSQL for production",
        label="DB choice: Postgres",
        node_type=NodeType.DECISION,
    ).node

    node_b = graph.add_node(
        content="We use MySQL for production",
        label="DB choice: MySQL",
        node_type=NodeType.DECISION,
    ).node

    edge = graph.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship=RelationType.CONTRADICTS.value,
    )

    graph.resolve_conflict(edge_id=edge.id, winner=node_a.id)

    subgraph = graph.query(
        query="production database choice",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
    )
    returned_ids = {n.id for n in subgraph.nodes}

    assert node_b.id not in returned_ids, "Loser node must be excluded from default query after resolution"


def test_resolve_conflict_loser_returned_with_include_invalidated(tmp_path: Path) -> None:
    """After resolve_conflict, the loser must still be retrievable via include_invalidated=True."""
    graph = make_graph(tmp_path)

    node_a = graph.add_node(
        content="We use PostgreSQL for production",
        label="DB choice: Postgres",
        node_type=NodeType.DECISION,
    ).node

    node_b = graph.add_node(
        content="We use MySQL for production",
        label="DB choice: MySQL",
        node_type=NodeType.DECISION,
    ).node

    edge = graph.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship=RelationType.CONTRADICTS.value,
    )

    graph.resolve_conflict(edge_id=edge.id, winner=node_a.id)

    subgraph = graph.query(
        query="production database choice",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
        include_invalidated=True,
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_b.id in returned_ids, "Loser node must be retrievable via include_invalidated=True"


def test_resolve_conflict_without_winner_invalidates_target(tmp_path: Path) -> None:
    """resolve_conflict without winner must invalidate the target node by setting valid_to."""
    graph = make_graph(tmp_path)

    node_a = graph.add_node(
        content="We use PostgreSQL for production",
        label="DB choice: Postgres",
        node_type=NodeType.DECISION,
    ).node

    node_b = graph.add_node(
        content="We use MySQL for production",
        label="DB choice: MySQL",
        node_type=NodeType.DECISION,
    ).node

    edge = graph.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship=RelationType.CONTRADICTS.value,
    )

    graph.resolve_conflict(edge_id=edge.id)

    refreshed_a = graph.get_node(node_a.id)
    refreshed_b = graph.get_node(node_b.id)

    assert refreshed_a.valid_to is None, "Source node must not have valid_to set when no winner given"
    assert refreshed_b.valid_to is not None, "Target node must have valid_to set when no winner given"


# ---------------------------------------------------------------------------
# Tests: invalid winner raises ValueError
# ---------------------------------------------------------------------------


def test_resolve_conflict_invalid_winner_raises(tmp_path: Path) -> None:
    """resolve_conflict with a winner not on the edge must raise ValueError."""
    graph = make_graph(tmp_path)

    node_a = graph.add_node(
        content="We use PostgreSQL for production",
        label="DB choice: Postgres",
        node_type=NodeType.DECISION,
    ).node

    node_b = graph.add_node(
        content="We use MySQL for production",
        label="DB choice: MySQL",
        node_type=NodeType.DECISION,
    ).node

    node_c = graph.add_node(
        content="We use SQLite for production",
        label="DB choice: SQLite",
        node_type=NodeType.DECISION,
    ).node

    edge = graph.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship=RelationType.CONTRADICTS.value,
    )

    with pytest.raises(ValueError, match="not an endpoint"):
        graph.resolve_conflict(edge_id=edge.id, winner=node_c.id)


# ---------------------------------------------------------------------------
# Tests: WAGGLE_ENFORCE_VALID_TO=false restores legacy behaviour
# ---------------------------------------------------------------------------


def test_enforce_valid_to_false_returns_expired_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With WAGGLE_ENFORCE_VALID_TO=false, expired nodes must appear in default queries."""
    monkeypatch.setenv("WAGGLE_ENFORCE_VALID_TO", "false")

    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T1.isoformat(), node_id),
        )

    subgraph = graph.query(
        query="sky colour",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Expired node should appear when WAGGLE_ENFORCE_VALID_TO=false"


def test_enforce_valid_to_false_aggregate_returns_expired_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With WAGGLE_ENFORCE_VALID_TO=false, expired nodes must appear in aggregate."""
    monkeypatch.setenv("WAGGLE_ENFORCE_VALID_TO", "false")

    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T1.isoformat(), node_id),
        )

    subgraph = graph.aggregate(query="sky colour", max_nodes=100)
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Expired node should appear in aggregate when WAGGLE_ENFORCE_VALID_TO=false"


# ---------------------------------------------------------------------------
# Tests: valid nodes (no valid_to) are always returned
# ---------------------------------------------------------------------------


def test_node_without_valid_to_always_returned(tmp_path: Path) -> None:
    """Nodes with valid_to=NULL must always appear in default queries."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    subgraph = graph.query(
        query="sky colour",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Node without valid_to must always be returned"


def test_future_valid_to_node_returned(tmp_path: Path) -> None:
    """Nodes with valid_to in the future must appear in default queries."""
    graph = make_graph(tmp_path)
    result = graph.add_node(
        content="The sky is blue",
        label="Sky colour",
        node_type=NodeType.FACT,
    )
    node_id = result.node.id

    with graph._lock, graph._connect() as conn:
        conn.execute(
            "UPDATE nodes SET valid_to = ? WHERE id = ?",
            (T2.isoformat(), node_id),
        )

    subgraph = graph.query(
        query="sky colour",
        max_nodes=10,
        max_depth=0,
        retrieval_mode="graph",
    )
    returned_ids = {n.id for n in subgraph.nodes}
    assert node_id in returned_ids, "Node with future valid_to must be returned"
