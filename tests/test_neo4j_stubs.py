from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock

import numpy as np

from waggle.models import (
    Edge,
    Node,
    NodeType,
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
# Signature contract for previously trapped methods
# ---------------------------------------------------------------------------


def test_neo4j_update_node_signature() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.update_node)
    assert "node_id" in sig.parameters
    assert "content" in sig.parameters
    assert "label" in sig.parameters
    assert "tags" in sig.parameters
    assert "agent_id" in sig.parameters
    assert "project" in sig.parameters
    assert "session_id" in sig.parameters
    assert "valid_from" in sig.parameters
    assert "valid_to" in sig.parameters
    assert "evidence_records" in sig.parameters


def test_neo4j_delete_node_signature() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.delete_node)
    assert "node_id" in sig.parameters


def test_neo4j_update_edge_signature() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.update_edge)
    assert "edge_id" in sig.parameters
    assert "source_id" in sig.parameters
    assert "target_id" in sig.parameters
    assert "relationship" in sig.parameters
    assert "weight" in sig.parameters
    assert "metadata" in sig.parameters


def test_neo4j_delete_edge_signature() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.delete_edge)
    assert "edge_id" in sig.parameters


def test_neo4j_list_recent_nodes_signature() -> None:
    sig = inspect.signature(Neo4jMemoryGraph.list_recent_nodes)
    assert "limit" in sig.parameters
    assert "agent_id" in sig.parameters
    assert "project" in sig.parameters
    assert "session_id" in sig.parameters


def test_neo4j_list_context_scopes_signature() -> None:
    inspect.signature(Neo4jMemoryGraph.list_context_scopes)


def test_neo4j_get_stats_signature() -> None:
    inspect.signature(Neo4jMemoryGraph.get_stats)


def test_neo4j_merge_duplicate_node_preserves_scope_fields() -> None:
    graph = make_mock_graph()
    existing_node = Node(
        id="existing_id",
        tenant_id="tenant_x",
        agent_id="agent_123",
        project="project_abc",
        session_id="session_xyz",
        label="existing_label",
        content="existing_content",
        node_type=NodeType.CONCEPT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    incoming_node = Node(
        id="incoming_id",
        tenant_id="tenant_x",
        agent_id="agent_456",
        project="project_def",
        session_id="session_uvw",
        label="incoming_label",
        content="incoming_content",
        node_type=NodeType.CONCEPT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    mock_session = graph._session.return_value
    merged = graph._merge_duplicate_node(mock_session, existing_node=existing_node, incoming_node=incoming_node)

    assert merged.agent_id == "agent_123"
    assert merged.project == "project_abc"
    assert merged.session_id == "session_xyz"


def test_neo4j_update_edge_deduplication() -> None:
    graph = make_mock_graph()

    # Mock _fetch_node so self._require_node passes
    graph._fetch_node = MagicMock(return_value=MagicMock())

    # Mock database session return value for finding existing edge
    mock_session = graph._session.return_value
    mock_session.run.return_value.single.return_value = {
        "id": "edge_1",
        "source_id": "source",
        "target_id": "target",
        "relationship": "relates_to",
        "weight": 1.0,
        "metadata": "{}",
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    # Mock _find_existing_edge to return a duplicate edge
    existing_dup = Edge(
        id="edge_2",
        tenant_id="local-default",
        source_id="source",
        target_id="target",
        relationship="relates_to",
        weight=1.0,
    )
    graph._find_existing_edge = MagicMock(return_value=existing_dup)

    updated = graph.update_edge(
        edge_id="edge_1",
        source_id="source",
        target_id="target",
        relationship="relates_to",
    )

    # Check that it returns the duplicate edge (edge_2) instead of updating edge_1
    assert updated.id == "edge_2"

    # Verify that the old edge was deleted
    delete_called = False
    for call in mock_session.run.call_args_list:
        query = call[0][0]
        if "DELETE r" in query and "MEMORY_EDGE" in query:
            delete_called = True
    assert delete_called


def test_neo4j_snapshot_scope_persistence() -> None:
    graph = make_mock_graph()
    mock_session = graph._session.return_value

    raw_node = {
        "id": "node_123",
        "label": "TestNode",
        "content": "test content",
        "node_type": "note",
        "agent_id": "my-agent",
        "project": "my-project",
        "session_id": "my-session",
        "tags": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    graph._insert_snapshot_node(mock_session, raw_node)

    # Verify CREATE query contains agent_id, project, and session_id
    create_called = False
    for call in mock_session.run.call_args_list:
        query = call[0][0]
        kwargs = call[1]
        if "CREATE (n:MemoryNode" in query:
            create_called = True
            assert "agent_id" in kwargs
            assert kwargs["agent_id"] == "my-agent"
            assert kwargs["project"] == "my-project"
            assert kwargs["session_id"] == "my-session"
    assert create_called

    mock_session.run.reset_mock()
    graph._update_snapshot_node(mock_session, raw_node)

    # Verify SET query contains agent_id, project, and session_id
    update_called = False
    for call in mock_session.run.call_args_list:
        query = call[0][0]
        kwargs = call[1]
        if "MATCH (n:MemoryNode" in query and "SET" in query:
            update_called = True
            assert "agent_id" in kwargs
            assert kwargs["agent_id"] == "my-agent"
            assert kwargs["project"] == "my-project"
            assert kwargs["session_id"] == "my-session"
    assert update_called


def test_neo4j_import_graph_backup_preserves_embeddings(tmp_path: Path) -> None:
    graph = make_mock_graph()
    mock_session = graph._session.return_value

    # We mock _fetch_node to return None so it performs insert
    graph._fetch_node = MagicMock(return_value=None)

    backup_data = {
        "schema_version": 5,
        "tenant_id": "local-default",
        "nodes": [
            {
                "id": "node_123",
                "label": "TestNode",
                "content": "test content",
                "node_type": "note",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "edges": [],
        "embeddings": {
            "node_123": "AACAPwAAAEA="  # float32 array [1.0, 2.0]
        },
    }

    backup_file = tmp_path / "backup.json"
    backup_file.write_text(json.dumps(backup_data))

    graph.import_graph_backup(input_path=backup_file)

    # Verify that session.run was called to CREATE the node with the preserved embedding
    create_called = False
    for call in mock_session.run.call_args_list:
        query = call[0][0]
        kwargs = call[1]
        if "CREATE (n:MemoryNode" in query:
            create_called = True
            assert "embedding" in kwargs
            assert kwargs["embedding"] == [1.0, 2.0]
    assert create_called
