from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from starlette.testclient import TestClient

from waggle.config import AppConfig
from waggle.graph import MemoryGraph
from waggle.models import NodeType
from waggle.server import WaggleServer, create_http_application
from waggle.webmcp import compile_project_brief


class FakeEmbeddingModel:
    model_id = "fake-webmcp"

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
    return MemoryGraph(tmp_path / "webmcp.db", FakeEmbeddingModel(), tenant_id="local-default")


def make_http_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        backend="sqlite",
        transport="http",
        model_name="fake-model",
        db_path=str(tmp_path / "webmcp.db"),
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
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )


def seed_project(graph: MemoryGraph, project: str = "waggle-webmcp") -> None:
    graph.add_node(
        label="Project goal",
        content="Build governed shared memory for humans and participating agents.",
        node_type=NodeType.NOTE,
        tags=["goal"],
        project=project,
    )
    graph.add_node(
        label="Local-first architecture",
        content="Waggle remains local-first.",
        node_type=NodeType.DECISION,
        project=project,
    )
    graph.add_node(
        label="Approval boundary",
        content="Human approval is required before proposed changes become authoritative.",
        node_type=NodeType.NOTE,
        tags=["constraint"],
        project=project,
    )
    graph.add_node(
        label="Open deployment question",
        content="Which isolated demo storage should the hosted judge mode use?",
        node_type=NodeType.QUESTION,
        project=project,
    )
    graph.add_node(
        label="Superseded decision",
        content="Use the obsolete storage choice.",
        node_type=NodeType.DECISION,
        project=project,
        valid_to=datetime.now(UTC) - timedelta(minutes=1),
    )
    graph.add_node(
        label="Other project decision",
        content="This must not leak into the brief.",
        node_type=NodeType.DECISION,
        project="other-project",
    )


def test_compile_project_brief_is_scoped_and_authoritative(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    seed_project(graph)

    brief = compile_project_brief(graph, project_id="waggle-webmcp")

    assert brief["project"] == {"id": "waggle-webmcp", "name": "Waggle WebMCP"}
    assert brief["goal"] == "Build governed shared memory for humans and participating agents."
    assert [item["content"] for item in brief["decisions"]] == ["Waggle remains local-first."]
    assert [item["content"] for item in brief["constraints"]] == [
        "Human approval is required before proposed changes become authoritative."
    ]
    assert [item["content"] for item in brief["open_questions"]] == [
        "Which isolated demo storage should the hosted judge mode use?"
    ]
    serialized = str(brief)
    assert "obsolete storage" not in serialized
    assert "Other project" not in serialized


def test_project_brief_http_route_uses_real_waggle_state(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    seed_project(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        response = client.post(
            "/api/webmcp/project-brief",
            json={"project_id": "waggle-webmcp"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["id"] == "waggle-webmcp"
    assert payload["supporting_memory_ids"]
    assert payload["decisions"][0]["authority"] == "authoritative"


def test_project_brief_http_route_rejects_invalid_input(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        response = client.post("/api/webmcp/project-brief", json={"project_id": ["not", "a", "string"]})

    assert response.status_code == 400
    assert response.json()["message"] == "project_id must be a string."
