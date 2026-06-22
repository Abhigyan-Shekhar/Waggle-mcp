from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from waggle.graph import (
    EMBEDDING_BLOB_MAGIC,
    MemoryGraph,
    decode_embedding_blob,
    encode_embedding_blob,
    ensure_encoded_embedding,
    is_checksummed_embedding,
)
from waggle.models import NodeType


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
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
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


def _node_embedding_blob(graph: MemoryGraph, node_id: str) -> bytes:
    with sqlite3.connect(graph.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT embedding FROM nodes WHERE tenant_id = ? AND id = ?",
            (graph.tenant_id, node_id),
        ).fetchone()
    return bytes(row["embedding"])


def _write_node_embedding(graph: MemoryGraph, node_id: str, blob: bytes) -> None:
    with sqlite3.connect(graph.db_path) as conn:
        conn.execute(
            "UPDATE nodes SET embedding = ? WHERE tenant_id = ? AND id = ?",
            (blob, graph.tenant_id, node_id),
        )
        conn.commit()


def _corrupt_node_embedding(graph: MemoryGraph, node_id: str) -> None:
    blob = bytearray(_node_embedding_blob(graph, node_id))
    # Flip a byte inside the raw payload (after the 4-byte magic prefix) to
    # simulate on-disk bit-rot; the CRC trailer no longer matches.
    blob[len(EMBEDDING_BLOB_MAGIC) + 1] ^= 0xFF
    _write_node_embedding(graph, node_id, bytes(blob))


# ── codec unit tests ──────────────────────────────────────────────────────────


def test_encode_roundtrip_preserves_raw_bytes():
    raw = np.arange(8, dtype=np.float32).tobytes()
    blob = encode_embedding_blob(raw)
    assert blob.startswith(EMBEDDING_BLOB_MAGIC)
    assert len(blob) == len(EMBEDDING_BLOB_MAGIC) + len(raw) + 4
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) == raw


def test_decode_none_and_empty_return_none():
    assert decode_embedding_blob(None) is None
    assert decode_embedding_blob(b"") is None


def test_decode_legacy_blob_passes_through_verbatim():
    raw = np.arange(4, dtype=np.float32).tobytes()
    assert not is_checksummed_embedding(raw)
    assert decode_embedding_blob(raw) == raw


def test_decode_detects_single_bit_flip():
    raw = np.ones(8, dtype=np.float32).tobytes()
    blob = bytearray(encode_embedding_blob(raw))
    blob[len(EMBEDDING_BLOB_MAGIC) + 3] ^= 0x01
    assert decode_embedding_blob(bytes(blob)) is None


def test_decode_detects_truncated_trailer():
    raw = np.ones(8, dtype=np.float32).tobytes()
    blob = encode_embedding_blob(raw)
    assert decode_embedding_blob(blob[:-1]) is None


def test_ensure_encoded_is_idempotent():
    raw = np.ones(4, dtype=np.float32).tobytes()
    once = ensure_encoded_embedding(raw)
    twice = ensure_encoded_embedding(once)
    assert is_checksummed_embedding(once)
    assert once == twice


# ── storage-level tests (acceptance criteria 1-4) ──────────────────────────────


def test_new_writes_are_checksummed(tmp_path: Path):
    # Criterion 1: new writes include a checksum trailer.
    graph = make_graph(tmp_path)
    node = graph.add_node(label="A", content="hello world", node_type=NodeType.ENTITY).node
    blob = _node_embedding_blob(graph, node.id)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) is not None


def test_corrupt_embedding_read_falls_back_to_none(tmp_path: Path):
    # Criterion 2: reads validate the checksum and fall back to "no embedding".
    graph = make_graph(tmp_path)
    node_a = graph.add_node(label="A", content="alpha", node_type=NodeType.ENTITY).node
    node_b = graph.add_node(label="B", content="beta", node_type=NodeType.ENTITY).node

    assert graph._node_cosine_similarity(node_a, node_b) is not None

    _corrupt_node_embedding(graph, node_a.id)
    # The read path detects the bad checksum and returns None instead of a
    # wrong-but-plausible similarity from corrupt bytes.
    assert graph._node_cosine_similarity(node_a, node_b) is None


