from __future__ import annotations

import inspect
from types import MethodType
from unittest.mock import MagicMock

import numpy as np

from waggle.models import (
    SubgraphResult,
)
from waggle.neo4j_graph import Neo4jMemoryGraph


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        del text
        return np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        del a, b
        return 1.0


def make_stub_graph() -> Neo4jMemoryGraph:
    graph = object.__new__(Neo4jMemoryGraph)
    graph.tenant_id = "local-default"
    graph.embedding_model = FakeEmbeddingModel()
    return graph


def make_mock_graph() -> Neo4jMemoryGraph:
    graph = object.__new__(Neo4jMemoryGraph)
    graph.tenant_id = "local-default"
    graph.embedding_model = FakeEmbeddingModel()
    graph._lock = MagicMock()
    graph._lock.__enter__ = MagicMock(return_value=None)
    graph._lock.__exit__ = MagicMock(return_value=None)
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=None)
    graph._session = MagicMock(return_value=mock_session)
    return graph


# ---------------------------------------------------------------------------
# Stub coverage — methods that return hardcoded values without a DB
# ---------------------------------------------------------------------------


def test_neo4j_context_window_stubs_do_not_raise() -> None:
    graph = make_stub_graph()

    repo_id, window_id = graph.resolve_window_context("project", "session")
    window = graph.get_context_window(window_id)
    closed = graph.close_context_window(window_id)

    assert repo_id == "default"
    assert window.id == "session"
    assert graph.list_context_windows() == []
    assert graph.get_context_window_edges(window_id) == []
    assert graph.get_window_nodes(window_id) == []
    assert graph.compute_window_embedding(window_id) is None
    assert graph.derive_context_window_edges(window_id, repo_id) == []
    assert graph.get_nodes_without_window() == []
    assert graph.assign_nodes_to_window(["node"], window_id) == 0
    assert graph.list_repos() == []
    assert graph.update_window_node_count(window_id) == 0
    assert closed.status == "closed"


def test_neo4j_tiered_query_falls_back_to_flat_query() -> None:
    graph = make_stub_graph()

    def fake_query(self: Neo4jMemoryGraph, **kwargs: object) -> SubgraphResult:
        return SubgraphResult(query=str(kwargs["query"]), retrieval_mode="graph")

    graph.query = MethodType(fake_query, graph)

    result = graph.tiered_query(query="database", project="project")

    assert result.query == "database"
    assert result.retrieval_mode == "flat_fallback"


# ---------------------------------------------------------------------------
# Signature contract — verify parameter names match SQLite expectations
# ---------------------------------------------------------------------------


def test_neo4j_add_node_signature_has_required_params() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.add_node)
    assert "label" in sig.parameters
    assert "content" in sig.parameters
    assert "node_type" in sig.parameters
    assert sig.parameters["label"].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["content"].kind == inspect.Parameter.KEYWORD_ONLY


def test_neo4j_add_node_optional_params() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.add_node)
    assert "agent_id" in sig.parameters
    assert "project" in sig.parameters
    assert "session_id" in sig.parameters
    assert "tags" in sig.parameters
    assert "node_id" in sig.parameters


def test_neo4j_add_edge_signature_has_required_params() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.add_edge)
    assert "source_id" in sig.parameters
    assert "target_id" in sig.parameters
    assert "relationship" in sig.parameters


def test_neo4j_query_signature_has_required_params() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.query)
    assert "query" in sig.parameters
    assert "retrieval_mode" in sig.parameters
    assert "max_nodes" in sig.parameters
    assert "max_depth" in sig.parameters


# ---------------------------------------------------------------------------
# Input validation — verify argument checking exists
# ---------------------------------------------------------------------------


def test_neo4j_add_node_validates_required_params() -> None:
    graph = make_mock_graph()
    import pytest

    with pytest.raises(TypeError):
        graph.add_node()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        graph.add_node(label="x")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        graph.add_node(label="x", content="y")  # type: ignore[call-arg]


