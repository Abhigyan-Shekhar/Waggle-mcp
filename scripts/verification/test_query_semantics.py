#!/usr/bin/env python3
"""
Test script to validate graph traversal and query semantics.

Tests:
1. Decision recall - can query recover decision + reason
2. Reasoning chain - decision depends_on reason is followed
3. Contradiction handling - old vs new decisions are connected
4. Noise resistance - similarity_to edges don't dominate
"""

import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType


class FakeEmbeddingModel:
    """Minimal embedding for testing."""
    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(c) for c in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm > 0 else vector

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm, b_norm = np.linalg.norm(a), np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def test_decision_recall():
    """Test 1: Decision recall - query 'what did we decide about database'"""
    print("\n" + "=" * 70)
    print("TEST 1: Decision Recall")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        graph = MemoryGraph(Path(tmpdir) / "memory.db", FakeEmbeddingModel())

        # Create decision node
        decision = graph.add_node(
            label="Database decision",
            content="We decided to use PostgreSQL for the main database",
            node_type=NodeType.DECISION,
            tags=["database", "architecture"],
        ).node

        # Create reason node
        reason = graph.add_node(
            label="Database requirements",
            content="PostgreSQL supports advanced queries and JSONB storage",
            node_type=NodeType.CONCEPT,
            tags=["database", "requirements"],
        ).node

        # Connect with depends_on (strong relationship)
        graph.add_edge(
            source_id=decision.id,
            target_id=reason.id,
            relationship=RelationType.DEPENDS_ON,
            weight=1.0,
        )

        # Query for decision context
        result = graph.query(query="what did we decide about database", max_nodes=10, max_depth=2)

        result_ids = {n.id for n in result.nodes}
        result_labels = {n.label for n in result.nodes}

        print("Query: 'what did we decide about database'")
        print(f"Found {len(result.nodes)} nodes:")
        for node in result.nodes:
            print(f"  - {node.label} ({node.node_type.value})")

        # Verify
        has_decision = decision.id in result_ids
        has_reason = reason.id in result_ids
        has_edge = any(
            e.source_id == decision.id and e.target_id == reason.id
            for e in result.edges
        )

        print(f"\n✓ Decision node found: {has_decision}")
        print(f"✓ Reason node found: {has_reason}")
        print(f"✓ Edge between them: {has_edge}")

        return has_decision and has_reason and has_edge