def test_doctor_health_reports_and_repairs_checksum_failures(tmp_path: Path):
    # Criterion 3: the store-health report (consumed by `doctor`) counts failures.
    graph = make_graph(tmp_path)
    node = graph.add_node(label="DB", content="we chose postgres", node_type=NodeType.ENTITY).node

    assert graph.get_embedding_store_health()["node_checksum_failures"] == 0
    _corrupt_node_embedding(graph, node.id)
    assert graph.get_embedding_store_health()["node_checksum_failures"] == 1

    cleared = graph.clear_corrupt_embeddings()
    assert cleared["nodes"] == 1
    health = graph.get_embedding_store_health()
    assert health["node_checksum_failures"] == 0
    assert health["node_stale_rows"] >= 1

    graph.reembed_stale_embeddings()
    after = graph.get_embedding_store_health()
    assert after["node_checksum_failures"] == 0
    assert after["node_stale_rows"] == 0
    assert decode_embedding_blob(_node_embedding_blob(graph, node.id)) is not None


def test_legacy_row_is_not_a_failure_and_migration_upgrades_it(tmp_path: Path):
    # Criterion 4: a migration covers legacy (pre-checksum) rows.
    graph = make_graph(tmp_path)
    node = graph.add_node(label="A", content="alpha beta", node_type=NodeType.ENTITY).node
    raw = decode_embedding_blob(_node_embedding_blob(graph, node.id))
    assert raw is not None
    _write_node_embedding(graph, node.id, raw)  # downgrade to pre-checksum form

    assert not is_checksummed_embedding(_node_embedding_blob(graph, node.id))
    health = graph.get_embedding_store_health()
    assert health["node_legacy_rows"] == 1
    assert health["node_checksum_failures"] == 0  # legacy != corrupt

    upgraded = graph.migrate_embeddings_to_checksummed()
    assert upgraded["nodes"] == 1
    blob = _node_embedding_blob(graph, node.id)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) == raw
    assert graph.get_embedding_store_health()["node_legacy_rows"] == 0


def test_legacy_rows_are_migrated_on_open(tmp_path: Path):
    # Criterion 4: the migration runs automatically on open. It is tenant-scoped
    # and only touches this tenant's legacy rows, so it is safe to run per open.
    graph = make_graph(tmp_path)
    node = graph.add_node(label="A", content="alpha beta", node_type=NodeType.ENTITY).node
    raw = decode_embedding_blob(_node_embedding_blob(graph, node.id))
    assert raw is not None
    _write_node_embedding(graph, node.id, raw)  # downgrade to pre-checksum form
    assert not is_checksummed_embedding(_node_embedding_blob(graph, node.id))

    reopened = MemoryGraph(graph.db_path, FakeEmbeddingModel())
    assert is_checksummed_embedding(_node_embedding_blob(reopened, node.id))


def test_legacy_embedding_still_reads_correctly(tmp_path: Path):
    # Backward compatibility: a legacy blob with no trailer decodes to the
    # original vector so existing stores keep working for one release.
    graph = make_graph(tmp_path)
    node_a = graph.add_node(label="A", content="alpha", node_type=NodeType.ENTITY).node
    node_b = graph.add_node(label="B", content="beta", node_type=NodeType.ENTITY).node
    for n in (node_a, node_b):
        raw = decode_embedding_blob(_node_embedding_blob(graph, n.id))
        _write_node_embedding(graph, n.id, raw)
    assert graph._node_cosine_similarity(node_a, node_b) is not None


# ── context_windows coverage (reviewer follow-up on #71) ───────────────────────


def _window_embedding_blob(graph: MemoryGraph, window_id: str) -> bytes | None:
    with sqlite3.connect(graph.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT embedding FROM context_windows WHERE tenant_id = ? AND id = ?",
            (graph.tenant_id, window_id),
        ).fetchone()
    if row is None or row["embedding"] is None:
        return None
    return bytes(row["embedding"])