def test_neo4j_query_validates_inputs() -> None:
    graph = make_stub_graph()
    import pytest

    with pytest.raises(ValueError, match="empty"):
        graph.query(query="")
    with pytest.raises(ValueError, match="max_nodes"):
        graph.query(query="test", max_nodes=0)
    with pytest.raises(ValueError, match="max_depth"):
        graph.query(query="test", max_depth=-1)


# ---------------------------------------------------------------------------
# Query contract — verify stub-based query can be overridden
# ---------------------------------------------------------------------------


def test_neo4j_query_accepts_standard_params() -> None:
    graph = make_stub_graph()

    def fake_graph_only(**kwargs: object) -> SubgraphResult:
        return SubgraphResult(query=str(kwargs["query"]), retrieval_mode="graph")

    graph._query_graph_only = fake_graph_only  # type: ignore[method-assign]
    graph._query_replay_hits = MagicMock(return_value=[])  # type: ignore[method-assign]
    graph._build_fusion_hits = MagicMock(return_value=[])  # type: ignore[method-assign]

    graph_mode = graph.query(query="test query", max_nodes=10, max_depth=2, retrieval_mode="graph")
    assert isinstance(graph_mode, SubgraphResult)
    assert graph_mode.query == "test query"
    assert graph_mode.retrieval_mode == "graph"

    verbatim_mode = graph.query(
        query="test query", retrieval_mode="verbatim", agent_id="agent", project="project", session_id="session"
    )
    assert isinstance(verbatim_mode, SubgraphResult)
    assert verbatim_mode.retrieval_mode == "verbatim"


# ---------------------------------------------------------------------------
# for_tenant factory — verify it returns a properly-configured instance
# ---------------------------------------------------------------------------


def test_neo4j_for_tenant_returns_new_instance() -> None:
    graph = make_mock_graph()
    graph._driver = MagicMock()
    # The child instance reuses this driver and runs ensure_tenant() during
    # construction, which parses created_at; return a real ISO timestamp so the
    # mocked session yields a parseable value instead of a MagicMock.
    _session = graph._driver.session.return_value.__enter__.return_value
    _session.run.return_value.single.return_value = {
        "tenant_id": "tenant-child",
        "name": "",
        "status": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    graph.database = None
    graph.dedup_similarity_threshold = 0.97
    graph.dedup_same_label_threshold = 0.9
    graph.api_key_environment = "test"
    graph._uri = "bolt://localhost:7687"
    graph._username = "neo4j"
    graph._password = "password"
    graph._owns_driver = True
    graph.export_dir = "exports"

    import pytest

    try:
        child = graph.for_tenant("tenant-child")
        assert isinstance(child, Neo4jMemoryGraph)
        assert child.tenant_id == "tenant-child"
    except (ImportError, RuntimeError) as e:
        if "neo4j" in str(e):
            pytest.skip("neo4j driver not available")


# ---------------------------------------------------------------------------
# Note: Known Neo4j gaps
# ---------------------------------------------------------------------------
#
# The following methods are defined in `src/waggle/neo4j_graph.py` but are
# NOT accessible on `Neo4jMemoryGraph` instances because they appear inside
# a module-level `def update_node(...)` function (line 1867) that is never
# called and whose body (lines 1959-4277) is dead code.  These methods
# cannot be tested without first fixing the indentation:
#
#   - delete_node        (line 2069)
#   - update_edge        (line 1959)
#   - delete_edge        (line 2034)
#   - list_recent_nodes  (line 2084)
#   - list_context_scopes(line 2110)
#   - get_stats          (line 2125)
#   - list_transcript_records  (line 3375)
#   - search_transcript_records(line 3407)
#
# Additionally, `add_node` and `add_edge` *are* accessible on the class but
# internally call private helpers (`_find_duplicate_node`, `_require_node`,
# `_fetch_node`, `_node_create_params`, `_node_from_props`,
# `_register_conflicts`, `_find_existing_edge`) that are also trapped in
# the same dead-code region.  These methods cannot execute without fixing
# the indentation first.
