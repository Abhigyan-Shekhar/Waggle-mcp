from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

import waggle
from waggle.auth import hash_api_key, verify_api_key
from waggle.config import AppConfig
from waggle.errors import (
    AuthenticationError,
    RateLimitExceededError,
    ValidationFailure,
)
from waggle.graph import MemoryGraph
from waggle.models import NodeType
from waggle.rate_limit import RateLimiter
from waggle.server import WaggleServer, create_http_application


class FakeEmbeddingModel:
    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            vector[sum(ord(character) for character in token) % len(vector)] += 1.0
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


def make_graph(tmp_path: Path, tenant_id: str = "local-default") -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel(), tenant_id=tenant_id)


def make_http_config(tmp_path: Path, **overrides: object) -> AppConfig:
    config = AppConfig(
        backend="neo4j",
        transport="http",
        model_name="fake-model",
        db_path=str(tmp_path / "memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=10,
        write_rate_limit_rpm=5,
        max_concurrent_requests=2,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="secret",
        neo4j_database="neo4j",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def insert_transcript_record(
    graph: MemoryGraph, *, project: str, session_id: str, role: str, turn_index: int, text: str
) -> None:
    with graph._lock, graph._connect() as connection:
        graph._store_transcript_record(
            connection,
            agent_id="codex",
            project=project,
            session_id=session_id,
            observed_at=datetime(2026, 5, 1, tzinfo=UTC),
            turn_index=turn_index,
            role=role,
            transcript_text=text,
            metadata={},
            message_identity=f"{session_id}:{turn_index}:{role}",
        )


def test_api_key_hashing_round_trip() -> None:
    hashed = hash_api_key("secret-token")
    assert hashed != "secret-token"
    assert verify_api_key("secret-token", hashed) is True
    assert verify_api_key("wrong-token", hashed) is False


def test_api_key_record_tracks_prefix_and_last_used(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)

    created = graph.create_api_key("tenant-http", "http-test", created_by="ops@example.com")
    assert created.record.prefix.startswith("sk_test_")
    assert created.record.created_by == "ops@example.com"

    authenticated = graph.authenticate_api_key(created.raw_api_key)

    assert authenticated.prefix == created.record.prefix
    assert authenticated.last_used_at is not None


def test_expired_api_key_is_rejected(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    created = graph.create_api_key(
        "tenant-http",
        "expired-test",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    with pytest.raises(AuthenticationError, match="expired"):
        graph.authenticate_api_key(created.raw_api_key)


def test_retention_policy_and_prune_delete_old_records(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    graph.ensure_tenant("tenant-http", "Tenant HTTP")
    tenant_graph = graph.for_tenant("tenant-http")
    old_time = datetime(2026, 1, 1, tzinfo=UTC)
    new_time = datetime(2026, 5, 1, tzinfo=UTC)

    old_node = tenant_graph.add_node(
        label="Old decision",
        content="Should be pruned",
        node_type=NodeType.DECISION,
    )
    fresh_node = tenant_graph.add_node(
        label="Fresh decision",
        content="Should remain",
        node_type=NodeType.DECISION,
    )

    with tenant_graph._lock, tenant_graph._connect() as connection:
        connection.execute(
            "UPDATE nodes SET created_at = ?, updated_at = ? WHERE id = ?",
            (old_time.isoformat(), old_time.isoformat(), old_node.node.id),
        )
        connection.execute(
            "UPDATE nodes SET created_at = ?, updated_at = ? WHERE id = ?",
            (new_time.isoformat(), new_time.isoformat(), fresh_node.node.id),
        )
        tenant_graph._store_transcript_record(
            connection,
            agent_id="codex",
            project="MCP",
            session_id="session-old",
            observed_at=old_time,
            turn_index=0,
            role="user",
            transcript_text="Old transcript",
            metadata={},
            message_identity="old:0:user",
        )
        tenant_graph._store_transcript_record(
            connection,
            agent_id="codex",
            project="MCP",
            session_id="session-new",
            observed_at=new_time,
            turn_index=0,
            role="user",
            transcript_text="Fresh transcript",
            metadata={},
            message_identity="new:0:user",
        )

    policy = tenant_graph.update_retention_policy(enabled=True, retention_days=30, prune_interval_hours=12)
    assert policy.enabled is True
    assert policy.retention_days == 30
    assert policy.prune_interval_hours == 12

    run = tenant_graph.prune_retention(now=datetime(2026, 5, 6, tzinfo=UTC))

    assert run.status == "completed"
    assert run.deleted_nodes == 1
    assert run.deleted_transcripts == 1

    with tenant_graph._lock, tenant_graph._connect() as connection:
        rows = connection.execute("SELECT label FROM nodes ORDER BY label ASC").fetchall()
    assert {row["label"] for row in rows} == {"Fresh decision"}

    transcripts = tenant_graph.list_transcript_records()
    assert [record.transcript_text for record in transcripts] == ["Fresh transcript"]

    runs = tenant_graph.list_retention_runs(limit=5)
    assert runs[0].run_id == run.run_id
    prune_events = tenant_graph.list_audit_events(limit=5, event_type="retention.prune.completed")
    assert prune_events[0].metadata["deleted_nodes"] == 1


def test_node_write_and_export_emit_audit_events(tmp_path: Path) -> None:
    graph = make_graph(tmp_path, tenant_id="tenant-http")
    created = graph.add_node(
        label="Audit fact",
        content="Track this write",
        node_type=NodeType.FACT,
        project="MCP",
        session_id="session-1",
    )

    export_result = graph.export_graph_backup()
    events = graph.list_audit_events(limit=10)
    event_types = [event.event_type for event in events]

    assert created.created is True
    assert "graph.node.created" in event_types
    assert "export.created" in event_types
    export_events = [event for event in events if event.resource_id == export_result.output_path]
    assert export_events[0].metadata["format"] == "backup"


def test_app_config_disables_hybrid_rerank_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAGGLE_HYBRID_RERANK_ENABLED", raising=False)
    monkeypatch.delenv("WAGGLE_HYBRID_RERANK_MODEL", raising=False)

    config = AppConfig.from_env()

    assert config.hybrid_rerank_enabled is False
    assert config.hybrid_rerank_model == "claude-3-5-sonnet-latest"


def test_app_config_invalid_http_port_raises_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAGGLE_HTTP_PORT", "abc")

    with pytest.raises(
        ValidationFailure,
        match="WAGGLE_HTTP_PORT must be an integer",
    ):
        AppConfig.from_env()


def test_app_config_invalid_dedup_threshold_raises_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAGGLE_DEDUP_THRESHOLD", "abc")

    with pytest.raises(
        ValidationFailure,
        match="WAGGLE_DEDUP_THRESHOLD must be a float",
    ):
        AppConfig.from_env()


def test_waggle_unknown_lazy_export_raises_attribute_error() -> None:
    def access_missing_export() -> object:
        return waggle.definitely_missing_export

    with pytest.raises(AttributeError):
        access_missing_export()
    assert hasattr(waggle, "definitely_missing_export") is False


def test_rate_limiter_enforces_request_and_concurrency_limits() -> None:
    limiter = RateLimiter(requests_per_minute=1, write_requests_per_minute=1, max_concurrent_requests=1)

    async def exercise() -> None:
        await limiter.check_rate("tenant-a", is_write=False)
        with pytest.raises(RateLimitExceededError):
            await limiter.check_rate("tenant-a", is_write=False)

        async with limiter.concurrency_slot("tenant-a"):
            with pytest.raises(RateLimitExceededError):
                async with limiter.concurrency_slot("tenant-a"):
                    pass

    asyncio.run(exercise())


def test_tenant_scoping_isolated_within_same_sqlite_database(tmp_path: Path) -> None:
    root = make_graph(tmp_path)
    tenant_a = root.for_tenant("tenant-a")
    tenant_b = root.for_tenant("tenant-b")

    tenant_a.add_node(
        label="Tenant A Project",
        content="Tenant A stores isolated memory",
        node_type=NodeType.ENTITY,
    )

    assert tenant_a.get_stats().total_nodes == 1
    assert tenant_b.get_stats().total_nodes == 0
    assert tenant_b.query(query="isolated memory", max_nodes=5, max_depth=1).nodes == []


def test_backup_round_trip_preserves_schema_and_tenant_metadata(tmp_path: Path) -> None:
    source = make_graph(tmp_path / "source", tenant_id="tenant-source")
    source.add_node(
        label="Tenant Source Node",
        content="Backup metadata should include tenant identity.",
        node_type=NodeType.NOTE,
    )

    backup = source.export_graph_backup(output_path=tmp_path / "source" / "backup.json")
    target = make_graph(tmp_path / "target", tenant_id="tenant-target")
    imported = target.import_graph_backup(input_path=backup.output_path)

    assert backup.tenant_id == "tenant-source"
    assert backup.schema_version >= 2
    assert imported.tenant_id == "tenant-target"
    assert imported.schema_version >= 2
    assert target.get_stats().total_nodes == 1


def test_http_app_health_auth_and_metrics(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    created = graph.create_api_key("tenant-http", "http-test")
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200

        missing = client.post("/mcp", json={})
        assert missing.status_code == 401
        invalid = client.post("/mcp", json={}, headers={"X-API-Key": "bad-key"})
        assert invalid.status_code == 401

        valid = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}},
            headers={"X-API-Key": created.raw_api_key, "accept": "application/json, text/event-stream"},
        )
        assert valid.status_code == 200

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "waggle_http_requests_total" in metrics.text
        assert "waggle_ready" in metrics.text

    audit_events = graph.for_tenant("tenant-http").list_audit_events(limit=10, event_type="api_key.used")
    assert audit_events[0].api_key_id == created.record.api_key_id


def test_http_app_rate_limit_and_payload_limit(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_http_config(tmp_path, rate_limit_rpm=1, max_payload_bytes=256)
    app_server = WaggleServer(graph=graph, config=config)
    created = graph.create_api_key("tenant-http", "http-test")
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        too_large = client.post(
            "/mcp",
            content=b"x" * 512,
            headers={"X-API-Key": created.raw_api_key},
        )
        assert too_large.status_code == 413

        first = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "1", "method": "tools/list", "params": {}},
            headers={"X-API-Key": created.raw_api_key, "accept": "application/json, text/event-stream"},
        )
        second = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "2", "method": "tools/list", "params": {}},
            headers={"X-API-Key": created.raw_api_key, "accept": "application/json, text/event-stream"},
        )

        assert first.status_code == 200
        assert second.status_code == 429


