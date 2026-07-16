"""Tests for waggle-mcp demo command and demo.abhi fixture."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure src/ is on the path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
resolved_src = str(SRC.resolve())

if resolved_src not in map(str, map(Path, sys.path)):
    sys.path.insert(0, resolved_src)

import numpy as np

from waggle.graph import MemoryGraph
from waggle.server import _build_parser, _run_demo

DEMO_ABHI = ROOT / "examples" / "demo.abhi"


# ── Helpers ───────────────────────────────────────────────────────────────────


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(c) for c in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if np.isclose(norm, 0.0):
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


# ── demo.abhi fixture tests ───────────────────────────────────────────────────


def test_demo_abhi_exists() -> None:
    """examples/demo.abhi must be checked in."""
    assert DEMO_ABHI.exists(), (
        f"examples/demo.abhi not found at {DEMO_ABHI}. Run: python3 examples/generate_demo_abhi.py"
    )


def test_demo_abhi_imports_cleanly(tmp_path: Path) -> None:
    """demo.abhi must import into a fresh DB without errors."""
    if not DEMO_ABHI.exists():
        pytest.skip("demo.abhi not generated yet")

    db_path = tmp_path / "import-test.db"
    graph = MemoryGraph(str(db_path), FakeEmbeddingModel(), tenant_id="local-default", enable_dedup=False)
    result = graph.import_abhi(input_path=DEMO_ABHI, merge_strategy="skip-existing")

    assert result.nodes_created >= 15, f"Expected ≥15 nodes, got {result.nodes_created}"
    assert result.edges_created >= 15, f"Expected ≥15 edges, got {result.edges_created}"


def test_demo_abhi_has_expected_node_types(tmp_path: Path) -> None:
    """demo.abhi must contain decisions, preferences, facts, and notes."""
    if not DEMO_ABHI.exists():
        pytest.skip("demo.abhi not generated yet")

    db_path = tmp_path / "types-test.db"
    graph = MemoryGraph(str(db_path), FakeEmbeddingModel(), tenant_id="local-default", enable_dedup=False)
    graph.import_abhi(input_path=DEMO_ABHI, merge_strategy="skip-existing")

    nodes = graph.list_recent_nodes(limit=50)
    types = {n.node_type.value for n in nodes}

    assert "decision" in types, "Expected at least one decision node"
    assert "preference" in types, "Expected at least one preference node"
    assert "fact" in types, "Expected at least one fact node"
    assert "note" in types, "Expected at least one note node"


def test_demo_abhi_has_contradiction_edge(tmp_path: Path) -> None:
    """demo.abhi must contain at least one contradicts edge."""
    if not DEMO_ABHI.exists():
        pytest.skip("demo.abhi not generated yet")

    db_path = tmp_path / "contradiction-test.db"
    graph = MemoryGraph(str(db_path), FakeEmbeddingModel(), tenant_id="local-default", enable_dedup=False)
    graph.import_abhi(input_path=DEMO_ABHI, merge_strategy="skip-existing")

    result = graph.query(query="database contradiction", max_nodes=15, max_depth=2)
    contradiction_edges = [e for e in result.edges if e.relationship == "contradicts"]
    assert len(contradiction_edges) >= 1, "Expected at least one contradicts edge"


# ── demo command tests ────────────────────────────────────────────────────────


def test_demo_command_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """waggle-mcp demo must exit with code 0."""
    if not DEMO_ABHI.exists():
        pytest.skip("demo.abhi not generated yet")

    # Patch tempfile.mkdtemp to use tmp_path so we control cleanup
    import tempfile as _tempfile

    monkeypatch.setattr(_tempfile, "mkdtemp", lambda **kw: str(tmp_path / "demo-run"))
    (tmp_path / "demo-run").mkdir(exist_ok=True)

    # Patch EmbeddingModel to use fake model for speed
    import waggle.server as server_mod

    monkeypatch.setattr(server_mod, "EmbeddingModel", lambda name: FakeEmbeddingModel())

    args = SimpleNamespace(with_embeddings=False)
    exit_code = _run_demo(args)
    assert exit_code == 0


def test_demo_does_not_touch_home_waggle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """waggle-mcp demo must not write to ~/.waggle/."""
    if not DEMO_ABHI.exists():
        pytest.skip("demo.abhi not generated yet")

    home_waggle = Path.home() / ".waggle"
    mtime_before = home_waggle.stat().st_mtime if home_waggle.exists() else None

    import tempfile as _tempfile

    monkeypatch.setattr(_tempfile, "mkdtemp", lambda **kw: str(tmp_path / "demo-run"))
    (tmp_path / "demo-run").mkdir(exist_ok=True)

    import waggle.server as server_mod

    monkeypatch.setattr(server_mod, "EmbeddingModel", lambda name: FakeEmbeddingModel())

    args = SimpleNamespace(with_embeddings=False)
    _run_demo(args)

    if home_waggle.exists() and mtime_before is not None:
        mtime_after = home_waggle.stat().st_mtime
        assert mtime_after == mtime_before, "~/.waggle/ was modified by demo command"


def test_demo_queries_return_nonempty(tmp_path: Path) -> None:
    """All 4 demo queries must return at least one node."""
    if not DEMO_ABHI.exists():
        pytest.skip("demo.abhi not generated yet")

    db_path = tmp_path / "query-test.db"
    graph = MemoryGraph(str(db_path), FakeEmbeddingModel(), tenant_id="local-default", enable_dedup=False)
    graph.import_abhi(input_path=DEMO_ABHI, merge_strategy="skip-existing")

    queries = [
        "What database did we choose?",
        "What changed about the database decision? contradiction superseded",
        "team preferences",
        "decisions reasons why",
    ]
    for q in queries:
        result = graph.query(query=q, max_nodes=8, max_depth=2)
        assert result.nodes, f"Query returned no nodes: {q!r}"


def test_demo_parser_registered() -> None:
    """waggle-mcp demo subcommand must be registered in the parser."""
    parser = _build_parser()
    # Parse with demo subcommand — should not raise
    args = parser.parse_args(["demo"])
    assert args.command == "demo"
    assert not args.with_embeddings

    args_embed = parser.parse_args(["demo", "--with-embeddings"])
    assert args_embed.with_embeddings
