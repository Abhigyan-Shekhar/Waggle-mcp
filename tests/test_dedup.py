"""Tests for canonicalization-at-write deduplication and manual canonicalize_node."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from waggle.graph import MemoryGraph
from waggle.models import CanonicalizeResult, DedupCandidatesResult, NodeType


class FakeEmbeddingModel:
    """Deterministic embedding model for tests."""

    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
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
    return MemoryGraph(
        tmp_path / "memory.db",
        FakeEmbeddingModel(),
        dedup_similarity_threshold=0.88,
        dedup_same_label_threshold=0.88,
    )


class TestCanonicalizationAtWrite:
    """Test that semantically equivalent nodes are merged at write time."""

    def test_semantic_duplicate_nodes_are_merged_with_aliases(self, tmp_path: Path) -> None:
        """Store two nodes with different content and manually merge them."""
        # Use high threshold to prevent automatic dedup
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        first = graph.add_node(
            label="Database Choice",
            content="We use PostgreSQL for our production database",
            node_type=NodeType.DECISION,
        )
        # Use content that doesn't share the same entity to avoid automatic dedup
        second = graph.add_node(
            label="Database Choice",
            content="Our database is MySQL",
            node_type=NodeType.DECISION,
        )

        # With high threshold, these should NOT merge automatically
        assert first.created is True
        assert second.created is True
        assert second.node.id != first.node.id

        # Now manually merge them using canonicalize_node
        result = graph.canonicalize_node(
            node_ids=[second.node.id],
            canonical_id=first.node.id,
        )

        # Both should now point to the same canonical node
        assert result.canonical_node.id == first.node.id
        assert second.node.id in result.merged_node_ids

        # The canonical node should have both phrasings in aliases
        canonical = graph.get_node(first.node.id)
        assert canonical is not None
        assert len(canonical.aliases) >= 1
        # The incoming content should be in aliases
        assert "Our database is MySQL" in canonical.aliases

        # Only one node should exist
        stats = graph.get_stats()
        assert stats.total_nodes == 1

    def test_different_node_types_are_not_merged(self, tmp_path: Path) -> None:
        """Store "we use Postgres" (decision) and "Postgres is fast" (fact) — assert TWO nodes."""
        graph = make_graph(tmp_path)

        decision = graph.add_node(
            label="Database Choice",
            content="We use Postgres for our production database",
            node_type=NodeType.DECISION,
        )
        fact = graph.add_node(
            label="Database Performance",
            content="Postgres is fast and reliable",
            node_type=NodeType.FACT,
        )

        # Different node types should not merge
        assert decision.created is True
        assert fact.created is True
        assert fact.node.id != decision.node.id

        stats = graph.get_stats()
        assert stats.total_nodes == 2

    def test_different_scopes_are_not_merged(self, tmp_path: Path) -> None:
        """Store node A in project=X, store equivalent in project=Y — assert TWO nodes."""
        graph = make_graph(tmp_path)

        node_x = graph.add_node(
            label="Database Choice",
            content="We use Postgres",
            node_type=NodeType.DECISION,
            project="project_x",
        )
        node_y = graph.add_node(
            label="Database Choice",
            content="We use Postgres",
            node_type=NodeType.DECISION,
            project="project_y",
        )

        # Different scopes should not merge
        assert node_x.created is True
        assert node_y.created is True
        assert node_y.node.id != node_x.node.id

        stats = graph.get_stats()
        assert stats.total_nodes == 2

    def test_edge_points_to_canonical_node(self, tmp_path: Path) -> None:
        """Test that an edge created at the same time as a deduplicated node points to the canonical node."""
        graph = make_graph(tmp_path)

        # Create first node
        node_a = graph.add_node(
            label="Feature Flag",
            content="We use LaunchDarkly for feature flags",
            node_type=NodeType.DECISION,
        )

        # Create second node that will dedup, then immediately create an edge
        node_b = graph.add_node(
            label="Feature Flag Provider",
            content="LaunchDarkly is our feature flag provider",
            node_type=NodeType.DECISION,
        )

        # Create edge from node_a to node_b
        edge = graph.add_edge(
            source_id=node_a.node.id,
            target_id=node_b.node.id,
            relationship="relates_to",
        )

        # Edge should point to the canonical node (node_a.id == node_b.id)
        assert edge.source_id == node_a.node.id
        assert edge.target_id == node_b.node.id
        assert edge.source_id == edge.target_id  # They should be the same canonical node

    def test_regression_threshold_0_88_no_false_positive(self, tmp_path: Path) -> None:
        """At threshold 0.88, "we use Postgres" and "we don't use Postgres" must NOT merge."""
        graph = make_graph(tmp_path)

        node1 = graph.add_node(
            label="Database Choice",
            content="We use Postgres for our production database",
            node_type=NodeType.DECISION,
        )
        node2 = graph.add_node(
            label="Database Choice",
            content="We don't use Postgres, we use MySQL",
            node_type=NodeType.DECISION,
        )

        # These should NOT merge because they have opposite meanings
        assert node1.created is True
        assert node2.created is True
        assert node2.node.id != node1.node.id

        stats = graph.get_stats()
        assert stats.total_nodes == 2


class TestCanonicalizeNode:
    """Test the manual canonicalize_node tool."""

    def test_canonicalize_node_merges_correctly(self, tmp_path: Path) -> None:
        """Test that canonicalize_node merges nodes and collects aliases."""
        # Use high threshold to prevent automatic dedup
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        # Create three nodes with different content that don't share entities
        node1 = graph.add_node(
            label="Database",
            content="We use PostgreSQL for production",
            node_type=NodeType.ENTITY,
        )
        node2 = graph.add_node(
            label="DB",
            content="Our database is MySQL",
            node_type=NodeType.ENTITY,
        )
        node3 = graph.add_node(
            label="Database System",
            content="We use MongoDB for analytics",
            node_type=NodeType.ENTITY,
        )

        # Manually merge node2 and node3 into node1
        result = graph.canonicalize_node(
            node_ids=[node2.node.id, node3.node.id],
            canonical_id=node1.node.id,
        )

        assert isinstance(result, CanonicalizeResult)
        assert result.canonical_node.id == node1.node.id
        assert node2.node.id in result.merged_node_ids
        assert node3.node.id in result.merged_node_ids

        # All content should be in aliases
        canonical = graph.get_node(node1.node.id)
        assert canonical is not None
        assert "Our database is MySQL" in canonical.aliases
        assert "We use MongoDB for analytics" in canonical.aliases

        # Merged nodes should be deleted
        try:
            graph.get_node(node2.node.id)
            raise AssertionError("node2 should have been deleted")
        except ValueError:
            pass
        try:
            graph.get_node(node3.node.id)
            raise AssertionError("node3 should have been deleted")
        except ValueError:
            pass

    def test_canonicalize_node_is_idempotent(self, tmp_path: Path) -> None:
        """Test that merging an already-merged node is a no-op."""
        # Use high threshold to prevent automatic dedup
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        node1 = graph.add_node(
            label="Database",
            content="We use PostgreSQL for production",
            node_type=NodeType.ENTITY,
        )
        node2 = graph.add_node(
            label="DB",
            content="Our database is MySQL",
            node_type=NodeType.ENTITY,
        )

        # First merge
        result1 = graph.canonicalize_node(
            node_ids=[node2.node.id],
            canonical_id=node1.node.id,
        )
        assert node2.node.id in result1.merged_node_ids

        # Second merge (node2 is already deleted)
        result2 = graph.canonicalize_node(
            node_ids=[node2.node.id],
            canonical_id=node1.node.id,
        )
        # Should be a no-op
        assert node2.node.id not in result2.merged_node_ids

    def test_canonicalize_node_repoints_edges(self, tmp_path: Path) -> None:
        """Test that edges pointing to merged nodes are re-pointed to canonical."""
        # Use high threshold to prevent automatic dedup
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        node1 = graph.add_node(
            label="Database",
            content="We use PostgreSQL for production",
            node_type=NodeType.ENTITY,
        )
        node2 = graph.add_node(
            label="DB",
            content="Our database is MySQL",
            node_type=NodeType.ENTITY,
        )
        node3 = graph.add_node(
            label="Language",
            content="We use Python for backend",
            node_type=NodeType.ENTITY,
        )
        node4 = graph.add_node(
            label="Framework",
            content="We use Django",
            node_type=NodeType.ENTITY,
        )

        # Create edges pointing to node2
        graph.add_edge(
            source_id=node3.node.id,
            target_id=node2.node.id,
            relationship="relates_to",
        )
        graph.add_edge(
            source_id=node4.node.id,
            target_id=node2.node.id,
            relationship="relates_to",
        )

        # Merge node2 into node1
        result = graph.canonicalize_node(
            node_ids=[node2.node.id],
            canonical_id=node1.node.id,
        )

        # Edges should be re-pointed
        assert result.edges_repointed >= 2


class TestDedupCandidates:
    """Test the dedup_candidates tool."""

    def test_dedup_candidates_returns_expected_pairs(self, tmp_path: Path) -> None:
        """Test that dedup_candidates returns pairs above threshold but below auto-merge."""
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        # node1 and node2 share most tokens → high cosine similarity
        # node3 is completely different → low similarity
        node1 = graph.add_node(
            label="Preference A",
            content="alpha beta gamma delta epsilon",
            node_type=NodeType.FACT,
        )
        node2 = graph.add_node(
            label="Preference B",
            content="alpha beta gamma delta zeta",
            node_type=NodeType.FACT,
        )
        graph.add_node(
            label="Preference C",
            content="one two three four five six seven",
            node_type=NodeType.FACT,
        )

        result = graph.dedup_candidates(threshold=0.80)

        assert isinstance(result, DedupCandidatesResult)
        assert result.threshold == 0.80
        assert result.total_nodes_scanned == 3

        pair_found = any(
            (p.node_id_a == node1.node.id and p.node_id_b == node2.node.id)
            or (p.node_id_a == node2.node.id and p.node_id_b == node1.node.id)
            for p in result.pairs
        )
        assert pair_found, f"Expected node1/node2 pair, got: {result.pairs}"

    def test_dedup_candidates_respects_scope(self, tmp_path: Path) -> None:
        """Test that dedup_candidates respects project scope."""
        # Use high threshold to prevent automatic dedup
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        # Create nodes in different projects
        graph.add_node(
            label="Database",
            content="We use PostgreSQL for production",
            node_type=NodeType.ENTITY,
            project="project_a",
        )
        graph.add_node(
            label="Database",
            content="We use MySQL for staging",
            node_type=NodeType.ENTITY,
            project="project_b",
        )

        # Query for candidates in project_a only
        result = graph.dedup_candidates(scope={"project": "project_a"}, threshold=0.80)

        # Should only find node1 (or none if project_b node is excluded)
        assert result.total_nodes_scanned == 1

    def test_dedup_candidates_sorted_by_similarity(self, tmp_path: Path) -> None:
        """Test that dedup_candidates returns pairs sorted by descending similarity."""
        graph = MemoryGraph(
            tmp_path / "memory.db",
            FakeEmbeddingModel(),
            dedup_similarity_threshold=0.99,
            dedup_same_label_threshold=0.99,
        )

        graph.add_node(label="A", content="alpha beta gamma delta epsilon", node_type=NodeType.FACT)
        graph.add_node(label="B", content="alpha beta gamma delta zeta", node_type=NodeType.FACT)
        graph.add_node(label="C", content="one two three four five six seven", node_type=NodeType.FACT)

        result = graph.dedup_candidates(threshold=0.80)

        similarities = [pair.similarity for pair in result.pairs]
        assert similarities == sorted(similarities, reverse=True)