def test_http_admin_endpoints_require_admin_scope_when_key_present(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    scoped_key = graph.create_api_key("tenant-http", "reader", scopes=["graph:read"])
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        denied = client.get(
            "/api/admin/audit-events",
            params={"tenant_id": "tenant-http"},
            headers={"X-API-Key": scoped_key.raw_api_key},
        )
        assert denied.status_code == 403


def test_mcp_write_requires_graph_write_scope(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    read_only_key = graph.create_api_key("tenant-http", "reader", scopes=["graph:read"])
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        denied = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "tools/call",
                "params": {
                    "name": "store_node",
                    "arguments": {
                        "label": "Scoped",
                        "content": "Should fail without write scope",
                        "node_type": "note",
                    },
                },
            },
            headers={"X-API-Key": read_only_key.raw_api_key, "accept": "application/json, text/event-stream"},
        )
        assert denied.status_code == 403


def test_http_graph_delete_edge_requires_graph_write_scope(tmp_path: Path) -> None:
    """Regression test for #46: graph_delete_edge mutates the graph and must require graph:write."""
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    read_only_key = graph.create_api_key("tenant-http", "reader", scopes=["graph:read"])
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        denied = client.delete(
            "/api/graph/edges/any-edge-id",
            headers={"X-API-Key": read_only_key.raw_api_key},
        )
        assert denied.status_code == 403


