"""Tests for Phase 2 Graphify-inspired features:

D. Persisted community clustering (recompute_communities, get_communities)
E. Neo4j Cypher export (export_cypher)
F. Graphify import bridge (graphify_bridge.import_graphify_json)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from waggle.graph import MemoryGraph
from waggle.graphify_bridge import import_graphify_json, parse_graphify_document
from waggle.models import NodeType


class FakeEmbeddingModel:
    """Deterministic, high-separation embedding for tests.

    Seeds a 64-dim pseudo-random unit vector from a hash of the full normalized
    text. Distinct texts get near-orthogonal vectors, so node dedup does not
    spuriously merge differently-labelled nodes (unlike a coarse bag-of-tokens
    model).
    """

    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str):
        import hashlib

        import numpy as np

        seed = int.from_bytes(hashlib.sha256(text.strip().lower().encode("utf-8")).digest()[:8], "big")
        rng = np.random.default_rng(seed)
        vector = rng.standard_normal(64).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector

    def to_bytes(self, embedding) -> bytes:
        import numpy as np

        return np.asarray(embedding, dtype=np.float32).tobytes()

    def from_bytes(self, data: bytes):
        import numpy as np

        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a, b) -> float:
        import numpy as np

        a_norm = float(np.linalg.norm(a))
        b_norm = float(np.linalg.norm(b))
        if a_norm == 0 or b_norm == 0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


def add_node(graph: MemoryGraph, label: str, node_type: NodeType = NodeType.FACT) -> str:
    return graph.add_node(label=label, content=label, node_type=node_type).node.id


def make_two_clusters(graph: MemoryGraph) -> tuple[list[str], list[str]]:
    """Build two clearly separated triangles (clusters) with no edges between."""
    cluster_a = [add_node(graph, f"A{i}") for i in range(3)]
    cluster_b = [add_node(graph, f"B{i}") for i in range(3)]
    # Triangle within A
    graph.add_edge(source_id=cluster_a[0], target_id=cluster_a[1], relationship="relates_to")
    graph.add_edge(source_id=cluster_a[1], target_id=cluster_a[2], relationship="relates_to")
    graph.add_edge(source_id=cluster_a[2], target_id=cluster_a[0], relationship="relates_to")
    # Triangle within B
    graph.add_edge(source_id=cluster_b[0], target_id=cluster_b[1], relationship="relates_to")
    graph.add_edge(source_id=cluster_b[1], target_id=cluster_b[2], relationship="relates_to")
    graph.add_edge(source_id=cluster_b[2], target_id=cluster_b[0], relationship="relates_to")
    return cluster_a, cluster_b


# ---------------------------------------------------------------------------
# Feature D: community clustering
# ---------------------------------------------------------------------------


class TestCommunityClustering:
    def test_empty_graph_recompute(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        stats = graph.recompute_communities()
        assert stats["cluster_count"] == 0
        assert stats["nodes_updated"] == 0

    def test_recompute_assigns_community_ids(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        cluster_a, cluster_b = make_two_clusters(graph)
        stats = graph.recompute_communities()

        assert stats["cluster_count"] >= 2
        assert stats["nodes_updated"] == 6

        # Nodes in the same triangle should share a community
        a_communities = {graph.get_node(nid).community_id for nid in cluster_a}
        b_communities = {graph.get_node(nid).community_id for nid in cluster_b}
        assert len(a_communities) == 1
        assert len(b_communities) == 1
        # The two triangles should be in different communities
        assert a_communities != b_communities

    def test_community_id_none_before_recompute(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        nid = add_node(graph, "Solo")
        assert graph.get_node(nid).community_id is None

    def test_get_communities_returns_clusters(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        make_two_clusters(graph)
        graph.recompute_communities()
        communities = graph.get_communities()

        assert len(communities) >= 2
        total_members = sum(c["member_count"] for c in communities)
        assert total_members == 6
        for c in communities:
            assert "community_id" in c
            assert "label" in c
            assert "member_count" in c

    def test_largest_community_is_id_zero(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        # Cluster of 4 + cluster of 2
        big = [add_node(graph, f"Big{i}") for i in range(4)]
        small = [add_node(graph, f"Small{i}") for i in range(2)]
        for i in range(4):
            graph.add_edge(source_id=big[i], target_id=big[(i + 1) % 4], relationship="relates_to")
        graph.add_edge(source_id=small[0], target_id=small[1], relationship="relates_to")

        graph.recompute_communities()
        big_community = graph.get_node(big[0]).community_id
        assert big_community == 0  # largest renumbered to 0

    def test_community_persists_in_snapshot(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        cluster_a, _ = make_two_clusters(graph)
        graph.recompute_communities()

        snapshot = graph.get_graph_snapshot()
        node_data = next(n for n in snapshot["nodes"] if n["id"] == cluster_a[0])
        assert node_data["community_id"] is not None
        assert "community_label" in node_data

    def test_invalid_resolution_raises(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        with pytest.raises(ValueError, match="resolution"):
            graph.recompute_communities(resolution=0)

    def test_recompute_is_idempotent_on_structure(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        cluster_a, cluster_b = make_two_clusters(graph)
        graph.recompute_communities()
        first = {nid: graph.get_node(nid).community_id for nid in cluster_a + cluster_b}
        graph.recompute_communities()
        second = {nid: graph.get_node(nid).community_id for nid in cluster_a + cluster_b}
        # Same grouping (same nodes share a community), even if ids renumber identically here
        assert (first[cluster_a[0]] == first[cluster_a[1]]) == (second[cluster_a[0]] == second[cluster_a[1]])


# ---------------------------------------------------------------------------
# Feature E: Neo4j Cypher export
# ---------------------------------------------------------------------------


class TestCypherExport:
    def test_export_creates_file(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "Alpha")
        b = add_node(graph, "Beta")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")

        result = graph.export_cypher(output_path=str(tmp_path / "out.cypher"))
        assert Path(result["output_path"]).exists()
        assert result["node_count"] == 2
        assert result["edge_count"] == 1

    def test_cypher_has_constraint_and_creates(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "Alpha")
        b = add_node(graph, "Beta")
        graph.add_edge(source_id=a, target_id=b, relationship="depends_on")

        out = Path(graph.export_cypher(output_path=str(tmp_path / "out.cypher"))["output_path"])
        text = out.read_text()
        assert "CREATE CONSTRAINT" in text
        assert "n.id IS UNIQUE" in text
        assert "CREATE (:Memory:Fact" in text
        assert "MATCH (a:Memory" in text
        assert "[:DEPENDS_ON" in text

    def test_cypher_escapes_single_quotes(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        graph.add_node(label="it's a test", content="don't break", node_type=NodeType.FACT)

        out = Path(graph.export_cypher(output_path=str(tmp_path / "out.cypher"))["output_path"])
        text = out.read_text()
        assert "\\'" in text  # escaped quote present
        # No unescaped bare CREATE line broken by quote
        assert "it\\'s a test" in text

    def test_cypher_rel_type_sanitized(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "Alpha")
        b = add_node(graph, "Beta")
        # custom relationship with spaces/punctuation
        graph.add_edge(source_id=a, target_id=b, relationship="related to (loosely)")

        out = Path(graph.export_cypher(output_path=str(tmp_path / "out.cypher"))["output_path"])
        text = out.read_text()
        # The relationship must be sanitized to a valid Cypher identifier
        # (UPPER_SNAKE, no spaces or parens) and the raw string must not leak.
        assert "[:RELATED_TO_LOOSELY" in text
        assert "related to (loosely)" not in text

    def test_cypher_includes_confidence(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "Alpha")
        b = add_node(graph, "Beta")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to", confidence="weak")

        out = Path(graph.export_cypher(output_path=str(tmp_path / "out.cypher"))["output_path"])
        assert "confidence: 'weak'" in out.read_text()

    def test_label_helper(self) -> None:
        assert MemoryGraph._cypher_label("fact") == "Fact"
        assert MemoryGraph._cypher_label("design rationale") == "DesignRationale"

    def test_rel_type_helper(self) -> None:
        assert MemoryGraph._cypher_rel_type("relates_to") == "RELATES_TO"
        assert MemoryGraph._cypher_rel_type("calls method") == "CALLS_METHOD"


# ---------------------------------------------------------------------------
# Feature F: Graphify import bridge
# ---------------------------------------------------------------------------


GRAPHIFY_SAMPLE = {
    "nodes": [
        {
            "id": "fn:auth",
            "name": "authenticate",
            "type": "function",
            "file": "auth.py",
            "signature": "def authenticate(user)",
        },
        {"id": "cls:User", "name": "User", "type": "class", "file": "models.py"},
        {"id": "doc:readme", "name": "README", "type": "doc", "description": "Project overview"},
    ],
    "edges": [
        {"source": "fn:auth", "target": "cls:User", "type": "uses", "confidence": "EXTRACTED"},
        {"source": "doc:readme", "target": "fn:auth", "type": "documents", "confidence": "INFERRED"},
        {"source": "fn:auth", "target": "missing:node", "type": "calls", "confidence": "AMBIGUOUS"},
    ],
}


class TestGraphifyParse:
    def test_parse_maps_node_types(self) -> None:
        nodes, _ = parse_graphify_document(GRAPHIFY_SAMPLE)
        by_id = {n["graphify_id"]: n for n in nodes}
        assert by_id["fn:auth"]["node_type"] == NodeType.ENTITY
        assert by_id["cls:User"]["node_type"] == NodeType.ENTITY
        assert by_id["doc:readme"]["node_type"] == NodeType.CONCEPT

    def test_parse_maps_edge_relationships(self) -> None:
        _, edges = parse_graphify_document(GRAPHIFY_SAMPLE)
        rels = {(e["source"], e["target"]): e for e in edges}
        assert str(rels[("fn:auth", "cls:User")]["relationship"]) == "depends_on"
        assert str(rels[("doc:readme", "fn:auth")]["relationship"]) == "derived_from"

    def test_parse_maps_confidence(self) -> None:
        _, edges = parse_graphify_document(GRAPHIFY_SAMPLE)
        rels = {(e["source"], e["target"]): e for e in edges}
        assert rels[("fn:auth", "cls:User")]["confidence"] == "explicit"  # EXTRACTED
        assert rels[("doc:readme", "fn:auth")]["confidence"] == "inferred"  # INFERRED
        assert rels[("fn:auth", "missing:node")]["confidence"] == "weak"  # AMBIGUOUS

    def test_parse_tolerates_field_aliases(self) -> None:
        doc = {
            "entities": [{"key": "x", "title": "X", "kind": "module"}],
            "links": [{"from": "x", "to": "x", "label": "imports", "tag": "EXTRACTED"}],
        }
        nodes, edges = parse_graphify_document(doc)
        assert nodes[0]["graphify_id"] == "x"
        assert nodes[0]["node_type"] == NodeType.ENTITY
        assert edges[0]["source"] == "x"


class TestGraphifyImport:
    def _write_sample(self, tmp_path: Path) -> Path:
        path = tmp_path / "graph.json"
        path.write_text(json.dumps(GRAPHIFY_SAMPLE), encoding="utf-8")
        return path

    def test_import_creates_nodes(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        result = import_graphify_json(graph, self._write_sample(tmp_path), project="myrepo")
        assert result.nodes_created == 3

    def test_import_creates_edges_skips_dangling(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        result = import_graphify_json(graph, self._write_sample(tmp_path))
        # 3 edges in source, but one references a missing node -> 2 created
        assert result.edges_created == 2

    def test_imported_nodes_tagged_graphify(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        import_graphify_json(graph, self._write_sample(tmp_path))
        recent = graph.list_recent_nodes(limit=10)
        assert any("graphify" in (n.tags or []) for n in recent)
        assert all(n.metadata.get("graphify_source") is True for n in recent if "graphify" in (n.tags or []))

    def test_import_assigns_scope(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        import_graphify_json(graph, self._write_sample(tmp_path), project="proj-x", agent_id="agent-y")
        recent = graph.list_recent_nodes(limit=10)
        graphify_nodes = [n for n in recent if "graphify" in (n.tags or [])]
        assert graphify_nodes
        assert all(n.project == "proj-x" for n in graphify_nodes)
        assert all(n.agent_id == "agent-y" for n in graphify_nodes)

    def test_import_edge_confidence_mapped(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        import_graphify_json(graph, self._write_sample(tmp_path))
        # Read edges from the snapshot (what the UI consumes) and check mapping.
        snapshot = graph.get_graph_snapshot()
        confidences = {e["confidence"] for e in snapshot["edges"]}
        # EXTRACTED -> explicit and INFERRED -> inferred should both be present
        assert "explicit" in confidences
        assert "inferred" in confidences

    def test_reimport_reuses_nodes(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        sample = self._write_sample(tmp_path)
        import_graphify_json(graph, sample)
        result2 = import_graphify_json(graph, sample)
        # Second import should reuse existing nodes (dedup), not create duplicates
        assert result2.nodes_updated >= 1
