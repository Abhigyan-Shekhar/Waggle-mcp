#!/usr/bin/env python3
"""
Generate canonical .abhi fixture files for the abhi-diff-merge-tool test suite.

This script is NOT run at test time. Run it manually when the .abhi format
changes and the fixtures need to be regenerated:

    PYTHONPATH=src python3 scripts/generate_abhi_fixtures.py

The six fixtures written to tests/fixtures/abhi/:
  empty.abhi              — zero nodes, zero edges
  single-node.abhi        — one node, zero edges
  linear-history.abhi     — 10 nodes, 9 edges in a linear sequence
  branched.abhi           — 20 nodes, 25 edges with branching structure
  with-contradictions.abhi — at least one CONTRADICTS edge
  with-dangling-edges.abhi — at least one edge whose target node is absent
                             (intentionally invalid)
"""
from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

# Ensure src/ is on the path when run as PYTHONPATH=src python3 ...
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from waggle.abhi import write_abhi_document

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "abhi"


def _node(label: str, content: str, node_type: str = "fact", **kwargs) -> dict:
    return {
        "id": str(uuid4()),
        "label": label,
        "content": content,
        "node_type": node_type,
        "tags": [],
        "aliases": [],
        "metadata": {},
        **kwargs,
    }


def _edge(source_id: str, target_id: str, relationship: str = "relates_to", **kwargs) -> dict:
    return {
        "id": str(uuid4()),
        "source_id": source_id,
        "target_id": target_id,
        "relationship": relationship,
        "weight": 1.0,
        "metadata": {},
        **kwargs,
    }


def _snapshot(nodes: list[dict], edges: list[dict]) -> dict:
    return {
        "tenant_id": "test",
        "nodes": nodes,
        "edges": edges,
        "transcripts": [],
        "context_windows": [],
    }


def generate_empty() -> None:
    """Zero nodes, zero edges."""
    snapshot = _snapshot(nodes=[], edges=[])
    write_abhi_document(snapshot, output_path=FIXTURES_DIR / "empty.abhi")
    print("  ✓ empty.abhi")


def generate_single_node() -> None:
    """One node, zero edges."""
    nodes = [_node("Alpha", "The first and only node.")]
    snapshot = _snapshot(nodes=nodes, edges=[])
    write_abhi_document(snapshot, output_path=FIXTURES_DIR / "single-node.abhi")
    print("  ✓ single-node.abhi")


def generate_linear_history() -> None:
    """10 nodes, 9 edges in a linear sequence (n0→n1→…→n9)."""
    nodes = [_node(f"Node {i}", f"Content of node {i}.") for i in range(10)]
    edges = [
        _edge(nodes[i]["id"], nodes[i + 1]["id"], "relates_to")
        for i in range(9)
    ]
    snapshot = _snapshot(nodes=nodes, edges=edges)
    write_abhi_document(snapshot, output_path=FIXTURES_DIR / "linear-history.abhi")
    print("  ✓ linear-history.abhi")