def _write_window_embedding(graph: MemoryGraph, window_id: str, blob: bytes) -> None:
    with sqlite3.connect(graph.db_path) as conn:
        conn.execute(
            "UPDATE context_windows SET embedding = ? WHERE tenant_id = ? AND id = ?",
            (blob, graph.tenant_id, window_id),
        )
        conn.commit()


def _window_with_embedding(tmp_path: Path) -> tuple[MemoryGraph, str]:
    graph = make_graph(tmp_path)
    node = graph.add_node(
        label="W",
        content="window embedding fact about postgres",
        node_type=NodeType.FACT,
        project="waggle",
        session_id="s1",
    ).node
    window_id = node.context_window_id
    assert window_id is not None
    assert graph.get_window_embedding(window_id) is not None  # compute + persist (checksummed, not stale)
    return graph, window_id


def _corrupt_window_embedding(graph: MemoryGraph, window_id: str) -> None:
    blob = bytearray(_window_embedding_blob(graph, window_id))
    blob[len(EMBEDDING_BLOB_MAGIC) + 1] ^= 0xFF
    _write_window_embedding(graph, window_id, bytes(blob))


def test_window_new_writes_are_checksummed(tmp_path: Path):
    graph, window_id = _window_with_embedding(tmp_path)
    blob = _window_embedding_blob(graph, window_id)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) is not None


def test_window_corruption_is_reported_in_health(tmp_path: Path):
    graph, window_id = _window_with_embedding(tmp_path)
    assert graph.get_embedding_store_health()["window_checksum_failures"] == 0
    _corrupt_window_embedding(graph, window_id)
    assert graph.get_embedding_store_health()["window_checksum_failures"] == 1


def test_window_corruption_self_heals_on_read(tmp_path: Path):
    # A non-stale but corrupt cached window embedding must recompute on read
    # instead of silently returning None.
    graph, window_id = _window_with_embedding(tmp_path)
    assert graph.get_context_window(window_id).embedding_stale is False
    _corrupt_window_embedding(graph, window_id)

    recovered = graph.get_window_embedding(window_id)
    assert recovered is not None
    blob = _window_embedding_blob(graph, window_id)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) is not None
    assert graph.get_embedding_store_health()["window_checksum_failures"] == 0


def test_window_corruption_cleared_and_recomputed(tmp_path: Path):
    graph, window_id = _window_with_embedding(tmp_path)
    _corrupt_window_embedding(graph, window_id)

    cleared = graph.clear_corrupt_embeddings()
    assert cleared["context_windows"] == 1
    health = graph.get_embedding_store_health()
    assert health["window_checksum_failures"] == 0
    assert health["window_stale_rows"] >= 1
    assert graph.get_context_window(window_id).embedding_stale is True

    recomputed = graph.recompute_stale_window_embeddings()
    assert recomputed >= 1
    blob = _window_embedding_blob(graph, window_id)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) is not None
    assert graph.get_context_window(window_id).embedding_stale is False


def test_window_legacy_row_is_migrated(tmp_path: Path):
    graph, window_id = _window_with_embedding(tmp_path)
    raw = decode_embedding_blob(_window_embedding_blob(graph, window_id))
    assert raw is not None
    _write_window_embedding(graph, window_id, raw)  # downgrade to pre-checksum form

    health = graph.get_embedding_store_health()
    assert health["window_legacy_rows"] == 1
    assert health["window_checksum_failures"] == 0

    upgraded = graph.migrate_embeddings_to_checksummed()
    assert upgraded["context_windows"] == 1
    blob = _window_embedding_blob(graph, window_id)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) == raw


# ── multi-tenant scoping + crash-safety (second review round) ───────────────────


