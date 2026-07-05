"""Server-layer integration tests for Graphify features (MCP tools + HTTP endpoints).

These exercise the dispatch/serialization glue that the graph-layer unit tests
(test_graphify_phase{1,2,3}.py) do not: MCP tool handlers in WaggleServer and
the Starlette HTTP routes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from starlette.testclient import TestClient

from waggle.config import AppConfig
from waggle.graph import MemoryGraph
from waggle.server import WaggleServer, create_http_application


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(text.strip().lower().encode("utf-8")).digest()[:8], "big")
        vector = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return np.asarray(embedding, dtype=np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        an = float(np.linalg.norm(a))
        bn = float(np.linalg.norm(b))
        return float(np.dot(a, b) / (an * bn)) if an and bn else 0.0


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(tmp_path / "memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=1000,
        write_rate_limit_rpm=1000,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def make_app(tmp_path: Path) -> WaggleServer:
    graph = MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())
    return WaggleServer(graph=graph, config=_config(tmp_path))


def _store_node(app: WaggleServer, label: str, node_type: str = "fact") -> str:
    result = app.handle_tool_call("store_node", {"label": label, "content": f"{label} content", "node_type": node_type})
    assert result.isError is False
    return result.structuredContent["id"]


# ---------------------------------------------------------------------------
# MCP tool dispatch
# ---------------------------------------------------------------------------


class TestMcpTools:
    def test_shortest_path_tool(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        a = _store_node(app, "Alpha")
        b = _store_node(app, "Beta")
        app.handle_tool_call("store_edge", {"source_id": a, "target_id": b, "relationship": "relates_to"})

        result = app.handle_tool_call("shortest_path", {"source_id": a, "target_id": b})
        assert result.isError is False
        node_ids = [n["id"] for n in result.structuredContent["nodes"]]
        assert node_ids[0] == a and node_ids[-1] == b

    def test_shortest_path_tool_no_path(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        a = _store_node(app, "Alpha")
        b = _store_node(app, "Beta")
        result = app.handle_tool_call("shortest_path", {"source_id": a, "target_id": b})
        assert result.isError is False
        assert result.structuredContent["nodes"] == []
        assert "No path" in result.content[0].text

    def test_shortest_path_edges_include_confidence(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        a = _store_node(app, "Alpha")
        b = _store_node(app, "Beta")
        app.handle_tool_call("store_edge", {"source_id": a, "target_id": b, "relationship": "relates_to"})
        result = app.handle_tool_call("shortest_path", {"source_id": a, "target_id": b})
        assert all("confidence" in e for e in result.structuredContent["edges"])

    def test_recompute_and_get_communities_tools(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        a = _store_node(app, "Alpha")
        b = _store_node(app, "Beta")
        c = _store_node(app, "Gamma")
        app.handle_tool_call("store_edge", {"source_id": a, "target_id": b, "relationship": "relates_to"})
        app.handle_tool_call("store_edge", {"source_id": b, "target_id": c, "relationship": "relates_to"})

        recompute = app.handle_tool_call("recompute_communities", {})
        assert recompute.isError is False
        assert recompute.structuredContent["nodes_updated"] >= 1

        communities = app.handle_tool_call("get_communities", {})
        assert communities.isError is False
        assert communities.structuredContent["communities"]

    def test_export_cypher_tool(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        a = _store_node(app, "Alpha")
        b = _store_node(app, "Beta")
        app.handle_tool_call("store_edge", {"source_id": a, "target_id": b, "relationship": "relates_to"})

        result = app.handle_tool_call("export_cypher", {"output_path": str(tmp_path / "out.cypher")})
        assert result.isError is False
        path = Path(result.structuredContent["output_path"])
        assert path.exists()
        text = path.read_text()
        assert "CREATE CONSTRAINT" in text and "MATCH (a:Memory" in text

    def test_import_graphify_tool(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        graph_json = {
            "nodes": [
                {"id": "fn:a", "name": "authenticate", "type": "function"},
                {"id": "cls:u", "name": "User", "type": "class"},
            ],
            "edges": [{"source": "fn:a", "target": "cls:u", "type": "uses", "confidence": "EXTRACTED"}],
        }
        path = tmp_path / "graph.json"
        path.write_text(json.dumps(graph_json), encoding="utf-8")

        result = app.handle_tool_call("import_graphify", {"input_path": str(path), "project": "repo"})
        assert result.isError is False
        assert result.structuredContent["nodes_created"] == 2
        assert result.structuredContent["edges_created"] == 1

    def test_new_tools_listed(self, tmp_path: Path) -> None:
        app = make_app(tmp_path)
        names = {tool.name for tool in app.build_tools()}
        assert {
            "shortest_path",
            "get_communities",
            "recompute_communities",
            "export_cypher",
            "import_graphify",
        }.issubset(names)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def _http_client_and_key(tmp_path: Path) -> tuple[TestClient, str]:
    graph = MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())
    app_server = WaggleServer(graph=graph, config=_config(tmp_path, transport="http"))
    created = graph.create_api_key("local-default", "http-test")
    app = create_http_application(app_server, app_server.config)
    return TestClient(app), created.raw_api_key


class TestHttpEndpoints:
    def _seed(self, headers: dict, client: TestClient) -> tuple[str, str]:
        a = client.post(
            "/api/graph/nodes", json={"label": "Alpha", "content": "a", "node_type": "fact"}, headers=headers
        )
        b = client.post(
            "/api/graph/nodes", json={"label": "Beta", "content": "b", "node_type": "fact"}, headers=headers
        )
        a_id, b_id = a.json()["id"], b.json()["id"]
        client.post(
            "/api/graph/edges",
            json={"source_id": a_id, "target_id": b_id, "relationship": "relates_to"},
            headers=headers,
        )
        return a_id, b_id

    def test_shortest_path_endpoint(self, tmp_path: Path) -> None:
        client, key = _http_client_and_key(tmp_path)
        headers = {"X-API-Key": key}
        with client:
            a_id, b_id = self._seed(headers, client)
            resp = client.post("/api/graph/shortest-path", json={"source_id": a_id, "target_id": b_id}, headers=headers)
            assert resp.status_code == 200
            body = resp.json()
            assert body["found"] is True
            assert body["hop_count"] == 1

    def test_hubs_endpoint(self, tmp_path: Path) -> None:
        client, key = _http_client_and_key(tmp_path)
        headers = {"X-API-Key": key}
        with client:
            self._seed(headers, client)
            resp = client.get("/api/graph/hubs", headers=headers)
            assert resp.status_code == 200
            assert "hubs" in resp.json()

    def test_communities_endpoints(self, tmp_path: Path) -> None:
        client, key = _http_client_and_key(tmp_path)
        headers = {"X-API-Key": key}
        with client:
            self._seed(headers, client)
            recompute = client.post("/api/graph/communities/recompute", json={}, headers=headers)
            assert recompute.status_code == 200
            assert "cluster_count" in recompute.json()
            listing = client.get("/api/graph/communities", headers=headers)
            assert listing.status_code == 200
            assert "communities" in listing.json()

    def test_export_cypher_endpoint(self, tmp_path: Path) -> None:
        client, key = _http_client_and_key(tmp_path)
        headers = {"X-API-Key": key}
        with client:
            self._seed(headers, client)
            resp = client.get("/api/graph/export?format=cypher", headers=headers)
            assert resp.status_code == 200
            assert "CREATE CONSTRAINT" in resp.text

    def test_import_graphify_endpoint(self, tmp_path: Path) -> None:
        client, key = _http_client_and_key(tmp_path)
        headers = {"X-API-Key": key}
        graph_json = {
            "nodes": [{"id": "fn:a", "name": "render", "type": "function"}],
            "edges": [],
        }
        with client:
            resp = client.post(
                "/api/graph/import-graphify",
                json={"content": json.dumps(graph_json), "project": "repo"},
                headers=headers,
            )
            assert resp.status_code == 200
            assert resp.json()["nodes_created"] == 1


# ---------------------------------------------------------------------------
# POST /api/graph/shortest-path - error paths
# ---------------------------------------------------------------------------


def test_shortest_path_rejects_missing_source_id(tmp_path: Path) -> None:
    """source_id is required; omitting it must return 400."""
    client, key = _http_client_and_key(tmp_path)
    headers = {"X-API-Key": key}

    with client:
        resp = client.post(
            "/api/graph/shortest-path",
            json={"target_id": "node-b"},
            headers=headers,
        )

    assert resp.status_code == 400
    assert "source_id" in resp.json()["message"].lower()


def test_shortest_path_rejects_missing_target_id(tmp_path: Path) -> None:
    """target_id is required; omitting it must return 400."""
    client, key = _http_client_and_key(tmp_path)
    headers = {"X-API-Key": key}

    with client:
        resp = client.post(
            "/api/graph/shortest-path",
            json={"source_id": "node-a"},
            headers=headers,
        )

    assert resp.status_code == 400
    assert "target_id" in resp.json()["message"].lower()


def test_shortest_path_rejects_malformed_json(tmp_path: Path) -> None:
    """A non-JSON body must not produce a 2xx response."""
    graph = MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())
    app_server = WaggleServer(graph=graph, config=_config(tmp_path, transport="http"))
    created = graph.create_api_key("local-default", "shortest-path-test")
    app = create_http_application(app_server, app_server.config)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/graph/shortest-path",
            content=b"{not valid json",
            headers={"X-API-Key": created.raw_api_key, "Content-Type": "application/json"},
        )

    assert resp.status_code >= 400


# ---------------------------------------------------------------------------
# POST /api/graph/import-graphify - error paths
# ---------------------------------------------------------------------------


def test_import_graphify_rejects_empty_content(tmp_path: Path) -> None:
    """An empty content string must return 400."""
    client, key = _http_client_and_key(tmp_path)
    headers = {"X-API-Key": key}

    with client:
        resp = client.post(
            "/api/graph/import-graphify",
            json={"content": ""},
            headers=headers,
        )

    assert resp.status_code == 400
    assert "content" in resp.json()["message"].lower()


def test_import_graphify_rejects_malformed_json(tmp_path: Path) -> None:
    """A non-JSON request body must not produce a 2xx response."""
    graph = MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())
    app_server = WaggleServer(graph=graph, config=_config(tmp_path, transport="http"))
    created = graph.create_api_key("local-default", "import-graphify-test")
    app = create_http_application(app_server, app_server.config)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/graph/import-graphify",
            content=b"{not valid json",
            headers={"X-API-Key": created.raw_api_key, "Content-Type": "application/json"},
        )

    assert resp.status_code >= 400


# ---------------------------------------------------------------------------
# GET /api/graph/export - unknown format (cypher added by #419; others still 400)
# ---------------------------------------------------------------------------


def test_graph_export_rejects_unknown_format(tmp_path: Path) -> None:
    """An unrecognised export format must return 400."""
    client, key = _http_client_and_key(tmp_path)
    headers = {"X-API-Key": key}

    with client:
        resp = client.get(
            "/api/graph/export",
            params={"format": "xml"},
            headers=headers,
        )

    assert resp.status_code == 400
    assert "format" in resp.json()["message"].lower()
