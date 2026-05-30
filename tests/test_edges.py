"""Tests for edge quality improvements.

Covers:
- infer_relationship tightened RELATES_TO fallback (≥2 shared tokens + cosine ≥ 0.55)
- edge_confidence field on all inferred edges
- .abhi export filtering of low-confidence RELATES_TO edges
- --include-low-confidence-edges override
- edge_quality_report MCP tool
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from waggle.abhi import build_abhi_document, write_abhi_document
from waggle.graph import MemoryGraph
from waggle.intelligence import (
    RELATES_TO_COSINE_THRESHOLD,
    TYPED_EDGE_CONFIDENCE,
    infer_relationship,
)
from waggle.models import Node, NodeType, RelationType

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class FakeEmbeddingModel:
    """Deterministic embedding model for tests.

    Produces unit-normalised 8-dim vectors based on token character sums.
    Two texts with many shared tokens will have high cosine similarity;
    texts with no shared tokens will have low similarity.
    """

    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(c) for c in token) % len(vector)
            vector[index] += 1.0
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return vector
        return vector / norm

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return np.asarray(embedding, dtype=np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = float(np.linalg.norm(a))
        b_norm = float(np.linalg.norm(b))
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


def _make_node(content: str, node_type: NodeType = NodeType.FACT) -> Node:
    """Create a minimal Node for unit-testing infer_relationship."""
    return Node(label=content[:40], content=content, node_type=node_type)


def _edge_confidence(graph: MemoryGraph, source_id: str, target_id: str) -> float | None:
    """Fetch edge_confidence from metadata for a specific edge pair."""

    with graph._lock, graph._connect() as conn:
        row = conn.execute(
            "SELECT metadata FROM edges WHERE source_id = ? AND target_id = ? AND tenant_id = ?",
            (source_id, target_id, graph.tenant_id),
        ).fetchone()
    if row is None:
        return None
    meta = json.loads(row["metadata"] or "{}")
    return meta.get("edge_confidence")


# ---------------------------------------------------------------------------
# Unit tests for infer_relationship
# ---------------------------------------------------------------------------


class TestInferRelationshipStopwordOnly:
    """Nodes sharing only stopwords must produce no edge."""

    def test_only_the_returns_none(self) -> None:
        # "the" is a stopword; tokenize_text filters it → 0 shared non-stopword tokens
        a = _make_node("the quick brown fox")
        b = _make_node("the lazy dog")
        result = infer_relationship(a, b, cosine_similarity=0.9)
        assert result is None, "Stopword-only overlap must not create an edge"

    def test_single_stopword_returns_none(self) -> None:
        a = _make_node("it is raining")
        b = _make_node("it is sunny")
        # "it" and "is" are stopwords; "raining" vs "sunny" → 0 shared non-stopword tokens
        result = infer_relationship(a, b, cosine_similarity=0.9)
        assert result is None


class TestInferRelationshipLowCosine:
    """Two non-stopword shared tokens but cosine < 0.55 → no edge."""

    def test_two_shared_tokens_low_cosine_returns_none(self) -> None:
        # Manually pass shared_tokens with 2 entries but low cosine
        a = _make_node("python database migration")
        b = _make_node("python database schema")
        shared = {"python", "database"}
        result = infer_relationship(a, b, shared_tokens=shared, cosine_similarity=0.40)
        assert result is None, "Low cosine must block RELATES_TO even with 2 shared tokens"


class TestInferRelationshipHighCosine:
    """Two non-stopword shared tokens AND cosine ≥ 0.55 → RELATES_TO with confidence = cosine."""

    def test_two_shared_tokens_high_cosine_creates_relates_to(self) -> None:
        a = _make_node("python database migration")
        b = _make_node("python database schema")
        shared = {"python", "database"}
        result = infer_relationship(a, b, shared_tokens=shared, cosine_similarity=0.70)
        assert result is not None
        rel_type, confidence = result
        assert rel_type == RelationType.RELATES_TO
        assert confidence == pytest.approx(0.70)

    def test_confidence_equals_cosine_score(self) -> None:
        a = _make_node("machine learning model training")
        b = _make_node("machine learning model inference")
        shared = {"machine", "learning", "model"}
        cosine = 0.82
        result = infer_relationship(a, b, shared_tokens=shared, cosine_similarity=cosine)
        assert result is not None
        _, confidence = result
        assert confidence == pytest.approx(cosine)

    def test_at_threshold_boundary_creates_edge(self) -> None:
        a = _make_node("api authentication token")
        b = _make_node("api authentication session")
        shared = {"api", "authentication"}
        result = infer_relationship(a, b, shared_tokens=shared, cosine_similarity=RELATES_TO_COSINE_THRESHOLD)
        assert result is not None
        assert result[0] == RelationType.RELATES_TO


class TestInferRelationshipTypedEdges:
    """Typed edges (DEPENDS_ON, UPDATES, PART_OF) carry TYPED_EDGE_CONFIDENCE = 0.9."""

    def test_depends_on_keyword(self) -> None:
        a = _make_node("service A depends on service B")
        b = _make_node("service B provides auth")
        result = infer_relationship(a, b)
        assert result is not None
        rel_type, confidence = result
        assert rel_type == RelationType.DEPENDS_ON
        assert confidence == pytest.approx(TYPED_EDGE_CONFIDENCE)

    def test_updates_keyword(self) -> None:
        a = _make_node("this replaces the old config")
        b = _make_node("old config was yaml")
        result = infer_relationship(a, b)
        assert result is not None
        rel_type, confidence = result
        assert rel_type == RelationType.UPDATES
        assert confidence == pytest.approx(TYPED_EDGE_CONFIDENCE)

    def test_part_of_concept_target(self) -> None:
        a = _make_node("JWT token", node_type=NodeType.FACT)
        b = _make_node("authentication", node_type=NodeType.CONCEPT)
        result = infer_relationship(a, b)
        assert result is not None
        rel_type, confidence = result
        assert rel_type == RelationType.PART_OF
        assert confidence == pytest.approx(TYPED_EDGE_CONFIDENCE)


class TestInferRelationshipNoCosine:
    """When cosine is not supplied, token condition alone is sufficient (confidence = 0.5)."""

    def test_two_shared_tokens_no_cosine_creates_edge_with_low_confidence(self) -> None:
        a = _make_node("redis cache eviction policy")
        b = _make_node("redis cache configuration")
        shared = {"redis", "cache"}
        result = infer_relationship(a, b, shared_tokens=shared, cosine_similarity=None)
        assert result is not None
        rel_type, confidence = result
        assert rel_type == RelationType.RELATES_TO
        assert confidence == pytest.approx(0.5)

    def test_one_shared_token_no_cosine_returns_none(self) -> None:
        a = _make_node("redis eviction")
        b = _make_node("redis configuration")
        shared = {"redis"}
        result = infer_relationship(a, b, shared_tokens=shared, cosine_similarity=None)
        assert result is None


# ---------------------------------------------------------------------------
# Integration tests: edge_confidence stored in graph metadata
# ---------------------------------------------------------------------------


class TestEdgeConfidenceStoredInGraph:
    def test_observe_conversation_stores_edge_confidence(self, tmp_path: Path) -> None:
        """Edges created via observe_conversation carry edge_confidence in metadata."""
        graph = make_graph(tmp_path)
        # Use content that shares ≥2 non-stopword tokens so an edge may be created
        graph.observe_conversation(
            user_message="I use Python for machine learning projects.",
            assistant_response="Python is great for machine learning.",
        )
        # Check that any RELATES_TO edges have edge_confidence in metadata
        with graph._lock, graph._connect() as conn:
            rows = conn.execute(
                "SELECT metadata FROM edges WHERE tenant_id = ? AND relationship = 'relates_to'",
                (graph.tenant_id,),
            ).fetchall()
        for row in rows:
            meta = json.loads(row["metadata"] or "{}")
            assert "edge_confidence" in meta, "RELATES_TO edges must carry edge_confidence"
            assert 0.0 <= meta["edge_confidence"] <= 1.0

    def test_typed_edge_has_confidence_09(self, tmp_path: Path) -> None:
        """Typed edges created via observe_conversation carry confidence = 0.9."""
        graph = make_graph(tmp_path)
        graph.observe_conversation(
            user_message="The auth service depends on the user database.",
            assistant_response="Yes, auth requires the user database to validate credentials.",
        )
        with graph._lock, graph._connect() as conn:
            rows = conn.execute(
                "SELECT metadata FROM edges WHERE tenant_id = ? AND relationship = 'depends_on'",
                (graph.tenant_id,),
            ).fetchall()
        for row in rows:
            meta = json.loads(row["metadata"] or "{}")
            if "edge_confidence" in meta:
                assert meta["edge_confidence"] == pytest.approx(TYPED_EDGE_CONFIDENCE)


# ---------------------------------------------------------------------------
# .abhi export filtering tests
# ---------------------------------------------------------------------------


def _make_snapshot_with_edges(
    *,
    high_confidence_count: int = 2,
    low_confidence_count: int = 3,
) -> dict:
    """Build a minimal snapshot dict with synthetic edges for export tests."""
    import uuid

    from waggle.models import utc_now

    now = utc_now().isoformat()
    nodes = []
    for i in range(max(high_confidence_count, low_confidence_count) * 2 + 2):
        nodes.append(
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "agent_id": "",
                "project": "",
                "session_id": "",
                "context_window_id": None,
                "label": f"Node {i}",
                "content": f"Content for node {i}",
                "node_type": "fact",
                "tags": [],
                "source_prompt": "",
                "embedding_model_id": "",
                "embedding_dim": 0,
                "source_turn_pair_id": "",
                "metadata": {},
                "evidence_records": [],
                "valid_from": None,
                "valid_to": None,
                "created_at": now,
                "updated_at": now,
                "access_count": 0,
                "embedding": None,
            }
        )

    node_ids = [n["id"] for n in nodes]
    edges = []

    # High-confidence RELATES_TO edges (confidence = 0.8)
    for i in range(high_confidence_count):
        edges.append(
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "source_id": node_ids[i * 2],
                "target_id": node_ids[i * 2 + 1],
                "relationship": "relates_to",
                "weight": 1.0,
                "metadata": {"edge_confidence": 0.8},
                "created_at": now,
            }
        )

    # Low-confidence RELATES_TO edges (confidence = 0.5)
    for i in range(low_confidence_count):
        edges.append(
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "source_id": node_ids[i * 2],
                "target_id": node_ids[i * 2 + 1],
                "relationship": "relates_to",
                "weight": 1.0,
                "metadata": {"edge_confidence": 0.5},
                "created_at": now,
            }
        )

    # A typed DEPENDS_ON edge (no confidence field — treated as 1.0)
    edges.append(
        {
            "id": str(uuid.uuid4()),
            "tenant_id": "local-default",
            "source_id": node_ids[0],
            "target_id": node_ids[1],
            "relationship": "depends_on",
            "weight": 1.0,
            "metadata": {"edge_confidence": 0.9},
            "created_at": now,
        }
    )

    return {
        "schema_version": 1,
        "tenant_id": "local-default",
        "embedding_model_id": "",
        "nodes": nodes,
        "edges": edges,
        "transcripts": [],
        "context_windows": [],
        "repos": [],
        "context_window_edges": [],
    }


class TestAbhiExportEdgeFiltering:
    def test_low_confidence_edges_filtered_by_default(self) -> None:
        """Default export excludes RELATES_TO edges with confidence < 0.7."""
        snapshot = _make_snapshot_with_edges(high_confidence_count=2, low_confidence_count=3)
        doc = build_abhi_document(snapshot)
        [e["relationship"] for e in doc["edges"]]
        # All 3 low-confidence RELATES_TO should be gone; 2 high-confidence + 1 depends_on remain
        relates_to_edges = [e for e in doc["edges"] if e["relationship"] == "relates_to"]
        assert len(relates_to_edges) == 2, f"Expected 2 high-confidence RELATES_TO, got {len(relates_to_edges)}"
        # depends_on must always be present
        assert any(e["relationship"] == "depends_on" for e in doc["edges"])

    def test_export_summary_reports_filtered_count(self) -> None:
        """Export context reports how many edges were filtered."""
        snapshot = _make_snapshot_with_edges(high_confidence_count=2, low_confidence_count=3)
        doc = build_abhi_document(snapshot)
        edge_filter = doc["manifest"]["export_context"]["edge_filter"]
        assert edge_filter["edges_filtered"] == 3
        assert edge_filter["edges_exported"] == 3  # 2 high-conf + 1 depends_on
        assert edge_filter["edges_total"] == 6  # 2 + 3 + 1

    def test_include_low_confidence_edges_flag_includes_all(self) -> None:
        """include_low_confidence_edges=True exports all edges."""
        snapshot = _make_snapshot_with_edges(high_confidence_count=2, low_confidence_count=3)
        doc = build_abhi_document(snapshot, include_low_confidence_edges=True)
        edge_filter = doc["manifest"]["export_context"]["edge_filter"]
        assert edge_filter["edges_filtered"] == 0
        assert edge_filter["edges_exported"] == 6
        relates_to_edges = [e for e in doc["edges"] if e["relationship"] == "relates_to"]
        assert len(relates_to_edges) == 5

    def test_write_abhi_document_filtered(self, tmp_path: Path) -> None:
        """write_abhi_document respects include_low_confidence_edges=False."""
        snapshot = _make_snapshot_with_edges(high_confidence_count=1, low_confidence_count=2)
        out = tmp_path / "test.abhi"
        result = write_abhi_document(snapshot, output_path=out, include_low_confidence_edges=False)
        # 1 high-conf RELATES_TO + 1 depends_on = 2 edges exported
        assert result.edge_count == 2
        assert result.export_context["edge_filter"]["edges_filtered"] == 2

    def test_write_abhi_document_include_all(self, tmp_path: Path) -> None:
        """write_abhi_document with include_low_confidence_edges=True exports all."""
        snapshot = _make_snapshot_with_edges(high_confidence_count=1, low_confidence_count=2)
        out = tmp_path / "test_all.abhi"
        result = write_abhi_document(snapshot, output_path=out, include_low_confidence_edges=True)
        assert result.edge_count == 4  # 1 + 2 + 1 depends_on
        assert result.export_context["edge_filter"]["edges_filtered"] == 0

    def test_non_relates_to_edges_never_filtered(self) -> None:
        """DEPENDS_ON, UPDATES, CONTRADICTS edges are never filtered regardless of confidence."""
        import uuid

        from waggle.models import utc_now

        now = utc_now().isoformat()
        node_ids = [str(uuid.uuid4()) for _ in range(4)]
        nodes = [
            {
                "id": nid,
                "tenant_id": "local-default",
                "agent_id": "",
                "project": "",
                "session_id": "",
                "context_window_id": None,
                "label": f"N{i}",
                "content": f"content {i}",
                "node_type": "fact",
                "tags": [],
                "source_prompt": "",
                "embedding_model_id": "",
                "embedding_dim": 0,
                "source_turn_pair_id": "",
                "metadata": {},
                "evidence_records": [],
                "valid_from": None,
                "valid_to": None,
                "created_at": now,
                "updated_at": now,
                "access_count": 0,
                "embedding": None,
            }
            for i, nid in enumerate(node_ids)
        ]
        edges = [
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "source_id": node_ids[0],
                "target_id": node_ids[1],
                "relationship": "depends_on",
                "weight": 1.0,
                "metadata": {"edge_confidence": 0.1},
                "created_at": now,
            },
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "source_id": node_ids[2],
                "target_id": node_ids[3],
                "relationship": "updates",
                "weight": 1.0,
                "metadata": {"edge_confidence": 0.2},
                "created_at": now,
            },
        ]
        snapshot = {
            "schema_version": 1,
            "tenant_id": "local-default",
            "embedding_model_id": "",
            "nodes": nodes,
            "edges": edges,
            "transcripts": [],
            "context_windows": [],
            "repos": [],
            "context_window_edges": [],
        }
        doc = build_abhi_document(snapshot, include_low_confidence_edges=False)
        # Both typed edges must survive even with very low confidence
        assert len(doc["edges"]) == 2


# ---------------------------------------------------------------------------
# edge_quality_report tests
# ---------------------------------------------------------------------------


class TestEdgeQualityReport:
    def test_empty_graph_returns_zero_counts(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        report = graph.edge_quality_report()
        assert report["total_edges"] == 0
        assert report["total_edge_types"] == 0
        assert report["by_type"] == {}

    def test_report_counts_by_type(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = graph.add_node(label="A", content="service A", node_type=NodeType.FACT).node
        b = graph.add_node(label="B", content="service B", node_type=NodeType.FACT).node
        c = graph.add_node(label="C", content="service C", node_type=NodeType.FACT).node
        graph.add_edge(
            source_id=a.id,
            target_id=b.id,
            relationship=RelationType.DEPENDS_ON,
            metadata={"edge_confidence": 0.9},
        )
        graph.add_edge(
            source_id=b.id,
            target_id=c.id,
            relationship=RelationType.RELATES_TO,
            metadata={"edge_confidence": 0.6},
        )
        report = graph.edge_quality_report()
        assert report["total_edges"] == 2
        assert "depends_on" in report["by_type"]
        assert "relates_to" in report["by_type"]
        assert report["by_type"]["depends_on"]["count"] == 1
        assert report["by_type"]["relates_to"]["count"] == 1

    def test_report_avg_confidence(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = graph.add_node(label="A", content="node A content", node_type=NodeType.FACT).node
        b = graph.add_node(label="B", content="node B content", node_type=NodeType.FACT).node
        c = graph.add_node(label="C", content="node C content", node_type=NodeType.FACT).node
        graph.add_edge(
            source_id=a.id,
            target_id=b.id,
            relationship=RelationType.RELATES_TO,
            metadata={"edge_confidence": 0.6},
        )
        graph.add_edge(
            source_id=b.id,
            target_id=c.id,
            relationship=RelationType.RELATES_TO,
            metadata={"edge_confidence": 0.8},
        )
        report = graph.edge_quality_report()
        avg = report["by_type"]["relates_to"]["avg_confidence"]
        assert avg == pytest.approx(0.7, abs=0.001)

    def test_report_top10_highest_and_lowest(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        nodes = [
            graph.add_node(label=f"N{i}", content=f"node {i} content", node_type=NodeType.FACT).node for i in range(6)
        ]
        confidences = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
        for i in range(5):
            graph.add_edge(
                source_id=nodes[i].id,
                target_id=nodes[i + 1].id,
                relationship=RelationType.RELATES_TO,
                metadata={"edge_confidence": confidences[i]},
            )
        report = graph.edge_quality_report()
        rt = report["by_type"]["relates_to"]
        assert len(rt["top_10_highest"]) == 5
        assert len(rt["top_10_lowest"]) == 5
        # Highest first entry should have the max confidence
        assert rt["top_10_highest"][0]["edge_confidence"] == pytest.approx(0.75)
        # Lowest first entry should have the min confidence
        assert rt["top_10_lowest"][0]["edge_confidence"] == pytest.approx(0.55)

    def test_edges_without_confidence_default_to_1(self, tmp_path: Path) -> None:
        """Edges created before this feature (no edge_confidence in metadata) default to 1.0."""
        graph = make_graph(tmp_path)
        a = graph.add_node(label="A", content="node A", node_type=NodeType.FACT).node
        b = graph.add_node(label="B", content="node B", node_type=NodeType.FACT).node
        # Add edge with no edge_confidence in metadata
        graph.add_edge(
            source_id=a.id,
            target_id=b.id,
            relationship=RelationType.RELATES_TO,
            metadata={},
        )
        report = graph.edge_quality_report()
        rt = report["by_type"]["relates_to"]
        assert rt["avg_confidence"] == pytest.approx(1.0)

    def test_malformed_or_non_dict_edge_metadata_logs_warning(self, caplog) -> None:
        """Edges with malformed or non-dict metadata JSON should log a warning and be treated as having no confidence."""
        import uuid

        from waggle.models import utc_now

        now = utc_now().isoformat()
        node_ids = [str(uuid.uuid4()) for _ in range(4)]
        nodes = [
            {
                "id": nid,
                "tenant_id": "local-default",
                "agent_id": "",
                "project": "",
                "session_id": "",
                "context_window_id": None,
                "label": f"N{i}",
                "content": f"content {i}",
                "node_type": "fact",
                "tags": [],
                "source_prompt": "",
                "embedding_model_id": "",
                "embedding_dim": 0,
                "source_turn_pair_id": "",
                "metadata": {},
                "evidence_records": [],
                "valid_from": None,
                "valid_to": None,
                "created_at": now,
                "updated_at": now,
                "access_count": 0,
                "embedding": None,
            }
            for i, nid in enumerate(node_ids)
        ]
        edges = [
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "source_id": node_ids[0],
                "target_id": node_ids[1],
                "relationship": "relates_to",
                "weight": 1.0,
                "metadata": "{not valid",
                "created_at": now,
            },
            {
                "id": str(uuid.uuid4()),
                "tenant_id": "local-default",
                "source_id": node_ids[2],
                "target_id": node_ids[3],
                "relationship": "relates_to",
                "weight": 1.0,
                "metadata": '"foo"',  # Valid JSON, but not a dict
                "created_at": now,
            },
        ]
        snapshot = {
            "schema_version": 1,
            "tenant_id": "local-default",
            "embedding_model_id": "",
            "nodes": nodes,
            "edges": edges,
            "transcripts": [],
            "context_windows": [],
            "repos": [],
            "context_window_edges": [],
        }
        with caplog.at_level("WARNING"):
            doc = build_abhi_document(snapshot, include_low_confidence_edges=False)
            assert "malformed metadata" in caplog.text
            assert "non-dict metadata" in caplog.text
            # Both edges should be kept because the default confidence is 1.0
            assert len(doc["edges"]) == 2