def generate_branched() -> None:
    """20 nodes, 25 edges with branching structure."""
    nodes = [_node(f"Branch Node {i}", f"Branched content {i}.") for i in range(20)]

    edges: list[dict] = []

    # Backbone: 0→1→2→3→4→5→6→7→8→9 (9 edges)
    for i in range(9):
        edges.append(_edge(nodes[i]["id"], nodes[i + 1]["id"], "relates_to"))

    # Branch A: 2→10→11→12→13 (4 edges)
    edges.append(_edge(nodes[2]["id"], nodes[10]["id"], "relates_to"))
    edges.append(_edge(nodes[10]["id"], nodes[11]["id"], "relates_to"))
    edges.append(_edge(nodes[11]["id"], nodes[12]["id"], "relates_to"))
    edges.append(_edge(nodes[12]["id"], nodes[13]["id"], "relates_to"))

    # Branch B: 5→14→15→16 (3 edges)
    edges.append(_edge(nodes[5]["id"], nodes[14]["id"], "relates_to"))
    edges.append(_edge(nodes[14]["id"], nodes[15]["id"], "relates_to"))
    edges.append(_edge(nodes[15]["id"], nodes[16]["id"], "relates_to"))

    # Branch C: 8→17→18→19 (3 edges)
    edges.append(_edge(nodes[8]["id"], nodes[17]["id"], "relates_to"))
    edges.append(_edge(nodes[17]["id"], nodes[18]["id"], "relates_to"))
    edges.append(_edge(nodes[18]["id"], nodes[19]["id"], "relates_to"))

    # Cross-links: 13→9, 16→9, 19→9 (3 edges)
    edges.append(_edge(nodes[13]["id"], nodes[9]["id"], "depends_on"))
    edges.append(_edge(nodes[16]["id"], nodes[9]["id"], "depends_on"))
    edges.append(_edge(nodes[19]["id"], nodes[9]["id"], "depends_on"))

    # Extra cross-links to reach 25 edges: 3→11, 6→15, 4→17 (3 edges)
    edges.append(_edge(nodes[3]["id"], nodes[11]["id"], "relates_to"))
    edges.append(_edge(nodes[6]["id"], nodes[15]["id"], "relates_to"))
    edges.append(_edge(nodes[4]["id"], nodes[17]["id"], "relates_to"))

    assert len(edges) == 25, f"Expected 25 edges, got {len(edges)}"

    snapshot = _snapshot(nodes=nodes, edges=edges)
    write_abhi_document(snapshot, output_path=FIXTURES_DIR / "branched.abhi")
    print("  ✓ branched.abhi")


def generate_with_contradictions() -> None:
    """Nodes with at least one CONTRADICTS edge."""
    n1 = _node("Claim A", "The sky is blue.")
    n2 = _node("Claim B", "The sky is green.")
    n3 = _node("Claim C", "Water is wet.")
    n4 = _node("Claim D", "Water is dry.")

    edges = [
        _edge(n2["id"], n1["id"], "contradicts"),   # B contradicts A
        _edge(n4["id"], n3["id"], "contradicts"),   # D contradicts C
        _edge(n1["id"], n3["id"], "relates_to"),    # A relates to C
    ]

    snapshot = _snapshot(nodes=[n1, n2, n3, n4], edges=edges)
    write_abhi_document(snapshot, output_path=FIXTURES_DIR / "with-contradictions.abhi")
    print("  ✓ with-contradictions.abhi")


def generate_with_dangling_edges() -> None:
    """At least one edge whose target node is absent (intentionally invalid).

    Strategy: build a snapshot with nodes n1 and n2 and a valid edge e1 (n1→n2),
    plus a second edge e2 (n1→n3_missing) where n3 is NOT in the node list.
    The _find_dangling_edges() function will detect e2 as dangling.
    """
    n1 = _node("Present Node 1", "This node exists.")
    n2 = _node("Present Node 2", "This node also exists.")
    # n3 is intentionally absent from the node list
    missing_node_id = str(uuid4())

    e1 = _edge(n1["id"], n2["id"], "relates_to")
    e2 = _edge(n1["id"], missing_node_id, "relates_to")  # dangling — target absent

    # Include only n1 and n2 in the node list; e2 points to a non-existent node
    snapshot = _snapshot(nodes=[n1, n2], edges=[e1, e2])
    write_abhi_document(snapshot, output_path=FIXTURES_DIR / "with-dangling-edges.abhi")
    print("  ✓ with-dangling-edges.abhi  (intentionally invalid — dangling edge present)")


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating fixtures in {FIXTURES_DIR} …")
    generate_empty()
    generate_single_node()
    generate_linear_history()
    generate_branched()
    generate_with_contradictions()
    generate_with_dangling_edges()
    print("Done.")


if __name__ == "__main__":
    main()
