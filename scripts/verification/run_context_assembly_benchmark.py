#!/usr/bin/env python3
"""
Benchmark the context assembly refactoring improvements.
Measures: support coverage, synthesis quality, temporal handling, noise resistance.
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType


class FakeEmbeddingModel(EmbeddingModel):
    """Deterministic fake embedding for testing."""

    def __init__(self):
        self.counter = 0

    def embed(self, text: str) -> np.ndarray:
        # Deterministic based on text hash
        h = hash(text) % 1000
        return np.array([h/1000, (h+1)/1000, (h+2)/1000], dtype=np.float32)

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        # Real cosine similarity
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))


def benchmark_support_coverage():
    """Benchmark: decision + reason must co-appear."""
    print("\n" + "="*70)
    print("BENCHMARK 1: Support Coverage (decision + reason)")
    print("="*70)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        graph = MemoryGraph(db_path, embedding_model=FakeEmbeddingModel())

        # Create decision + reason network
        decision = graph.add_node(
            label="Use PostgreSQL",
            content="We chose PostgreSQL for ACID compliance and scalability",
            node_type=NodeType.DECISION,
        ).node

        reason = graph.add_node(
            label="ACID requirements",
            content="Our application requires ACID transaction guarantees",
            node_type=NodeType.CONCEPT,
        ).node

        # Add noise: similar decisions
        for i in range(5):
            graph.add_node(
                label=f"Database option {i}",
                content=f"Considered {['MySQL', 'SQLite', 'MongoDB', 'DynamoDB', 'Cassandra'][i]}",
                node_type=NodeType.CONCEPT,
            )

        # Connect decision to reason with DEPENDS_ON
        graph.add_edge(
            source_id=decision.id,
            target_id=reason.id,
            relationship=RelationType.DEPENDS_ON,
            weight=0.95,
        )

        # Query for decision
        result = graph.query(query="what database did we choose", max_nodes=3)

        result_ids = {n.id for n in result.nodes}
        decision_found = decision.id in result_ids
        reason_found = reason.id in result_ids
        both_found = decision_found and reason_found

        print("\nQuery: 'what database did we choose'")
        print(f"Nodes returned: {len(result.nodes)}")
        print(f"  Decision found: {'✓' if decision_found else '✗'}")
        print(f"  Reason found: {'✓' if reason_found else '✗'}")
        print(f"  Both (support coverage): {'✓ PASS' if both_found else '✗ FAIL'}")

        return 1 if both_found else 0


def benchmark_contradiction_handling():
    """Benchmark: old + new decisions + updates edge must appear."""
    print("\n" + "="*70)
    print("BENCHMARK 2: Contradiction Handling (old + new + updates)")
    print("="*70)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        graph = MemoryGraph(db_path, embedding_model=FakeEmbeddingModel())

        # Create decision evolution
        old_decision = graph.add_node(
            label="Use SQLite",
            content="Original decision: SQLite for local development",
            node_type=NodeType.DECISION,
        ).node

        new_decision = graph.add_node(
            label="Use PostgreSQL",
            content="Updated decision: PostgreSQL for production",
            node_type=NodeType.DECISION,
        ).node

        reason = graph.add_node(
            label="Scalability issue",
            content="SQLite couldn't handle concurrent writes",
            node_type=NodeType.FACT,
        ).node

        # Add noise
        for i in range(4):
            graph.add_node(
                label=f"Other decision {i}",
                content=f"Unrelated decision about {['caching', 'auth', 'logging', 'monitoring'][i]}",
                node_type=NodeType.DECISION,
            )

        # Connect with UPDATES
        graph.add_edge(
            source_id=new_decision.id,
            target_id=old_decision.id,
            relationship=RelationType.UPDATES,
            weight=0.90,
        )

        graph.add_edge(
            source_id=new_decision.id,
            target_id=reason.id,
            relationship=RelationType.DERIVED_FROM,
            weight=0.85,
        )

        # Query for what changed
        result = graph.query(query="what changed about the database", max_nodes=4)

        result_ids = {n.id for n in result.nodes}
        has_old = old_decision.id in result_ids
        has_new = new_decision.id in result_ids
        has_reason = reason.id in result_ids
        has_both_decisions = has_old and has_new

        # Check for updates edge
        has_updates_edge = any(
            e.relationship == RelationType.UPDATES
            for e in result.edges
        )

        print("\nQuery: 'what changed about the database'")
        print(f"Nodes returned: {len(result.nodes)}")
        print(f"  Old decision found: {'✓' if has_old else '✗'}")
        print(f"  New decision found: {'✓' if has_new else '✗'}")
        print(f"  Both decisions: {'✓' if has_both_decisions else '✗'}")
        print(f"  Updates edge present: {'✓' if has_updates_edge else '✗'}")
        print(f"  Overall: {'✓ PASS' if (has_both_decisions and has_updates_edge) else '✗ FAIL'}")

        return 1 if (has_both_decisions and has_updates_edge) else 0


def benchmark_dependency_chain():
    """Benchmark: complex dependency chains preserve context."""
    print("\n" + "="*70)
    print("BENCHMARK 3: Dependency Chain (decision → reason → requirement)")
    print("="*70)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        graph = MemoryGraph(db_path, embedding_model=FakeEmbeddingModel())

        # Create chain: decision → reason → requirement
        decision = graph.add_node(
            label="FastAPI backend",
            content="Chose FastAPI for async support",
            node_type=NodeType.DECISION,
        ).node

        reason = graph.add_node(
            label="Real-time requirements",
            content="Application needs real-time WebSocket support",
            node_type=NodeType.CONCEPT,
        ).node

        requirement = graph.add_node(
            label="Concurrency model",
            content="Need support for thousands of concurrent connections",
            node_type=NodeType.FACT,
        ).node

        # Add noise
        for i in range(6):
            graph.add_node(
                label=f"Framework option {i}",
                content=f"Alternative: {['Flask', 'Django', 'Starlette', 'Quart', 'Tornado', 'aiohttp'][i]}",
                node_type=NodeType.CONCEPT,
            )

        # Chain connections
        graph.add_edge(
            source_id=decision.id,
            target_id=reason.id,
            relationship=RelationType.DEPENDS_ON,
            weight=0.95,
        )

        graph.add_edge(
            source_id=reason.id,
            target_id=requirement.id,
            relationship=RelationType.DERIVED_FROM,
            weight=0.90,
        )

        # Query
        result = graph.query(query="why FastAPI", max_nodes=5)

        result_ids = {n.id for n in result.nodes}
        has_decision = decision.id in result_ids
        has_reason = reason.id in result_ids
        has_requirement = requirement.id in result_ids
        chain_complete = has_decision and has_reason and has_requirement

        print("\nQuery: 'why FastAPI'")
        print(f"Nodes returned: {len(result.nodes)}")
        print(f"  Decision (FastAPI): {'✓' if has_decision else '✗'}")
        print(f"  Reason (real-time): {'✓' if has_reason else '✗'}")
        print(f"  Requirement (concurrency): {'✓' if has_requirement else '✗'}")
        print(f"  Complete chain: {'✓ PASS' if chain_complete else '✗ FAIL'}")

        return 1 if chain_complete else 0


def benchmark_noise_resistance():
    """Benchmark: noise doesn't overwhelm core context."""
    print("\n" + "="*70)
    print("BENCHMARK 4: Noise Resistance (10 noise nodes vs. core)")
    print("="*70)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        graph = MemoryGraph(db_path, embedding_model=FakeEmbeddingModel())

        # Create core context
        decision = graph.add_node(
            label="REST API design",
            content="Chose REST over GraphQL for API design",
            node_type=NodeType.DECISION,
        ).node

        reason = graph.add_node(
            label="Team expertise",
            content="Team is more familiar with REST patterns",
            node_type=NodeType.CONCEPT,
        ).node

        graph.add_edge(
            source_id=decision.id,
            target_id=reason.id,
            relationship=RelationType.DEPENDS_ON,
            weight=0.95,
        )

        # Add 10 noise nodes (weakly related tutorials)
        noise_nodes = []
        for i in range(10):
            noise = graph.add_node(
                label=f"REST tutorial {i}",
                content=f"Tutorial on REST principles and best practices (part {i})",
                node_type=NodeType.CONCEPT,
            ).node
            noise_nodes.append(noise)
            graph.add_edge(
                source_id=decision.id,
                target_id=noise.id,
                relationship=RelationType.SIMILAR_TO,
                weight=0.40,  # Weak weight
            )

        # Query
        result = graph.query(query="REST API decision", max_nodes=4)

        result_ids = {n.id for n in result.nodes}
        has_decision = decision.id in result_ids
        has_reason = reason.id in result_ids
        core_found = has_decision and has_reason

        # Count how many noise nodes made it in
        noise_ids_set = {n.id for n in noise_nodes}
        noise_in_result = len(noise_ids_set & result_ids)

        print("\nQuery: 'REST API decision'")
        print(f"Nodes returned: {len(result.nodes)} (max_nodes=4)")
        print(f"  Core decision found: {'✓' if has_decision else '✗'}")
        print(f"  Core reason found: {'✓' if has_reason else '✗'}")
        print(f"  Noise nodes included: {noise_in_result}/10")
        print(f"  Core preserved: {'✓ PASS' if (core_found and noise_in_result < 2) else '✗ FAIL'}")

        return 1 if (core_found and noise_in_result < 2) else 0


def main():
    print("\n" + "█"*70)
    print("█ CONTEXT ASSEMBLY BENCHMARK SUITE")
    print("█ Testing refactoring improvements:")
    print("█   - Support coverage (decision+reason co-appearance)")
    print("█   - Contradiction handling (old+new+edge)")
    print("█   - Dependency chains (multi-hop context)")
    print("█   - Noise resistance (core vs. weak relations)")
    print("█"*70)

    start = time.time()

    results = []
    results.append(("Support Coverage", benchmark_support_coverage()))
    results.append(("Contradiction Handling", benchmark_contradiction_handling()))
    results.append(("Dependency Chain", benchmark_dependency_chain()))
    results.append(("Noise Resistance", benchmark_noise_resistance()))

    elapsed = time.time() - start

    passed = sum(r[1] for r in results)
    total = len(results)

    print("\n" + "="*70)
    print("RESULTS")
    print("="*70)
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{name:.<50} {status}")

    print(f"\nTotal: {passed}/{total} passed ({100*passed//total}%)")
    print(f"Time: {elapsed:.2f}s")
    print("="*70 + "\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
