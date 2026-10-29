from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from tests.test_neo4j_stubs import _FakeSession, _FakeTransaction, make_stub_graph
from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType


def test_clear_scope_dry_run_and_deletion_parity(tmp_path: Path) -> None:
    # 1. Setup SQLite
    db_path = tmp_path / "test.db"
    from tests.test_context_windows import FakeEmbeddingModel

    sqlite_graph = MemoryGraph(str(db_path), FakeEmbeddingModel(), tenant_id="tenant")
    sqlite_graph.ensure_tenant("tenant")
    repo_id = sqlite_graph.ensure_repo("test-proj")
    sqlite_graph.ensure_context_window("sess-1", repo_id)
    sqlite_graph.add_node(
        node_id="n1", project="test-proj", session_id="sess-1", node_type=NodeType.CONCEPT, label="l1", content="c1"
    )
    sqlite_graph.add_node(
        node_id="n2", project="test-proj", session_id="sess-1", node_type=NodeType.CONCEPT, label="l2", content="c2"
    )
    sqlite_graph.add_edge(source_id="n1", target_id="n2", relationship=RelationType.RELATES_TO)
    sqlite_graph.save_ui_state(agent_id="agent", session_id="sess-1", positions={"pos": "test"}, project="test-proj")

    # 2. Setup Neo4j Mock
    import threading

    neo4j_graph = make_stub_graph()
    neo4j_graph.tenant_id = "tenant"
    neo4j_graph._lock = threading.RLock()
    tx = _FakeTransaction()
    neo4j_graph._session = MagicMock(return_value=_FakeSession(tx))

    def fake_run(query: str, **kwargs: object) -> MagicMock:
        mock_result = MagicMock()
        if "RETURN n.node_type" in query:
            mock_result.__iter__.return_value = [{"node_type": NodeType.CONCEPT, "count": 2}]
        elif "count(DISTINCT r)" in query and "MEMORY_EDGE" in query:
            mock_result.single.return_value = {"count": 1}
        elif "t:MemoryTranscript" in query and "count(t)" in query:
            mock_result.single.return_value = {"count": 0}
        elif ("ui:GraphUIState" in query and "count(ui)" in query) or (
            "cw:ContextWindow" in query and "count(cw)" in query
        ):
            mock_result.single.return_value = {"count": 1}
        elif "CONTEXT_WINDOW_EDGE" in query and "count(DISTINCT cwe)" in query:
            mock_result.single.return_value = {"count": 0}
        elif "repo:Repo" in query and "count(repo)" in query:
            mock_result.single.return_value = {"count": 1}
        else:
            mock_result.single.return_value = None
            mock_result.consume.return_value = None
        return mock_result

    mock_run = MagicMock(side_effect=fake_run)
    tx.run = mock_run
    neo4j_graph._session.return_value.run = mock_run
    neo4j_graph.emit_audit_event = MagicMock()

    # --- DRY RUN PARITY ---
    sqlite_dry = sqlite_graph.clear_project(project="test-proj", dry_run=True)
    neo4j_dry = neo4j_graph.clear_project(project="test-proj", dry_run=True)

    assert sqlite_dry.model_dump() == neo4j_dry.model_dump()
    assert sqlite_dry.deleted_context_windows == 1
    assert sqlite_dry.deleted_repos == 1
    assert sqlite_dry.deleted_nodes == 2
    assert sqlite_dry.deleted_edges == 1
    assert sqlite_dry.deleted_graph_ui_rows == 1

    # --- DELETION PARITY ---
    sqlite_del = sqlite_graph.clear_project(project="test-proj", dry_run=False)
    neo4j_del = neo4j_graph.clear_project(project="test-proj", dry_run=False)

    assert sqlite_del.model_dump() == neo4j_del.model_dump()

    # Verify Neo4j deletes executed
    call_args_list = [call.args[0] for call in mock_run.call_args_list]
    assert any("DETACH DELETE ui" in q for q in call_args_list)
    assert any("DETACH DELETE t" in q for q in call_args_list)
    assert any("DETACH DELETE cw" in q for q in call_args_list)
    assert any("DETACH DELETE repo" in q for q in call_args_list)
    assert any("DETACH DELETE n" in q for q in call_args_list)
