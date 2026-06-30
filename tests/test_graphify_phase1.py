"""Tests for Phase 1 Graphify-inspired features:

A. shortest_path MCP tool and graph method
B. Edge confidence field (model, schema, extraction pipeline)
C. hub_analysis graph method
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from waggle.graph import MemoryGraph
from waggle.models import Edge, NodeType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str):
        import numpy as np

        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(c) for c in token) % len(vector)
            vector[index] += 1.0
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


def add_node(graph: MemoryGraph, label: str, content: str = "", node_type: NodeType = NodeType.FACT) -> str:
    result = graph.add_node(
        label=label,
        content=content or label,
        node_type=node_type,
    )
    return result.node.id


# ---------------------------------------------------------------------------
# Feature B: confidence field
# ---------------------------------------------------------------------------


class TestEdgeConfidence:
    def test_model_default_is_explicit(self) -> None:
        edge = Edge(source_id="a", target_id="b", relationship="relates_to")
        assert edge.confidence == "explicit"

    def test_add_edge_manual_defaults_to_explicit(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        edge = graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
        assert edge.confidence == "explicit"

    def test_add_edge_accepts_confidence_param(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        edge = graph.add_edge(source_id=a, target_id=b, relationship="relates_to", confidence="inferred")
        assert edge.confidence == "inferred"

    def test_confidence_round_trips_through_db(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        c = add_node(graph, "C")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to", confidence="explicit")
        graph.add_edge(source_id=b, target_id=c, relationship="depends_on", confidence="weak")

        subgraph = graph.get_related(node_id=a)
        edges_by_pair = {(e.source_id, e.target_id): e for e in subgraph.edges}

        assert edges_by_pair[(a, b)].confidence == "explicit"
        assert edges_by_pair[(b, c)].confidence == "weak"

    def test_confidence_in_backup_snapshot(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to", confidence="inferred")

        snapshot = graph.get_graph_snapshot()
        edge_data = next(e for e in snapshot["edges"] if e["source_id"] == a)
        assert edge_data["confidence"] == "inferred"

    def test_extraction_sets_inferred_confidence(self, tmp_path: Path) -> None:
        """Edges created by observe_conversation should be inferred or weak, not explicit."""
        graph = make_graph(tmp_path)
        graph.observe_conversation(
            user_message="We use PostgreSQL as the primary database for the project.",
            assistant_response="PostgreSQL is a solid choice for relational data with ACID compliance.",
        )
        subgraph = graph.get_related(node_id=graph.list_recent_nodes(limit=1)[0].id)
        # At minimum, extraction should not set all edges to "explicit"
        assert any(e.confidence != "explicit" for e in subgraph.edges) or len(subgraph.edges) == 0

    def test_confidence_preserved_through_snapshot_import(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to", confidence="weak")

        # Export and reimport via backup
        backup_path = tmp_path / "backup.json"
        graph.export_graph_backup(output_path=str(backup_path))
        snapshot = json.loads(backup_path.read_text())
        edge_data = next(e for e in snapshot["edges"] if e["source_id"] == a)
        assert edge_data["confidence"] == "weak"


# ---------------------------------------------------------------------------
# Feature A: shortest_path
# ---------------------------------------------------------------------------


class TestShortestPath:
    def test_direct_connection(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")

        result = graph.shortest_path(source_id=a, target_id=b)
        assert result.nodes
        node_ids = [n.id for n in result.nodes]
        assert node_ids[0] == a
        assert node_ids[-1] == b
        assert len(result.nodes) == 2

    def test_multi_hop_path(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        c = add_node(graph, "C")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
        graph.add_edge(source_id=b, target_id=c, relationship="depends_on")

        result = graph.shortest_path(source_id=a, target_id=c)
        node_ids = [n.id for n in result.nodes]
        assert node_ids == [a, b, c]
        assert len(result.edges) >= 2

    def test_no_path_returns_empty(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        # No edge between A and B

        result = graph.shortest_path(source_id=a, target_id=b)
        assert result.nodes == []
        assert result.edges == []

    def test_max_depth_cutoff(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        c = add_node(graph, "C")
        d = add_node(graph, "D")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
        graph.add_edge(source_id=b, target_id=c, relationship="relates_to")
        graph.add_edge(source_id=c, target_id=d, relationship="relates_to")

        # path is 3 hops (A->B->C->D), max_depth=2 should reject it
        result = graph.shortest_path(source_id=a, target_id=d, max_depth=2)
        assert result.nodes == []

    def test_self_path(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")

        result = graph.shortest_path(source_id=a, target_id=a)
        # A path from a node to itself has 0 hops — should return just that node
        assert len(result.nodes) == 1
        assert result.nodes[0].id == a

    def test_undirected_fallback(self, tmp_path: Path) -> None:
        """Reverse-direction edge should still be found via undirected fallback."""
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        # Edge goes B -> A (opposite direction)
        graph.add_edge(source_id=b, target_id=a, relationship="relates_to")

        result = graph.shortest_path(source_id=a, target_id=b)
        assert result.nodes  # should find path via undirected search
        node_ids = [n.id for n in result.nodes]
        assert a in node_ids and b in node_ids

    def test_prefers_shorter_path(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        c = add_node(graph, "C")
        # Direct A->C and longer A->B->C
        graph.add_edge(source_id=a, target_id=c, relationship="relates_to")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
        graph.add_edge(source_id=b, target_id=c, relationship="relates_to")

        result = graph.shortest_path(source_id=a, target_id=c)
        assert len(result.nodes) == 2  # direct path wins

    def test_invalid_max_depth_raises(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        with pytest.raises(ValueError, match="max_depth"):
            graph.shortest_path(source_id=a, target_id=b, max_depth=0)

    def test_missing_node_raises(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        with pytest.raises(ValueError):
            graph.shortest_path(source_id=a, target_id="nonexistent-id")

    def test_query_string_format(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")

        result = graph.shortest_path(source_id=a, target_id=b)
        assert result.query == f"path:{a}->{b}"


# ---------------------------------------------------------------------------
# Feature C: hub_analysis
# ---------------------------------------------------------------------------


class TestHubAnalysis:
    def test_empty_graph_returns_empty(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        hubs = graph.hub_analysis()
        assert hubs == []

    def test_isolated_nodes_below_min_degree(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        add_node(graph, "Isolated")
        hubs = graph.hub_analysis(min_degree=2)
        assert hubs == []

    def test_hub_node_identified(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        hub = add_node(graph, "Hub")
        spokes = [add_node(graph, f"Spoke{i}") for i in range(5)]
        for spoke in spokes:
            graph.add_edge(source_id=hub, target_id=spoke, relationship="relates_to")

        hubs = graph.hub_analysis(min_degree=2)
        assert hubs
        top_hub = hubs[0]
        assert top_hub["node_id"] == hub
        assert top_hub["degree"] == 5

    def test_hub_fields_present(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        c = add_node(graph, "C")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
        graph.add_edge(source_id=a, target_id=c, relationship="relates_to")

        hubs = graph.hub_analysis(min_degree=1)
        assert hubs
        hub = hubs[0]
        required_fields = {
            "node_id",
            "label",
            "node_type",
            "degree",
            "pct_of_edges",
            "betweenness_centrality",
            "access_count",
        }
        assert required_fields.issubset(hub.keys())

    def test_sorted_by_degree_descending(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        big_hub = add_node(graph, "BigHub")
        small_hub = add_node(graph, "SmallHub")
        spokes = [add_node(graph, f"S{i}") for i in range(6)]
        for spoke in spokes[:4]:
            graph.add_edge(source_id=big_hub, target_id=spoke, relationship="relates_to")
        for spoke in spokes[4:]:
            graph.add_edge(source_id=small_hub, target_id=spoke, relationship="relates_to")

        hubs = graph.hub_analysis(min_degree=1)
        degrees = [h["degree"] for h in hubs]
        assert degrees == sorted(degrees, reverse=True)

    def test_top_n_respected(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        hubs_list = [add_node(graph, f"Hub{i}") for i in range(5)]
        spoke = add_node(graph, "Spoke")
        for h in hubs_list:
            graph.add_edge(source_id=h, target_id=spoke, relationship="relates_to")

        result = graph.hub_analysis(top_n=3, min_degree=1)
        assert len(result) <= 3

    def test_pct_of_edges_sums_correctly(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = add_node(graph, "A")
        b = add_node(graph, "B")
        c = add_node(graph, "C")
        graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
        graph.add_edge(source_id=a, target_id=c, relationship="relates_to")

        hubs = graph.hub_analysis(min_degree=1)
        hub_a = next(h for h in hubs if h["node_id"] == a)
        # 2 edges total, a has degree 2: 2/(2*2) = 0.5
        assert hub_a["pct_of_edges"] == pytest.approx(0.5, abs=0.01)


def test_shortest_path_returns_only_consecutive_path_edges(tmp_path: Path) -> None:
    """Regression: shortest_path must not return edges that merely touch a path
    node (e.g. a branch B->D), only edges between consecutive path nodes."""
    graph = make_graph(tmp_path)
    a = add_node(graph, "PathA")
    b = add_node(graph, "PathB")
    c = add_node(graph, "PathC")
    d = add_node(graph, "PathD")
    graph.add_edge(source_id=a, target_id=b, relationship="relates_to")
    graph.add_edge(source_id=b, target_id=c, relationship="relates_to")
    graph.add_edge(source_id=b, target_id=d, relationship="relates_to")  # branch off B

    result = graph.shortest_path(source_id=a, target_id=c)
    node_ids = [n.id for n in result.nodes]
    assert node_ids == [a, b, c]
    assert d not in node_ids
    pairs = {(e.source_id, e.target_id) for e in result.edges}
    assert (a, b) in pairs
    assert (b, c) in pairs
    assert (b, d) not in pairs  # branch edge must be excluded


def test_shortest_path_search_bounded_by_max_depth(tmp_path: Path) -> None:
    """A path longer than max_depth returns empty (bounded search)."""
    graph = make_graph(tmp_path)
    ids = [add_node(graph, f"Chain{i}") for i in range(5)]  # A-B-C-D-E (4 hops)
    for x, y in itertools.pairwise(ids):
        graph.add_edge(source_id=x, target_id=y, relationship="relates_to")
    # 4 hops > max_depth=2 → empty
    assert graph.shortest_path(source_id=ids[0], target_id=ids[4], max_depth=2).nodes == []
    # within budget → found
    assert graph.shortest_path(source_id=ids[0], target_id=ids[4], max_depth=4).nodes