def test_health_and_clear_are_tenant_scoped(tmp_path: Path):
    # A corrupt row in one tenant must not appear in another tenant's health, and
    # clearing in one tenant must not touch another tenant's rows.
    db = tmp_path / "shared.db"
    model = FakeEmbeddingModel()
    graph_a = MemoryGraph(db, model, tenant_id="tenant-a")
    graph_b = MemoryGraph(db, model, tenant_id="tenant-b")

    node_a = graph_a.add_node(label="A", content="alpha for tenant a", node_type=NodeType.ENTITY).node
    graph_b.add_node(label="B", content="beta for tenant b", node_type=NodeType.ENTITY)

    _corrupt_node_embedding(graph_a, node_a.id)

    assert graph_a.get_embedding_store_health()["node_checksum_failures"] == 1
    assert graph_b.get_embedding_store_health()["node_checksum_failures"] == 0

    # Tenant B repairing its own store leaves tenant A's corruption intact.
    assert graph_b.clear_corrupt_embeddings()["nodes"] == 0
    assert graph_a.get_embedding_store_health()["node_checksum_failures"] == 1


def test_migration_is_tenant_scoped(tmp_path: Path):
    # Opening tenant B must migrate only B's legacy rows; A's stay legacy until A
    # opens (a global gate would have skipped A entirely or marked the DB done).
    db = tmp_path / "shared.db"
    model = FakeEmbeddingModel()
    graph_a = MemoryGraph(db, model, tenant_id="tenant-a")
    graph_b = MemoryGraph(db, model, tenant_id="tenant-b")
    node_a = graph_a.add_node(label="A", content="alpha", node_type=NodeType.ENTITY).node
    node_b = graph_b.add_node(label="B", content="beta", node_type=NodeType.ENTITY).node
    for g, n in ((graph_a, node_a), (graph_b, node_b)):
        _write_node_embedding(g, n.id, decode_embedding_blob(_node_embedding_blob(g, n.id)))

    MemoryGraph(db, model, tenant_id="tenant-b")  # reopen B only

    assert is_checksummed_embedding(_node_embedding_blob(graph_b, node_b.id))
    assert not is_checksummed_embedding(_node_embedding_blob(graph_a, node_a.id))


def test_multi_intent_query_survives_corrupt_embedding(tmp_path: Path):
    # A multi-clause ("... and ...") query exercises _add_clause_seed_ids, which
    # indexes embeddings_by_id; a node with a corrupt embedding is absent from
    # that map and must degrade to lexical scoring instead of raising KeyError.
    graph = make_graph(tmp_path)
    n1 = graph.add_node(
        label="Postgres",
        content="we use postgres for storage",
        node_type=NodeType.ENTITY,
        project="p",
        session_id="s",
    ).node
    graph.add_node(
        label="Redis",
        content="we use redis for caching",
        node_type=NodeType.ENTITY,
        project="p",
        session_id="s",
    )
    _corrupt_node_embedding(graph, n1.id)

    result = graph.query(
        query="postgres storage and redis caching",
        project="p",
        max_nodes=5,
        retrieval_mode="graph",
    )
    assert result is not None


# ── decode hardening: corrupted magic, misaligned payload, deserialization (review 3) ─


def _corrupt_node_magic(graph: MemoryGraph, node_id: str) -> None:
    blob = bytearray(_node_embedding_blob(graph, node_id))
    # Flip a byte *inside* the 4-byte magic prefix. is_checksummed_embedding() now
    # returns False, but the inner raw+CRC is intact — decode must treat this as
    # corruption, not silently pass the whole blob through as "legacy".
    blob[0] ^= 0xFF
    _write_node_embedding(graph, node_id, bytes(blob))


def test_decode_rejects_corrupted_magic():
    raw = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32).tobytes()
    blob = bytearray(encode_embedding_blob(raw))
    blob[0] ^= 0xFF  # damage the magic, leave raw+CRC intact
    assert decode_embedding_blob(bytes(blob)) is None


def test_decode_rejects_misaligned_canonical_payload():
    # CRC is valid, but a 3-byte payload is not a whole number of float32 values.
    assert decode_embedding_blob(encode_embedding_blob(b"abc")) is None