def test_http_graph_editor_routes_and_crud(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    insert_transcript_record(
        graph,
        project="studio",
        session_id="sess-a",
        role="user",
        turn_index=0,
        text="Need transcript provenance in the UI.",
    )
    insert_transcript_record(
        graph,
        project="studio",
        session_id="sess-a",
        role="assistant",
        turn_index=1,
        text="Stored transcript provenance as memory.",
    )
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        editor = client.get("/graph")
        assert editor.status_code == 200
        assert "Waggle Graph Studio" in editor.text
        assert "window.__WAGGLE_GRAPH_CONFIG__" in editor.text
        viewer = client.get("/graph?mode=view")
        assert viewer.status_code == 200
        assert '"mode": "view"' in viewer.text
        asset = client.get("/graph-assets/app.js")
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]

        created_node = client.post(
            "/api/graph/nodes",
            json={
                "label": "HTTP Node",
                "content": "Created through the graph editor API.",
                "node_type": "note",
                "project": "studio",
            },
        )
        assert created_node.status_code == 200
        node_id = created_node.json()["id"]

        snapshot = client.get("/api/graph", params={"project": "studio"})
        assert snapshot.status_code == 200
        assert len(snapshot.json()["nodes"]) == 1

        abhi_preview = client.get("/api/graph/abhi", params={"project": "studio"})
        assert abhi_preview.status_code == 200
        assert "schema" in abhi_preview.json()
        assert abhi_preview.json()["validation"]["valid"] is True

        saved_ui = client.patch(
            "/api/graph/ui",
            json={
                "project": "studio",
                "positions": {node_id: {"x": 140, "y": 280}},
                "zoom": 1.1,
                "viewport": {"center_x": 140, "center_y": 280},
                "selected_nodes": [node_id],
            },
        )
        assert saved_ui.status_code == 200
        assert saved_ui.json()["positions"][node_id] == {"x": 140, "y": 280}

        restored = client.post(
            "/api/graph/restore",
            json={
                "project": "studio",
                "nodes": [
                    {
                        "id": node_id,
                        "label": "HTTP Node Restored",
                        "content": "Restored through snapshot replay.",
                        "node_type": "note",
                        "tags": ["restored"],
                        "project": "studio",
                    }
                ],
                "edges": [],
                "ui": {"positions": {node_id: {"x": 220, "y": 120}}, "selected_nodes": [node_id]},
            },
        )
        assert restored.status_code == 200
        assert restored.json()["nodes"][0]["label"] == "HTTP Node Restored"

        updated_node = client.patch(
            f"/api/graph/nodes/{node_id}",
            json={"label": "HTTP Node Updated", "content": "Edited in browser."},
        )
        assert updated_node.status_code == 200
        assert updated_node.json()["label"] == "HTTP Node Updated"

        second_node = client.post(
            "/api/graph/nodes",
            json={
                "label": "HTTP Node 2",
                "content": "Second node",
                "node_type": "note",
                "project": "studio",
            },
        )
        second_id = second_node.json()["id"]

        created_edge = client.post(
            "/api/graph/edges",
            json={
                "source_id": node_id,
                "target_id": second_id,
                "relationship": "relates_to",
                "weight": 1.0,
            },
        )
        assert created_edge.status_code == 200
        edge_id = created_edge.json()["id"]

        updated_edge = client.patch(
            f"/api/graph/edges/{edge_id}",
            json={
                "source_id": node_id,
                "target_id": second_id,
                "relationship": "depends_on",
                "weight": 0.5,
            },
        )
        assert updated_edge.status_code == 200
        assert updated_edge.json()["relationship"] == "depends_on"

        query_result = client.post(
            "/api/graph/query",
            json={"project": "studio", "query": "FIND nodes WHERE type='note' AND content CONTAINS 'browser'"},
        )
        assert query_result.status_code == 200
        assert len(query_result.json()["nodes"]) == 1

        transcripts = client.get("/api/graph/transcripts", params={"project": "studio"})
        assert transcripts.status_code == 200
        assert transcripts.json()["records"]

        transcript_search = client.get("/api/graph/transcripts", params={"project": "studio", "query": "provenance"})
        assert transcript_search.status_code == 200
        assert transcript_search.json()["hits"]

        retrieval_debug = client.post(
            "/api/graph/retrieval-debug",
            json={"project": "studio", "query": "browser provenance", "max_nodes": 4, "max_depth": 1},
        )
        assert retrieval_debug.status_code == 200
        assert "fusion_hits" in retrieval_debug.json()

        diff_result = client.get("/api/graph/diff", params={"since": "24h"})
        assert diff_result.status_code == 200
        assert len(diff_result.json()["added_nodes"]) >= 2

        exported_abhi = client.get("/api/graph/export", params={"format": "abhi", "project": "studio"})
        assert exported_abhi.status_code == 200
        assert exported_abhi.content.startswith(b"WGL\x01")
        assert exported_abhi.headers["content-disposition"] == 'attachment; filename="waggle-memory.abhi"'
        exported_abhi_b64 = base64.b64encode(exported_abhi.content).decode("ascii")

        import_preview = client.post(
            "/api/graph/abhi/preview-import",
            json={"format": "abhi", "content_base64": exported_abhi_b64},
        )
        assert import_preview.status_code == 200
        assert import_preview.json()["validation"]["valid"] is True
        assert import_preview.json()["snapshot"]["nodes"]

        abhi_diff = client.post(
            "/api/graph/abhi/diff",
            json={"content_a_base64": exported_abhi_b64, "content_b_base64": exported_abhi_b64},
        )
        assert abhi_diff.status_code == 200
        assert "diff" in abhi_diff.json()

        deleted_edge = client.delete(f"/api/graph/edges/{edge_id}")
        assert deleted_edge.status_code == 200

        deleted_node = client.delete(f"/api/graph/nodes/{second_id}")
        assert deleted_node.status_code == 200

    audit_events = graph.list_audit_events(limit=50)
    event_types = {event.event_type for event in audit_events}
    assert "graph.snapshot.read" in event_types
    assert "record.read" in event_types
    assert "graph.query.executed" in event_types
    assert "graph.diff.read" in event_types
    assert "export.downloaded" in event_types