def test_reasoning_chain():
    """Test 2: Reasoning chain - depends_on edges are traversed"""
    print("\n" + "=" * 70)
    print("TEST 2: Reasoning Chain (depends_on traversal)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        graph = MemoryGraph(Path(tmpdir) / "memory.db", FakeEmbeddingModel())

        # Create chain: decision -> depends_on -> reason -> derives_from -> background
        decision = graph.add_node(
            label="FastAPI choice",
            content="We chose FastAPI for async support and performance",
            node_type=NodeType.DECISION,
        ).node

        reason = graph.add_node(
            label="Async requirement",
            content="The project needs async support for real-time features",
            node_type=NodeType.CONCEPT,
        ).node

        background = graph.add_node(
            label="Real-time WebSocket",
            content="Real-time WebSocket connections require async handling",
            node_type=NodeType.FACT,
        ).node

        # Connect with strong relationships
        graph.add_edge(
            source_id=decision.id,
            target_id=reason.id,
            relationship=RelationType.DEPENDS_ON,
            weight=1.0,
        )

        graph.add_edge(
            source_id=reason.id,
            target_id=background.id,
            relationship=RelationType.DERIVED_FROM,
            weight=1.0,
        )

        # Query for reasoning
        result = graph.query(query="why did we choose FastAPI", max_nodes=10, max_depth=2)

        result_ids = {n.id for n in result.nodes}
        result_labels = {n.label for n in result.nodes}

        print("Query: 'why did we choose FastAPI'")
        print(f"Found {len(result.nodes)} nodes:")
        for node in result.nodes:
            print(f"  - {node.label} ({node.node_type.value})")

        # Verify
        has_decision = decision.id in result_ids
        has_reason = reason.id in result_ids

        print(f"\n✓ Decision node found: {has_decision}")
        print(f"✓ Reason node found: {has_reason}")

        return has_decision and has_reason


def test_contradiction_handling():
    """Test 3: Contradiction - old vs new decisions connected"""
    print("\n" + "=" * 70)
    print("TEST 3: Contradiction Handling")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        graph = MemoryGraph(Path(tmpdir) / "memory.db", FakeEmbeddingModel())

        # Create old decision
        old_decision = graph.add_node(
            label="Original database",
            content="We initially chose MySQL for the database",
            node_type=NodeType.DECISION,
        ).node

        # Create new decision
        new_decision = graph.add_node(
            label="New database",
            content="We switched to PostgreSQL for better JSON support",
            node_type=NodeType.DECISION,
        ).node

        # Connect with updates (strong relationship)
        graph.add_edge(
            source_id=new_decision.id,
            target_id=old_decision.id,
            relationship=RelationType.UPDATES,
            weight=1.0,
        )

        # Query for database changes
        result = graph.query(query="what changed about database", max_nodes=10, max_depth=2)

        result_ids = {n.id for n in result.nodes}

        print("Query: 'what changed about database'")
        print(f"Found {len(result.nodes)} nodes:")
        for node in result.nodes:
            print(f"  - {node.label} ({node.node_type.value})")

        # Verify
        has_old = old_decision.id in result_ids
        has_new = new_decision.id in result_ids
        has_edge = any(
            (e.source_id == new_decision.id and e.target_id == old_decision.id
             and e.relationship == RelationType.UPDATES)
            for e in result.edges
        )

        print(f"\n✓ Old decision found: {has_old}")
        print(f"✓ New decision found: {has_new}")
        print(f"✓ Updates edge found: {has_edge}")

        return has_old and has_new and has_edge


def test_noise_resistance():
    """Test 4: Noise resistance - similar_to edges don't dominate"""
    print("\n" + "=" * 70)
    print("TEST 4: Noise Resistance (similar_to pruning)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        graph = MemoryGraph(Path(tmpdir) / "memory.db", FakeEmbeddingModel())

        # Create core decision
        decision = graph.add_node(
            label="Framework decision",
            content="We chose FastAPI as our async web framework",
            node_type=NodeType.DECISION,
        ).node

        # Create reason
        reason = graph.add_node(
            label="Async requirements",
            content="Async support is critical for our real-time features",
            node_type=NodeType.CONCEPT,
        ).node

        # Connect with depends_on (strong relationship, weight 0.85)
        graph.add_edge(
            source_id=decision.id,
            target_id=reason.id,
            relationship=RelationType.DEPENDS_ON,
            weight=1.0,
        )

        # Add 10 "noise" similar_to nodes (low priority, weight 0.30)
        for i in range(10):
            noise = graph.add_node(
                label=f"Tutorial number {i}",
                content=f"This is an unrelated FastAPI tutorial snippet {i}",
                node_type=NodeType.NOTE,
                tags=["tutorial"],
            ).node

            graph.add_edge(
                source_id=decision.id,
                target_id=noise.id,
                relationship=RelationType.SIMILAR_TO,
                weight=0.5,
            )

        # Query for the actual decision reasoning (high priority should pull reason first)
        result = graph.query(query="requirements for framework decision", max_nodes=5, max_depth=2)

        result_labels = {n.label for n in result.nodes}
        tutorial_count = sum(1 for label in result_labels if "tutorial" in label.lower())

        print("Query: 'requirements for framework decision'")
        print(f"Found {len(result.nodes)} nodes (max_nodes=5):")
        for node in result.nodes:
            print(f"  - {node.label} ({node.node_type.value})")

        has_decision = "Framework decision" in result_labels
        has_reason = "Async requirements" in result_labels
        # With proper weighting, depends_on (0.85 * decay) should beat similar_to (0.30 * decay)
        reason_found = has_reason

        print(f"\n✓ Decision found: {has_decision}")
        print(f"✓ Reason found (depends_on priority): {has_reason}")
        print(f"✓ Tutorial count: {tutorial_count}")

        return has_decision and has_reason


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("GRAPH TRAVERSAL & QUERY SEMANTICS VALIDATION")
    print("=" * 70)

    results = []

    try:
        results.append(("Decision Recall", test_decision_recall()))
    except Exception as e:
        print(f"\n✗ Test 1 failed: {e}")
        results.append(("Decision Recall", False))

    try:
        results.append(("Reasoning Chain", test_reasoning_chain()))
    except Exception as e:
        print(f"\n✗ Test 2 failed: {e}")
        results.append(("Reasoning Chain", False))

    try:
        results.append(("Contradiction Handling", test_contradiction_handling()))
    except Exception as e:
        print(f"\n✗ Test 3 failed: {e}")
        results.append(("Contradiction Handling", False))

    try:
        results.append(("Noise Resistance", test_noise_resistance()))
    except Exception as e:
        print(f"\n✗ Test 4 failed: {e}")
        results.append(("Noise Resistance", False))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")

    all_passed = all(passed for _, passed in results)
    print(f"\nOverall: {'✓ ALL TESTS PASSED' if all_passed else '✗ SOME TESTS FAILED'}")
    exit(0 if all_passed else 1)
