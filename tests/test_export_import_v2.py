from __future__ import annotations

import json
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
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel(), dedup_similarity_threshold=1.1)


def test_export_graph_backup_includes_context_window_hierarchy(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    node = graph.add_node(
        label="Hierarchy Node",
        content="Hierarchy exports should preserve context windows",
        node_type=NodeType.FACT,
        project="alpha",
        session_id="sess-1",
    ).node

    backup = graph.export_graph_backup(output_path=tmp_path / "backup.json")
    payload = json.loads(Path(backup.output_path).read_text())

    assert payload["schema_version"] >= 5
    assert payload["repos"]
    assert payload["context_windows"]
    assert payload["nodes"][0]["context_window_id"] == node.context_window_id
    assert payload["context_windows"][0]["embedding_stale"] is True


def test_import_graph_backup_recreates_context_window_hierarchy(tmp_path: Path) -> None:
    source = make_graph(tmp_path / "source")
    first = source.add_node(
        label="Dog",
        content="Dog is named X",
        node_type=NodeType.ENTITY,
        project="alpha",
        session_id="sess-1",
    ).node
    second = source.add_node(
        label="Dog",
        content="Dog is named Y",
        node_type=NodeType.ENTITY,
        project="alpha",
        session_id="sess-2",
    ).node
    assert first.context_window_id is not None
    assert second.context_window_id is not None
    source.derive_context_window_edges(second.context_window_id, source.ensure_repo("alpha"))
    backup = source.export_graph_backup(output_path=tmp_path / "backup.json")

    target = make_graph(tmp_path / "target")
    imported = target.import_graph_backup(input_path=backup.output_path)

    assert imported.nodes_created == 2
    windows = target.list_context_windows(project="alpha")
    assert len(windows) == 2
    imported_first = target.get_node(first.id)
    assert imported_first.context_window_id == first.context_window_id
    edge_types = {edge.edge_type for window in windows for edge in target.get_context_window_edges(window.id)}
    assert "supersedes" in edge_types


def test_import_legacy_backup_without_hierarchy_still_works(tmp_path: Path) -> None:
    source = make_graph(tmp_path / "source")
    source.add_node(
        label="Legacy Node",
        content="Legacy backups should still import",
        node_type=NodeType.FACT,
    )
    backup = source.export_graph_backup(output_path=tmp_path / "backup.json")
    payload = json.loads(Path(backup.output_path).read_text())
    payload.pop("repos", None)
    payload.pop("context_windows", None)
    payload.pop("context_window_edges", None)
    for node in payload["nodes"]:
        node.pop("context_window_id", None)
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(payload))

    target = make_graph(tmp_path / "target")
    imported = target.import_graph_backup(input_path=legacy_path)

    assert imported.nodes_created == 1
    assert target.get_stats().total_nodes == 1


def test_export_dangling_edge_resolves_referenced_target(tmp_path: Path) -> None:
    source = make_graph(tmp_path / "source")
    node_a = source.add_node(
        label="Alpha Node",
        content="This is in alpha project",
        node_type=NodeType.FACT,
        project="alpha",
    ).node
    node_b = source.add_node(
        label="Beta Node",
        content="This is in beta project",
        node_type=NodeType.FACT,
        project="beta",
    ).node

    source.add_edge(
        source_id=node_a.id,
        target_id=node_b.id,
        relationship="relates_to",
    )

    export_path_with_deps = tmp_path / "export_with_deps.abhi"
    source.export_abhi(
        output_path=export_path_with_deps,
        project="alpha",
        include_deps=True,
    )

    from waggle.abhi import load_abhi_document, _find_dangling_edges
    doc_with_deps = load_abhi_document(export_path_with_deps)
    exported_node_ids = {node["id"] for node in doc_with_deps["nodes"]}
    assert node_a.id in exported_node_ids
    assert node_b.id in exported_node_ids
    exported_edge_ids = {edge["id"] for edge in doc_with_deps["edges"]}
    assert len(exported_edge_ids) == 1
    assert _find_dangling_edges(doc_with_deps) == []

    # Verify that the imported target graph successfully loads the exported file
    target = make_graph(tmp_path / "target")
    imported = target.import_abhi(input_path=export_path_with_deps)
    assert imported.nodes_created == 2
    assert target.get_stats().total_nodes == 2


def test_build_abhi_document_dangling_edge_resolves_with_deps():
    from waggle.abhi import build_abhi_document, _find_dangling_edges
    n1 = {
        "id": "node-1",
        "label": "Node 1",
        "content": "Node 1 content",
        "node_type": "fact",
        "tags": [],
        "aliases": [],
        "metadata": {},
        "project": "alpha",
    }
    n2 = {
        "id": "node-2",
        "label": "Node 2",
        "content": "Node 2 content",
        "node_type": "fact",
        "tags": [],
        "aliases": [],
        "metadata": {},
        "project": "beta",
    }
    edge = {
        "id": "edge-1",
        "source_id": "node-1",
        "target_id": "node-2",
        "relationship": "relates_to",
        "weight": 1.0,
        "metadata": {},
    }

    snapshot = {
        "tenant_id": "test",
        "nodes": [n1, n2],
        "edges": [edge],
        "transcripts": [],
        "context_windows": [],
    }

    doc = build_abhi_document(snapshot, project="alpha", include_deps=True)
    exported_node_ids = {n["id"] for n in doc["nodes"]}
    assert "node-1" in exported_node_ids
    assert "node-2" in exported_node_ids
    assert len(doc["edges"]) == 1
    assert _find_dangling_edges(doc) == []