def test_http_admin_retention_and_audit_endpoints(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path, backend="sqlite", transport="http"))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        update = client.put(
            "/api/admin/retention",
            json={
                "tenant_id": "workspace-a",
                "enabled": True,
                "retention_days": 90,
                "prune_interval_hours": 24,
            },
        )
        assert update.status_code == 200
        assert update.json()["enabled"] is True

        status = client.get("/api/admin/retention", params={"tenant_id": "workspace-a"})
        assert status.status_code == 200
        assert status.json()["retention_days"] == 90

        prune = client.post("/api/admin/retention/prune", json={"tenant_id": "workspace-a", "batch_size": 1000})
        assert prune.status_code == 200
        assert prune.json()["status"] in {"completed", "skipped"}

        runs = client.get("/api/admin/retention/runs", params={"tenant_id": "workspace-a", "limit": 10})
        assert runs.status_code == 200
        assert runs.json()

        audit = client.get(
            "/api/admin/audit-events", params={"tenant_id": "workspace-a", "type": "retention.policy.updated"}
        )
        assert audit.status_code == 200
        assert audit.json()[0]["event_type"] == "retention.policy.updated"


def test_graph_create_edge_rejects_invalid_weight(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    created = graph.create_api_key("tenant-http", "http-test")
    app = create_http_application(app_server, app_server.config)

    source = graph.for_tenant("tenant-http").add_node(
        label="Source",
        content="source node",
        node_type="fact",
    )
    target = graph.for_tenant("tenant-http").add_node(
        label="Target",
        content="target node",
        node_type="fact",
    )

    with TestClient(app) as client:
        headers = {"X-API-Key": created.raw_api_key}

        resp = client.post(
            "/api/graph/edges",
            json={
                "source_id": source.node.id,
                "target_id": target.node.id,
                "relationship": "relates_to",
                "weight": 1.5,
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "numeric" in resp.json()["message"].lower()

        resp = client.post(
            "/api/graph/edges",
            json={
                "source_id": source.node.id,
                "target_id": target.node.id,
                "relationship": "relates_to",
                "weight": -0.5,
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "numeric" in resp.json()["message"].lower()

        resp = client.post(
            "/api/graph/edges",
            json={
                "source_id": source.node.id,
                "target_id": target.node.id,
                "relationship": "relates_to",
                "weight": "bad",
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "numeric" in resp.json()["message"].lower()

        resp = client.post(
            "/api/graph/edges",
            json={
                "source_id": source.node.id,
                "target_id": target.node.id,
                "relationship": "relates_to",
                "weight": 0.5,
            },
            headers=headers,
        )
        assert resp.status_code == 200


def test_graph_update_edge_rejects_invalid_weight(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    created = graph.create_api_key("tenant-http", "http-test")
    app = create_http_application(app_server, app_server.config)

    source = graph.for_tenant("tenant-http").add_node(
        label="Source",
        content="source node",
        node_type="fact",
    )
    target = graph.for_tenant("tenant-http").add_node(
        label="Target",
        content="target node",
        node_type="fact",
    )

    edge = graph.for_tenant("tenant-http").add_edge(
        source_id=source.node.id,
        target_id=target.node.id,
        relationship="relates_to",
        weight=0.5,
    )

    with TestClient(app) as client:
        headers = {"X-API-Key": created.raw_api_key}

        resp = client.patch(
            f"/api/graph/edges/{edge.id}",
            json={"weight": 1.5},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "numeric" in resp.json()["message"].lower()

        resp = client.patch(
            f"/api/graph/edges/{edge.id}",
            json={"weight": -0.1},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "numeric" in resp.json()["message"].lower()

        resp = client.patch(
            f"/api/graph/edges/{edge.id}",
            json={"weight": "bad"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "numeric" in resp.json()["message"].lower()

        resp = client.patch(
            f"/api/graph/edges/{edge.id}",
            json={"weight": 0.8},
            headers=headers,
        )
        assert resp.status_code == 200
