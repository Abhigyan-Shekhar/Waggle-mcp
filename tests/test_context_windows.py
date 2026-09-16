from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from waggle.graph import MemoryGraph
from waggle.models import NodeType


class FakeEmbeddingModel:
    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            vector[sum(ord(character) for character in token) % len(vector)] += 1.0
        norm = np.linalg.norm(vector)
        return vector if norm == 0.0 else vector / norm

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


def test_ensure_repo_creates_and_reuses_repo(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)

    first = graph.ensure_repo("waggle")
    second = graph.ensure_repo("waggle")

    assert first == second


def test_ensure_context_window_creates_and_reuses_window(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    repo_id = graph.ensure_repo("waggle")

    first = graph.ensure_context_window("session-1", repo_id)
    second = graph.ensure_context_window("session-1", repo_id)
    window = graph.get_context_window(first)

    assert first == second
    assert window.repo_id == repo_id
    assert window.session_id == "session-1"
    assert window.status == "active"


def test_add_node_explicit_timestamp_applies_to_context_window(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    stamp = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)

    stored = graph.add_node(
        node_id="event-1",
        label="Timestamped event",
        content="A deterministic event",
        node_type=NodeType.NOTE,
        project="octo/demo",
        created_at=stamp,
        updated_at=stamp,
    ).node

    assert stored.context_window_id is not None
    window = graph.get_context_window(stored.context_window_id)
    assert window.created_at == stamp
    assert window.updated_at == stamp


def test_resolve_window_context_defaults(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)

    repo_id, window_id = graph.resolve_window_context()
    window = graph.get_context_window(window_id)

    assert repo_id.endswith(":default")
    assert window.repo_id == repo_id
    assert window.session_id == "default"


def test_add_node_tags_created_node_with_context_window(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)

    stored = graph.add_node(
        label="Windowed Fact",
        content="Context windows should own new memory nodes",
        node_type=NodeType.FACT,
        project="waggle",
        session_id="session-1",
    ).node

    assert stored.context_window_id is not None
    window = graph.get_context_window(stored.context_window_id)
    assert window.session_id == "session-1"
    assert window.node_count == 1
    assert window.embedding_stale is True


def test_add_node_without_scope_uses_default_window(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)

    stored = graph.add_node(
        label="Default Window Fact",
        content="Nodes without explicit scope still get a default context window",
        node_type=NodeType.FACT,
    ).node

    assert stored.context_window_id is not None
    assert graph.get_context_window(stored.context_window_id).session_id == "default"


def test_window_embedding_computed_lazily_and_marks_clean(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    node = graph.add_node(
        label="Embedding Fact",
        content="Window embeddings summarize the important nodes",
        node_type=NodeType.FACT,
        project="waggle",
        session_id="session-embedding",
    ).node

    assert node.context_window_id is not None
    assert graph.get_context_window(node.context_window_id).embedding_stale is True

    embedding = graph.get_window_embedding(node.context_window_id)

    assert embedding is not None
    assert graph.get_context_window(node.context_window_id).embedding_stale is False


def test_window_embedding_none_for_empty_window(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    repo_id = graph.ensure_repo("waggle")
    window_id = graph.ensure_context_window("empty", repo_id)

    assert graph.get_window_embedding(window_id) is None


def test_entity_overlap_edge_created(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    repo_id = graph.ensure_repo("waggle")
    first_window = graph.ensure_context_window("first", repo_id)
    second_window = graph.ensure_context_window("second", repo_id)
    graph.add_node(
        label="Deployment",
        content="Deployment uses Kubernetes",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="first",
    )
    graph.add_node(
        label="Deployment",
        content="Deployment uses Kubernetes",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="second",
    )

    edges = graph.derive_context_window_edges(second_window, repo_id)

    overlap_edges = [edge for edge in edges if edge.edge_type == "entity_overlap"]
    assert overlap_edges
    assert overlap_edges[0].source_window_id == first_window
    assert overlap_edges[0].target_window_id == second_window
    assert overlap_edges[0].shared_entities == ["deployment"]


def test_supersedes_edge_created_on_conflicting_entity_content(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    repo_id = graph.ensure_repo("waggle")
    first_window = graph.ensure_context_window("first", repo_id)
    second_window = graph.ensure_context_window("second", repo_id)
    graph.add_node(
        label="Dog",
        content="Dog is named X",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="first",
    )
    graph.add_node(
        label="Dog",
        content="Dog is named Y",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="second",
    )

    edges = graph.derive_context_window_edges(second_window, repo_id)

    supersedes_edges = [edge for edge in edges if edge.edge_type == "supersedes"]
    assert supersedes_edges
    assert supersedes_edges[0].source_window_id == first_window
    assert supersedes_edges[0].target_window_id == second_window
    assert supersedes_edges[0].shared_entities == ["dog"]


def test_temporal_sequence_edge_created(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    repo_id = graph.ensure_repo("waggle")
    first_window = graph.ensure_context_window("first", repo_id)
    second_window = graph.ensure_context_window("second", repo_id)
    graph.add_node(
        label="First",
        content="First window content",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="first",
    )
    graph.add_node(
        label="Second",
        content="Second window content",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="second",
    )

    edges = graph.derive_context_window_edges(second_window, repo_id)

    temporal_edges = [edge for edge in edges if edge.edge_type == "temporal_sequence"]
    assert temporal_edges
    assert temporal_edges[0].source_window_id == first_window
    assert temporal_edges[0].target_window_id == second_window


def test_edge_derivation_is_idempotent(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    repo_id = graph.ensure_repo("waggle")
    graph.ensure_context_window("first", repo_id)
    second_window = graph.ensure_context_window("second", repo_id)
    graph.add_node(
        label="Deployment",
        content="Deployment uses Kubernetes",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="first",
    )
    graph.add_node(
        label="Deployment",
        content="Deployment uses Kubernetes",
        node_type=NodeType.ENTITY,
        project="waggle",
        session_id="second",
    )

    first = graph.derive_context_window_edges(second_window, repo_id)
    second = graph.derive_context_window_edges(second_window, repo_id)

    assert {(edge.source_window_id, edge.target_window_id, edge.edge_type) for edge in first} == {
        (edge.source_window_id, edge.target_window_id, edge.edge_type) for edge in second
    }


def test_list_context_windows_filters_by_project_and_status(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    alpha_repo = graph.ensure_repo("alpha")
    beta_repo = graph.ensure_repo("beta")
    alpha_window = graph.ensure_context_window("alpha-session", alpha_repo)
    graph.ensure_context_window("beta-session", beta_repo)
    graph.close_context_window(alpha_window)

    windows = graph.list_context_windows(project="alpha", status="closed")

    assert [window.id for window in windows] == [alpha_window]


def test_close_context_window_sets_status_and_finalizes_embedding(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    node = graph.add_node(
        label="Closable Window",
        content="Closing a context window computes its final embedding",
        node_type=NodeType.FACT,
        project="waggle",
        session_id="closable",
    ).node

    assert node.context_window_id is not None
    closed = graph.close_context_window(node.context_window_id)

    assert closed.status == "closed"
    assert closed.closed_at is not None
    assert closed.embedding_stale is False
    assert closed.node_count == 1
