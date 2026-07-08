from pathlib import Path

import pytest

from tests.test_dedup import make_graph
from waggle.models import Node, NodeType


def test_sqlite_vec_triggers_and_parity(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    # 1. Initialize MemoryGraph
    graph = make_graph(tmp_path)
    if not graph._sqlite_vec_loaded:
        pytest.skip("sqlite-vec extension is not loaded in this environment.")

    # 2. Add some nodes and verify triggers copied them to vec_nodes
    node1 = graph.add_node(
        label="Database",
        content="We use PostgreSQL as our primary database system.",
        node_type=NodeType.DECISION,
        project="proj-alpha",
    )
    graph.add_node(
        label="Database",
        content="We use MySQL as our primary database system.",
        node_type=NodeType.DECISION,
        project="proj-alpha",
    )
    node3 = graph.add_node(
        label="Authentication", content="We use JWT for session tokens.", node_type=NodeType.FACT, project="proj-beta"
    )

    # Query vec_nodes directly to verify triggers
    with graph._pool.checkout() as connection:
        rows = connection.execute("SELECT rowid, tenant_id, project FROM vec_nodes ORDER BY rowid ASC").fetchall()
        assert len(rows) == 3
        # Check projects match
        assert rows[0]["project"] == "proj-alpha"
        assert rows[2]["project"] == "proj-beta"

    # 3. Test update trigger
    # Update node3 embedding/project and see if vec_nodes reflects it
    new_embedding = graph.embedding_model.embed("We use OAuth2 for session tokens.")
    with graph._pool.checkout() as connection:
        connection.execute(
            "UPDATE nodes SET embedding = ?, project = ? WHERE id = ?",
            (graph._encode_embedding(new_embedding), "proj-gamma", node3.node.id),
        )

        # Verify update in vec_nodes dynamically using rowid
        node3_rowid = connection.execute("SELECT rowid FROM nodes WHERE id = ?", (node3.node.id,)).fetchone()[0]
        row = connection.execute("SELECT project FROM vec_nodes WHERE rowid = ?", (node3_rowid,)).fetchone()
        assert row["project"] == "proj-gamma"

    # 4. Test delete trigger
    with graph._pool.checkout() as connection:
        node1_rowid = connection.execute("SELECT rowid FROM nodes WHERE id = ?", (node1.node.id,)).fetchone()[0]
        connection.execute("DELETE FROM nodes WHERE id = ?", (node1.node.id,))
        rows = connection.execute("SELECT rowid FROM vec_nodes").fetchall()
        assert len(rows) == 2
        assert node1_rowid not in [r["rowid"] for r in rows]

    # 5. Verify Parity of Deduplication Paths
    # Recreate clean graph
    graph2 = make_graph(tmp_path / "parity")
    if not graph2._sqlite_vec_loaded:
        pytest.skip("sqlite-vec extension is not loaded in this environment.")

    # Add a set of nodes
    graph2.add_node(
        label="Python", content="We use Python for backend services", node_type=NodeType.DECISION, project="proj-X"
    )
    graph2.add_node(
        label="Go",
        content="We use Go for high performance microservices",
        node_type=NodeType.DECISION,
        project="proj-X",
    )
    graph2.add_node(
        label="Rust", content="We use Rust for critical system binaries", node_type=NodeType.DECISION, project="proj-Y"
    )

    # Run _find_duplicate_node with ANN enabled
    incoming = Node(
        tenant_id=graph2.tenant_id,
        project="proj-X",
        label="Python Backend",
        content="Python is used for backend",
        node_type=NodeType.DECISION,
    )
    incoming_emb = graph2.embedding_model.embed(incoming.content)

    with graph2._pool.checkout() as conn:
        # Get result with ANN
        graph2._sqlite_vec_loaded = True
        dup_ann = graph2._find_duplicate_node(conn, node=incoming, embedding=incoming_emb)

        # Get result with pure-Python fallback
        graph2._sqlite_vec_loaded = False
        dup_fallback = graph2._find_duplicate_node(conn, node=incoming, embedding=incoming_emb)

    assert dup_ann is not None
    assert dup_fallback is not None
    assert dup_ann[0].id == dup_fallback[0].id
    assert dup_ann[1] == dup_fallback[1]
    assert pytest.approx(dup_ann[2]) == dup_fallback[2]

    # 6. Verify Parity of Retrieval Seeding Paths
    retriever = graph2.hybrid_retriever()
    query = "Where do we use Python?"
    query_emb = graph2.embedding_model.embed(query)

    # Enable ANN
    graph2._sqlite_vec_loaded = True
    rank_ann = retriever._rank_nodes(query_emb, project="proj-X", agent_id="", session_id="")

    # Disable ANN
    graph2._sqlite_vec_loaded = False
    rank_fallback = retriever._rank_nodes(query_emb, project="proj-X", agent_id="", session_id="")

    assert len(rank_ann) == len(rank_fallback)
    for a, f in zip(rank_ann, rank_fallback, strict=True):
        assert a.candidate_id == f.candidate_id
        assert a.content == f.content
        assert a.source == f.source
        assert a.node_ids == f.node_ids
        assert pytest.approx(a.layer_scores["vector_node"]) == f.layer_scores["vector_node"]


def test_sqlite_vec_robustness(tmp_path: Path) -> None:
    pytest.importorskip("sqlite_vec")
    graph = make_graph(tmp_path)
    if not graph._sqlite_vec_loaded:
        pytest.skip("sqlite-vec extension is not loaded in this environment.")

    # Test 1: Verify _decode_trigger_blob corrupt blobs safety
    with graph._pool.checkout() as connection:
        dim = graph._sqlite_vec_dim
        corrupt_blob = b"NOT1" + b"\x00" * (dim * 4 + 4)
        result = connection.execute("SELECT vec_decode_embedding(?)", (corrupt_blob,)).fetchone()[0]
        assert result == b"\x00" * (dim * 4)

        # Test 2: Fallback path drops triggers and table
        # Pre-check: triggers and vec_nodes should exist
        for trigger in ["t_nodes_insert", "t_nodes_update", "t_nodes_delete"]:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
            ).fetchone()
            assert row is not None

        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_nodes'"
        ).fetchone()
        assert row is not None

        # Simulate fallback by setting load variable to False and running _initialize_database
        graph._sqlite_vec_loaded = False
        graph._initialize_database()

        # Check they are now dropped
        for trigger in ["t_nodes_insert", "t_nodes_update", "t_nodes_delete"]:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
            ).fetchone()
            assert row is None

        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_nodes'"
        ).fetchone()
        assert row is None