def test_decode_falls_back_when_model_deserialization_raises(tmp_path: Path):
    class RaisingModel(FakeEmbeddingModel):
        def from_bytes(self, data: bytes) -> np.ndarray:
            raise ValueError("cannot deserialize")

    graph = MemoryGraph(tmp_path / "memory.db", RaisingModel())
    raw = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32).tobytes()
    # decode_embedding_blob accepts it (valid CRC, 4-aligned); the model raises, so
    # _decode_embedding must still return None rather than propagating.
    assert graph._decode_embedding(encode_embedding_blob(raw)) is None


def test_corrupt_magic_is_a_failure_and_is_cleared(tmp_path: Path):
    graph = make_graph(tmp_path)
    node = graph.add_node(label="A", content="alpha beta", node_type=NodeType.ENTITY).node
    _corrupt_node_magic(graph, node.id)

    health = graph.get_embedding_store_health()
    # Counted as a failure (real corruption), NOT as a benign legacy row.
    assert health["node_checksum_failures"] == 1
    assert health["node_legacy_rows"] == 0

    assert graph.clear_corrupt_embeddings()["nodes"] == 1
    repaired = graph.reembed_stale_embeddings(batch_size=10)
    assert repaired["node_rows_updated"] == 1
    assert graph.get_embedding_store_health()["node_checksum_failures"] == 0


def test_migration_does_not_launder_corrupt_magic(tmp_path: Path):
    graph = make_graph(tmp_path)
    node = graph.add_node(label="A", content="alpha beta", node_type=NodeType.ENTITY).node
    _corrupt_node_magic(graph, node.id)

    # The on-open migration (and the explicit call) must NOT wrap a corrupt blob
    # into a valid-looking checksummed one — decode must still reject it.
    graph.migrate_embeddings_to_checksummed()
    assert decode_embedding_blob(_node_embedding_blob(graph, node.id)) is None
    assert graph.get_embedding_store_health()["node_checksum_failures"] == 1


# ── import / backfill validation (review 5) ─────────────────────────────────────


def _make_node_blob(values: list[float]) -> bytes:
    return np.asarray(values, dtype=np.float32).tobytes()


def test_coerce_imported_embedding_wraps_legacy_and_keeps_metadata(tmp_path: Path):
    graph = make_graph(tmp_path)
    legacy = _make_node_blob([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    blob, model_id, dim = graph._coerce_imported_embedding(legacy, text="ignored", model_id="snap-model", dim=8)
    assert is_checksummed_embedding(blob)
    assert decode_embedding_blob(blob) == legacy
    assert (model_id, dim) == ("snap-model", 8)  # supplied metadata preserved


def test_coerce_imported_embedding_reembeds_corrupt(tmp_path: Path):
    graph = make_graph(tmp_path)
    good = encode_embedding_blob(_make_node_blob([0.0] * 8))
    corrupt = bytearray(good)
    corrupt[len(EMBEDDING_BLOB_MAGIC) + 1] ^= 0xFF  # break the CRC
    blob, model_id, dim = graph._coerce_imported_embedding(
        bytes(corrupt), text="some content", model_id="snap-model", dim=8
    )
    # Corrupt blob is not persisted as-is: it is re-embedded with the live model.
    assert decode_embedding_blob(blob) is not None
    assert model_id == graph._current_embedding_model_id()
    assert dim == 8


def test_snapshot_node_import_validates_and_converts(tmp_path: Path):
    source = make_graph(tmp_path / "src")
    node = source.add_node(label="N", content="snapshot me", node_type=NodeType.ENTITY).node
    legacy_raw = decode_embedding_blob(_node_embedding_blob(source, node.id))

    target = MemoryGraph(tmp_path / "dst" / "memory.db", FakeEmbeddingModel())
    now = "2025-01-01T00:00:00+00:00"
    common = {
        "id": node.id,
        "label": "N",
        "content": "snapshot me",
        "node_type": NodeType.ENTITY.value,
        "embedding_model_id": "snap-model",
        "embedding_dim": 8,
        "created_at": now,
        "updated_at": now,
    }
    with target._lock, target._pool.checkout() as conn:
        # Legacy blob supplied by the snapshot -> stored checksummed.
        target._insert_snapshot_node(conn, {**common, "embedding": legacy_raw})
        stored = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (node.id,)).fetchone()["embedding"]
    assert is_checksummed_embedding(bytes(stored))
    assert decode_embedding_blob(bytes(stored)) == legacy_raw
    assert target.get_embedding_store_health()["node_checksum_failures"] == 0


def test_snapshot_node_import_reembeds_corrupt(tmp_path: Path):
    target = make_graph(tmp_path)
    good = encode_embedding_blob(_make_node_blob([0.0] * 8))
    corrupt = bytearray(good)
    corrupt[len(EMBEDDING_BLOB_MAGIC) + 1] ^= 0xFF
    now = "2025-01-01T00:00:00+00:00"
    raw_node = {
        "id": "imported-1",
        "label": "N",
        "content": "rebuild me from text",
        "node_type": NodeType.ENTITY.value,
        "embedding": bytes(corrupt),
        "embedding_model_id": "snap-model",
        "embedding_dim": 8,
        "created_at": now,
        "updated_at": now,
    }
    with target._lock, target._pool.checkout() as conn:
        target._insert_snapshot_node(conn, raw_node)
    health = target.get_embedding_store_health()
    assert health["node_checksum_failures"] == 0
    assert health["node_legacy_rows"] == 0


def test_transcript_backfill_reembeds_damaged_checksummed_blob(tmp_path: Path):
    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message="the codeword is saffron",
        assistant_response="noted, saffron",
        project="p",
        session_id="s",
    )
    # Damage the magic of a transcript embedding but keep model_id/dim, and blank
    # turn_pair_id so the backfill re-selects the row and hits the copy-through path.
    with sqlite3.connect(graph.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, embedding FROM transcript_records WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        tid = row["id"]
        damaged = bytearray(bytes(row["embedding"]))
        damaged[0] ^= 0xFF  # corrupt the magic
        conn.execute(
            "UPDATE transcript_records SET embedding = ?, turn_pair_id = '' WHERE id = ?",
            (bytes(damaged), tid),
        )
        conn.commit()

    # Reopen: _migrate_legacy_schema runs the backfill, which must re-embed (not
    # wrap) the damaged blob, leaving no checksum failure or laundered legacy row.
    reopened = MemoryGraph(graph.db_path, FakeEmbeddingModel())
    with sqlite3.connect(reopened.db_path) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT embedding FROM transcript_records WHERE id = ?", (tid,)).fetchone()["embedding"]
    decoded = decode_embedding_blob(bytes(stored))
    assert decoded is not None
    # A re-embed yields a clean 8-dim float32 vector (32 bytes). Wrapping the
    # damaged blob as "legacy" would instead decode to the 40-byte laundered blob,
    # so the exact length is what distinguishes re-embed from launder.
    assert len(decoded) == 8 * np.dtype(np.float32).itemsize
    assert reopened.get_embedding_store_health()["transcript_checksum_failures"] == 0


# ── model-decode validation + window recompute concurrency (review 6) ───────────


def test_coerce_reembeds_model_unreadable_blob(tmp_path: Path):
    class RaisingModel(FakeEmbeddingModel):
        def from_bytes(self, data: bytes) -> np.ndarray:
            raise ValueError("model cannot parse this")

    graph = MemoryGraph(tmp_path / "memory.db", RaisingModel())
    # CRC-valid wrapper, but the model rejects it: must re-embed, not preserve a
    # blob that every later read would treat as missing.
    blob = encode_embedding_blob(_make_node_blob([0.0] * 8))
    result, model_id, dim = graph._coerce_imported_embedding(blob, text="rebuild", model_id="snap", dim=8)
    assert is_checksummed_embedding(result)
    assert model_id == graph._current_embedding_model_id()
    assert dim == 8


def test_coerce_reembeds_when_metadata_missing(tmp_path: Path):
    graph = make_graph(tmp_path)
    legacy = _make_node_blob([0.1] * 8)  # decodable, but no model metadata supplied
    result, model_id, dim = graph._coerce_imported_embedding(legacy, text="rebuild", model_id="", dim=0)
    # Without usable model_id/dim the row would read as stale forever; re-embed instead.
    assert model_id == graph._current_embedding_model_id()
    assert dim == 8
    assert decode_embedding_blob(result) is not None


def test_recompute_repairs_uncorrupted_stale_window(tmp_path: Path):
    graph, window_id = _window_with_embedding(tmp_path)
    # Flag stale without corrupting anything (mirrors a membership change).
    with sqlite3.connect(graph.db_path) as conn:
        conn.execute(
            "UPDATE context_windows SET embedding_stale = 1, updated_at = ? WHERE tenant_id = ? AND id = ?",
            ("2025-02-02T00:00:00+00:00", graph.tenant_id, window_id),
        )
        conn.commit()
    assert graph.get_context_window(window_id).embedding_stale is True
    assert graph.get_embedding_store_health()["window_checksum_failures"] == 0

    assert graph.recompute_stale_window_embeddings() == 1
    assert graph.get_context_window(window_id).embedding_stale is False


def test_recompute_window_skips_when_restaled_during_compute(tmp_path: Path, monkeypatch):
    graph, window_id = _window_with_embedding(tmp_path)
    with sqlite3.connect(graph.db_path) as conn:
        conn.execute(
            "UPDATE context_windows SET embedding_stale = 1, updated_at = ? WHERE tenant_id = ? AND id = ?",
            ("2025-03-03T00:00:00+00:00", graph.tenant_id, window_id),
        )
        conn.commit()

    real_compute = graph.compute_window_embedding

    def racing_compute(wid: str):
        # Simulate another process re-staling the window (which bumps updated_at)
        # after we read it but before we save — the guard must skip the save.
        with sqlite3.connect(graph.db_path) as conn:
            conn.execute(
                "UPDATE context_windows SET embedding_stale = 1, updated_at = ? WHERE tenant_id = ? AND id = ?",
                ("2099-01-01T00:00:00+00:00", graph.tenant_id, wid),
            )
            conn.commit()
        return real_compute(wid)

    monkeypatch.setattr(graph, "compute_window_embedding", racing_compute)
    # The optimistic WHERE (updated_at = observed) misses, so nothing is saved...
    assert graph.recompute_stale_window_embeddings() == 0
    # ...and the window is left stale for a later, clean recompute.
    assert graph.get_context_window(window_id).embedding_stale is True


def test_get_window_embedding_skips_save_when_restaled_during_compute(tmp_path: Path, monkeypatch):
    graph, window_id = _window_with_embedding(tmp_path)
    # Make the window stale so the read falls through to the lazy recompute path.
    with sqlite3.connect(graph.db_path) as conn:
        conn.execute(
            "UPDATE context_windows SET embedding_stale = 1, updated_at = ? WHERE tenant_id = ? AND id = ?",
            ("2025-04-04T00:00:00+00:00", graph.tenant_id, window_id),
        )
        conn.commit()

    real_compute = graph.compute_window_embedding

    def racing_compute(wid: str):
        # Another writer re-stales the window (bumping updated_at) after get_window_embedding
        # read it but before the save — the optimistic guard must skip the save.
        with sqlite3.connect(graph.db_path) as conn:
            conn.execute(
                "UPDATE context_windows SET embedding_stale = 1, updated_at = ? WHERE tenant_id = ? AND id = ?",
                ("2099-01-01T00:00:00+00:00", graph.tenant_id, wid),
            )
            conn.commit()
        return real_compute(wid)

    monkeypatch.setattr(graph, "compute_window_embedding", racing_compute)
    # The caller still gets a usable vector (best-effort read)...
    assert graph.get_window_embedding(window_id) is not None
    # ...but the concurrent state is preserved: the save was skipped, so the
    # window stays stale rather than being marked fresh with an outdated vector.
    assert graph.get_context_window(window_id).embedding_stale is True
