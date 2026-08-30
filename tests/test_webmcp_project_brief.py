from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from starlette.testclient import TestClient

from waggle.config import AppConfig
from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType
from waggle.server import WaggleServer, create_http_application
from waggle.webmcp import (
    ProposalRepository,
    compile_project_brief,
    project_authority_snapshot,
    recall_authoritative_memory,
)


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


def make_demo_http_config(tmp_path: Path) -> AppConfig:
    return replace(make_http_config(tmp_path), demo_mode=True, demo_cookie_secure=False)


def make_split_demo_http_config(tmp_path: Path) -> AppConfig:
    return replace(
        make_demo_http_config(tmp_path),
        demo_frontend_origin="https://waggle-webmcp.onrender.com",
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


def test_workspace_is_landing_surface_and_graph_studio_remains_available(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        landing = client.get("/?project=waggle-webmcp")
        workspace = client.get("/workspace/proposals?project=waggle-webmcp")
        graph_studio = client.get("/graph?project=waggle-webmcp")

    assert landing.status_code == 200
    assert "<title>Waggle — Shared Memory</title>" in landing.text
    assert workspace.status_code == 200
    assert '"project": "waggle-webmcp"' in workspace.text
    assert graph_studio.status_code == 200
    assert "<title>Waggle Graph Studio</title>" in graph_studio.text


def seed_decision_chain(graph: MemoryGraph, project: str = "waggle-webmcp") -> tuple[str, str, str]:
    graph.enable_dedup = False
    v1 = graph.add_node(
        label="Storage architecture v1",
        content="Use Neo4j for storage.",
        node_type=NodeType.DECISION,
        project=project,
    ).node
    v2 = graph.add_node(
        label="Storage architecture v2",
        content="Use SQLite for storage.",
        node_type=NodeType.DECISION,
        project=project,
    ).node
    graph.add_edge(source_id=v2.id, target_id=v1.id, relationship=RelationType.UPDATES)
    v3 = graph.add_node(
        label="Storage architecture v3",
        content="Use SQLite by default; Neo4j remains optional.",
        node_type=NodeType.DECISION,
        project=project,
    ).node
    graph.add_edge(source_id=v3.id, target_id=v2.id, relationship=RelationType.UPDATES)
    return v1.id, v2.id, v3.id


def test_recall_projects_current_authority_over_update_chain(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    v1_id, v2_id, v3_id = seed_decision_chain(graph)
    graph.add_node(
        label="Expired storage note",
        content="Use files for storage.",
        node_type=NodeType.DECISION,
        project="waggle-webmcp",
        valid_to=datetime.now(UTC) - timedelta(minutes=1),
    )
    graph.add_node(
        label="Future storage note",
        content="Use a future storage architecture.",
        node_type=NodeType.DECISION,
        project="waggle-webmcp",
        valid_from=datetime.now(UTC) + timedelta(days=1),
    )
    graph.add_node(
        label="Other project storage",
        content="Use an unrelated remote database.",
        node_type=NodeType.DECISION,
        project="other-project",
    )

    recall = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="What storage architecture did we decide on?",
        limit=5,
    )

    assert [memory["memory_id"] for memory in recall["memories"]] == [v3_id]
    assert recall["memories"][0] == {
        "memory_id": v3_id,
        "type": "decision",
        "content": "Use SQLite by default; Neo4j remains optional.",
        "status": "authoritative",
        "created_at": recall["memories"][0]["created_at"],
        "updated_at": recall["memories"][0]["updated_at"],
        "source": "waggle",
        "supersedes": v2_id,
    }
    assert v1_id not in str(recall)
    assert v2_id not in [memory["memory_id"] for memory in recall["memories"]]
    assert "unrelated remote" not in str(recall)
    assert "files for storage" not in str(recall)
    assert "future storage" not in str(recall)


def test_workspace_authority_projection_matches_recall_semantics(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    v1_id, v2_id, v3_id = seed_decision_chain(graph)
    future = graph.add_node(
        label="Future",
        content="Future value.",
        node_type=NodeType.FACT,
        project="waggle-webmcp",
        valid_from=datetime.now(UTC) + timedelta(days=1),
    ).node
    expired = graph.add_node(
        label="Expired",
        content="Expired value.",
        node_type=NodeType.FACT,
        project="waggle-webmcp",
        valid_to=datetime.now(UTC) - timedelta(days=1),
    ).node
    historical = graph.add_node(
        label="Historical",
        content="Historical value.",
        node_type=NodeType.FACT,
        project="waggle-webmcp",
        metadata={"knowledge_status": "HISTORICAL"},
    ).node
    rejected = graph.add_node(
        label="Rejected",
        content="Rejected value.",
        node_type=NodeType.FACT,
        project="waggle-webmcp",
        metadata={"head_rejected_reason": "contradicted"},
    ).node
    logical = graph.add_node(
        label="Logical supersession",
        content="Logically superseded value.",
        node_type=NodeType.FACT,
        project="waggle-webmcp",
        metadata={"logically_superseded": True},
    ).node

    projected = project_authority_snapshot(graph.get_graph_snapshot(project="waggle-webmcp"))
    statuses = {node["id"]: node["authority_status"] for node in projected["nodes"]}

    assert statuses[v1_id] == "superseded"
    assert statuses[v2_id] == "superseded"
    assert statuses[v3_id] == "authoritative"
    assert statuses[future.id] == "future"
    assert statuses[expired.id] == "expired"
    assert statuses[historical.id] == "historical"
    assert statuses[rejected.id] == "rejected"
    assert statuses[logical.id] == "superseded"

    brief = compile_project_brief(graph, project_id="waggle-webmcp")
    assert [decision["memory_id"] for decision in brief["decisions"]] == [v3_id]
    assert not {future.id, expired.id, historical.id, rejected.id, logical.id}.intersection(
        brief["supporting_memory_ids"]
    )


def test_recall_http_matches_shared_service_and_empty_recall_is_valid(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    seed_decision_chain(graph)
    expected = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="storage architecture",
        limit=5,
    )
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        response = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture", "limit": 5},
        )
        empty = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "empty-project", "query": "anything", "limit": 5},
        )

    assert response.status_code == 200
    assert response.json() == expected
    assert empty.status_code == 200
    assert empty.json() == {"query": "anything", "project_id": "empty-project", "memories": []}


def test_recall_http_bounds_limit_and_rejects_malformed_project_id(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        oversized = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage", "limit": 11},
        )
        malformed = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle\u0000webmcp", "query": "storage", "limit": 5},
        )

    assert oversized.status_code == 400
    assert oversized.json()["message"] == "limit must be between 1 and 10."
    assert malformed.status_code == 400
    assert malformed.json()["message"] == "project_id contains invalid control characters."


def test_proposal_persists_without_changing_authoritative_memory(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    v1_id, _, v3_id = seed_decision_chain(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)
    before_recall = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="storage architecture",
    )
    before_brief = compile_project_brief(graph, project_id="waggle-webmcp")
    request_payload = {
        "project_id": "waggle-webmcp",
        "memory_id": v3_id,
        "proposed_content": "Use an encrypted SQLite database by default; Neo4j remains optional.",
        "reason": "Preserve local-first storage while making encryption explicit.",
        "evidence_ids": [v1_id],
    }

    with TestClient(app) as client:
        created = client.post("/api/webmcp/proposals", json=request_payload)
        duplicate = client.post("/api/webmcp/proposals", json=request_payload)

    assert created.status_code == 201
    proposal = created.json()
    assert duplicate.status_code == 200
    assert duplicate.json()["proposal_id"] == proposal["proposal_id"]
    assert proposal["status"] == "pending"
    assert proposal["target"]["memory_id"] == v3_id
    assert proposal["target"]["current_content"] == "Use SQLite by default; Neo4j remains optional."
    assert len(proposal["target"]["version"]) == 64
    assert proposal["evidence_ids"] == [v1_id]
    assert proposal["proposed_by"] == {"type": "agent", "id": "webmcp"}

    after_recall = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="storage architecture",
    )
    after_brief = compile_project_brief(graph, project_id="waggle-webmcp")
    assert after_recall == before_recall
    assert after_brief["decisions"] == before_brief["decisions"]
    assert request_payload["proposed_content"] not in str(graph.get_graph_snapshot(project="waggle-webmcp"))

    reloaded_app = create_http_application(app_server, app_server.config)
    with TestClient(reloaded_app) as client:
        listed = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")
    assert listed.status_code == 200
    assert listed.json()["proposals"] == [proposal]


def test_proposal_allows_distinct_changes_to_same_target(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    _, _, v3_id = seed_decision_chain(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        first = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": v3_id,
                "proposed_content": "Use encrypted SQLite by default.",
            },
        )
        second = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": v3_id,
                "proposed_content": "Use SQLite with daily backups by default.",
            },
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["proposal_id"] != second.json()["proposal_id"]


def test_proposal_rejects_non_authoritative_and_cross_project_targets(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    v1_id, v2_id, _ = seed_decision_chain(graph)
    expired = graph.add_node(
        label="Expired target",
        content="An expired target memory.",
        node_type=NodeType.DECISION,
        project="waggle-webmcp",
        valid_to=datetime.now(UTC) - timedelta(minutes=1),
    ).node
    other = graph.add_node(
        label="Other target",
        content="A different project's memory.",
        node_type=NodeType.DECISION,
        project="other-project",
    ).node
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    def propose(client: TestClient, memory_id: str):
        return client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": memory_id,
                "proposed_content": "A proposed replacement.",
            },
        )

    with TestClient(app) as client:
        responses = [
            propose(client, v1_id),
            propose(client, v2_id),
            propose(client, expired.id),
            propose(client, other.id),
            propose(client, "missing-memory"),
        ]

    assert all(response.status_code == 400 for response in responses)
    assert "current authoritative" in responses[0].json()["message"]
    assert "current authoritative" in responses[1].json()["message"]
    assert "current authoritative" in responses[2].json()["message"]
    assert "in this project" in responses[3].json()["message"]
    assert "existing memory" in responses[4].json()["message"]


def test_proposal_validates_content_and_evidence_scope(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    _, _, v3_id = seed_decision_chain(graph)
    other = graph.add_node(
        label="Other evidence",
        content="Evidence from another project.",
        node_type=NodeType.NOTE,
        project="other-project",
    ).node
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        empty = client.post(
            "/api/webmcp/proposals",
            json={"project_id": "waggle-webmcp", "memory_id": v3_id, "proposed_content": "   "},
        )
        cross_project = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": v3_id,
                "proposed_content": "A valid proposal.",
                "evidence_ids": [other.id],
            },
        )
        missing = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": v3_id,
                "proposed_content": "A valid proposal.",
                "evidence_ids": ["missing-evidence"],
            },
        )

    assert empty.status_code == 400
    assert empty.json()["message"] == "proposed_content is required."
    assert cross_project.status_code == 400
    assert "different project" in cross_project.json()["message"]
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["message"]


def seed_governance_target(graph: MemoryGraph, project: str = "waggle-webmcp"):
    graph.enable_dedup = False
    return graph.add_node(
        label="Storage architecture",
        content="Use Neo4j for storage.",
        node_type=NodeType.DECISION,
        project=project,
    ).node


def create_proposal(client: TestClient, memory_id: str, *, project: str = "waggle-webmcp"):
    return client.post(
        "/api/webmcp/proposals",
        json={
            "project_id": project,
            "memory_id": memory_id,
            "proposed_content": "Use SQLite for storage.",
            "reason": "Preserve local-first architecture.",
        },
    )


def test_governance_demo_edit_approve_apply_and_recall(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    target = seed_governance_target(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    before = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="storage architecture",
    )
    assert [memory["content"] for memory in before["memories"]] == ["Use Neo4j for storage."]

    approved_content = "Use SQLite by default; Neo4j remains optional."
    with TestClient(app) as client:
        proposed = create_proposal(client, target.id)
        proposal_id = proposed.json()["proposal_id"]
        after_proposal = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture"},
        )
        reviewed = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve", "approved_content": approved_content},
        )
        immutable = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve", "approved_content": "Agent tries to replace the human value."},
        )
        before_apply = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture"},
        )
        applied = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
        )
        applied_again = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
        )
        approve_applied = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve", "approved_content": "Forbidden replacement."},
        )

    assert proposed.status_code == 201
    assert after_proposal.json()["memories"][0]["content"] == "Use Neo4j for storage."
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["approved_content"] == approved_content
    assert reviewed.json()["proposed_content"] == "Use SQLite for storage."
    assert immutable.status_code == 409
    assert immutable.json()["error"] == "PROPOSAL_NOT_PENDING"
    assert before_apply.json()["memories"][0]["content"] == "Use Neo4j for storage."

    assert applied.status_code == 200
    applied_payload = applied.json()
    result_id = applied_payload["authoritative_memory"]["memory_id"]
    assert applied_payload["authoritative_memory"]["content"] == approved_content
    assert applied_payload["authoritative_memory"]["supersedes"] == target.id
    assert applied_payload["already_applied"] is False
    assert applied_payload["proposal"]["result_memory_id"] == result_id
    assert applied_again.status_code == 200
    assert applied_again.json()["already_applied"] is True
    assert applied_again.json()["authoritative_memory"]["memory_id"] == result_id
    assert approve_applied.status_code == 409
    assert approve_applied.json()["error"] == "PROPOSAL_NOT_PENDING"

    after = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="storage architecture",
    )
    assert [memory["content"] for memory in after["memories"]] == [approved_content]
    assert "Use Neo4j for storage." not in [memory["content"] for memory in after["memories"]]
    brief = compile_project_brief(graph, project_id="waggle-webmcp")
    assert [memory["content"] for memory in brief["decisions"]] == [approved_content]

    historical = graph.get_node(target.id)
    authoritative = graph.get_node(result_id)
    assert historical.valid_to is not None
    assert authoritative.valid_to is None
    assert authoritative.metadata["governance"]["proposal_id"] == proposal_id
    assert authoritative.metadata["governance"]["reviewed_by"] == "local-human"
    history = graph.get_related(node_id=result_id, max_depth=1)
    assert any(
        edge.source_id == result_id and edge.target_id == target.id and edge.relationship == RelationType.UPDATES.value
        for edge in history.edges
    )
    event_types = {event.event_type for event in graph.list_audit_events(limit=100)}
    assert {
        "proposal.created",
        "proposal.edited_and_approved",
        "proposal.applied",
        "memory.superseded",
    } <= event_types


def test_pending_and_rejected_proposals_cannot_be_applied(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    target = seed_governance_target(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        proposal_id = create_proposal(client, target.id).json()["proposal_id"]
        pending_apply = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
        )
        rejected = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "reject", "review_note": "Not the direction we want."},
        )
        rejected_apply = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
        )
        approve_rejected = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve"},
        )

    assert pending_apply.status_code == 409
    assert pending_apply.json()["error"] == "PROPOSAL_NOT_APPROVED"
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["review_note"] == "Not the direction we want."
    assert rejected_apply.status_code == 409
    assert rejected_apply.json()["error"] == "PROPOSAL_NOT_APPROVED"
    assert approve_rejected.status_code == 409
    assert approve_rejected.json()["error"] == "PROPOSAL_NOT_PENDING"
    assert graph.get_node(target.id).valid_to is None
    assert "proposal.rejected" in {event.event_type for event in graph.list_audit_events(limit=100)}


def test_human_apply_commits_only_the_frozen_approved_value(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    target = seed_governance_target(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)
    approved_content = "Use SQLite by default; Neo4j remains optional."

    with TestClient(app) as client:
        proposal_id = create_proposal(client, target.id).json()["proposal_id"]
        reviewed = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve", "approved_content": approved_content},
        )
        rejected_payload = client.post(
            f"/api/webmcp/proposals/{proposal_id}/human-apply",
            json={
                "project_id": "waggle-webmcp",
                "approved_content": "A caller must never be able to replace the reviewed value.",
            },
        )
        applied = client.post(
            f"/api/webmcp/proposals/{proposal_id}/human-apply",
            json={"project_id": "waggle-webmcp"},
        )
        applied_again = client.post(
            f"/api/webmcp/proposals/{proposal_id}/human-apply",
            json={"project_id": "waggle-webmcp"},
        )

    assert reviewed.status_code == 200
    assert rejected_payload.status_code == 400
    assert "approved content cannot be supplied" in rejected_payload.json()["message"]
    assert applied.status_code == 200
    assert applied.json()["authoritative_memory"]["content"] == approved_content
    assert applied_again.status_code == 200
    assert applied_again.json()["already_applied"] is True
    applied_event = next(
        event
        for event in graph.list_audit_events(limit=100)
        if event.event_type == "proposal.applied" and event.resource_id == proposal_id
    )
    assert applied_event.actor_type == "human"
    assert applied_event.actor_id == "local-human"


def test_pending_proposal_becomes_stale_at_review(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    target = seed_governance_target(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        proposal_id = create_proposal(client, target.id).json()["proposal_id"]
        newer = graph.add_node(
            label=target.label,
            content="Use PostgreSQL for storage.",
            node_type=NodeType.DECISION,
            project="waggle-webmcp",
            force_new=True,
        ).node
        graph.add_edge(source_id=newer.id, target_id=target.id, relationship=RelationType.UPDATES)
        review = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve"},
        )
        review_again = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve"},
        )
        listed = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")

    assert review.status_code == 409
    assert review.json()["error"] == "PROPOSAL_STALE"
    assert review_again.status_code == 409
    assert review_again.json()["error"] == "PROPOSAL_NOT_PENDING"
    assert listed.json()["proposals"][0]["status"] == "stale"
    assert "proposal.stale" in {event.event_type for event in graph.list_audit_events(limit=100)}


def test_approved_proposal_becomes_stale_before_apply(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    target = seed_governance_target(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        proposal_id = create_proposal(client, target.id).json()["proposal_id"]
        approved = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve"},
        )
        newer = graph.add_node(
            label=target.label,
            content="Use PostgreSQL for storage.",
            node_type=NodeType.DECISION,
            project="waggle-webmcp",
            force_new=True,
        ).node
        graph.add_edge(source_id=newer.id, target_id=target.id, relationship=RelationType.UPDATES)
        apply = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
        )
        listed = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")

    assert approved.status_code == 200
    assert approved.json()["approved_content"] == "Use SQLite for storage."
    assert "proposal.approved" in {event.event_type for event in graph.list_audit_events(limit=100)}
    assert apply.status_code == 409
    assert apply.json()["error"] == "PROPOSAL_STALE"
    assert listed.json()["proposals"][0]["status"] == "stale"
    recall = recall_authoritative_memory(
        graph,
        project_id="waggle-webmcp",
        query="storage architecture",
    )
    assert [memory["content"] for memory in recall["memories"]] == ["Use PostgreSQL for storage."]


def test_apply_rejects_cross_project_scope_and_content_override(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    target = seed_governance_target(graph)
    app_server = WaggleServer(graph=graph, config=make_http_config(tmp_path))
    app = create_http_application(app_server, app_server.config)

    with TestClient(app) as client:
        proposal_id = create_proposal(client, target.id).json()["proposal_id"]
        client.post(f"/api/webmcp/proposals/{proposal_id}/review", json={"action": "approve"})
        cross_project = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "other-project"},
        )
        override = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp", "content": "Bypass human approval."},
        )

    assert cross_project.status_code == 400
    assert "in this project" in cross_project.json()["message"]
    assert override.status_code == 400
    assert "approved content cannot be supplied" in override.json()["message"]
    assert graph.get_node(target.id).valid_to is None


def test_proposal_repository_migrates_phase3_state_machine(tmp_path: Path) -> None:
    db_path = tmp_path / "phase3-proposals.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE webmcp_memory_proposals (
                proposal_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
                target_memory_id TEXT NOT NULL, target_memory_version TEXT NOT NULL,
                current_content TEXT NOT NULL, proposed_content TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]', proposed_by_type TEXT NOT NULL,
                proposed_by_id TEXT NOT NULL DEFAULT '', dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','applied')),
                created_at TEXT NOT NULL, reviewed_at TEXT DEFAULT NULL, reviewed_by TEXT DEFAULT '',
                approved_content TEXT DEFAULT NULL, applied_at TEXT DEFAULT NULL, result_memory_id TEXT DEFAULT NULL
            );
            INSERT INTO webmcp_memory_proposals (
                proposal_id, tenant_id, project_id, target_memory_id, target_memory_version,
                current_content, proposed_content, proposed_by_type, dedupe_key, created_at
            ) VALUES (
                'proposal_legacy', 'local-default', 'waggle-webmcp', 'memory-v3', 'fingerprint',
                'Old value', 'New value', 'agent', 'dedupe', '2026-08-26T00:00:00+00:00'
            );
            """
        )

    repository = ProposalRepository(db_path)
    proposal = repository.get(tenant_id="local-default", proposal_id="proposal_legacy")

    assert proposal is not None
    assert proposal["status"] == "pending"
    assert proposal["review_note"] == ""
    with sqlite3.connect(db_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'webmcp_memory_proposals'"
        ).fetchone()[0]
    assert "'stale'" in table_sql


def test_fresh_demo_browser_gets_securely_scoped_deterministic_seed(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        landing = client.get("/")
        workspace = client.get("/workspace")
        graph_studio = client.get("/graph?project=waggle-webmcp")
        graph_asset = client.get("/graph-assets/app.js")
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        snapshot = client.get("/api/graph?project=waggle-webmcp")
        brief = client.post("/api/webmcp/project-brief", json={"project_id": "waggle-webmcp"})

    cookie = landing.headers["set-cookie"]
    assert "waggle_demo_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert '"demoMode": true' in landing.text
    assert workspace.status_code == 200
    assert graph_studio.status_code == 200
    assert graph_asset.status_code == 200
    assert "Challenge Demo" in graph_asset.text
    assert live.status_code == 200
    assert ready.status_code == 200
    assert snapshot.status_code == 200
    assert snapshot.json()["tenant_id"] == "challenge-demo"
    assert len(snapshot.json()["nodes"]) == 25
    assert {node["project"] for node in snapshot.json()["nodes"]} == {"waggle-webmcp"}
    assert all("authority_status" in node for node in snapshot.json()["nodes"])
    hero = next(node for node in snapshot.json()["nodes"] if node["label"] == "Storage architecture")
    assert hero["content"] == "Use Neo4j as the primary storage engine."
    assert hero["authority_status"] == "authoritative"
    assert brief.json()["project"] == {"id": "waggle-webmcp", "name": "Waggle WebMCP"}


def test_demo_does_not_accept_portable_abhi_on_the_server(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        client.get("/")
        imported = client.post("/api/webmcp/import-abhi", json={"content_base64": "private-bytes"})
        snapshot = client.get("/api/graph?project=waggle-webmcp")

    assert imported.status_code == 404
    assert any(node["content"] == "Use Neo4j as the primary storage engine." for node in snapshot.json()["nodes"])


def test_demo_cookie_preserves_state_and_all_four_webmcp_tools_use_isolated_scope(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        client.get("/")
        brief = client.post("/api/webmcp/project-brief", json={"project_id": "waggle-webmcp"})
        snapshot = client.get("/api/graph?project=waggle-webmcp").json()
        hero = next(node for node in snapshot["nodes"] if node["label"] == "Storage architecture")
        recall_before = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture", "limit": 5},
        )
        proposal = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": hero["id"],
                "proposed_content": "Use SQLite as the default storage engine.",
                "reason": "Preserve the local-first default.",
                "evidence_ids": [],
            },
        )
        proposal_id = proposal.json()["proposal_id"]
        approved = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve", "approved_content": "Use SQLite by default; Neo4j remains optional."},
        )
        applied = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
        )
        recalled_after = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture", "limit": 5},
        )
        refreshed = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")

    assert brief.status_code == 200
    assert any(
        memory["content"] == "Use Neo4j as the primary storage engine." for memory in recall_before.json()["memories"]
    )
    assert proposal.status_code == 201
    assert proposal.json()["project_id"] == "waggle-webmcp"
    assert approved.json()["approved_content"] == "Use SQLite by default; Neo4j remains optional."
    assert applied.json()["authoritative_memory"]["content"] == "Use SQLite by default; Neo4j remains optional."
    assert any(
        memory["content"] == "Use SQLite by default; Neo4j remains optional."
        for memory in recalled_after.json()["memories"]
    )
    assert refreshed.json()["proposals"][0]["status"] == "applied"


def test_demo_header_preserves_state_when_cross_origin_cookies_are_blocked(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_split_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)
    session_a = {"X-Waggle-Demo-Session": "a" * 64}
    session_b = {"X-Waggle-Demo-Session": "b" * 64}

    with TestClient(app) as client:

        def without_cookies() -> None:
            client.cookies.clear()

        without_cookies()
        brief = client.post(
            "/api/webmcp/project-brief",
            json={"project_id": "waggle-webmcp"},
            headers=session_a,
        )
        without_cookies()
        recall_before = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture", "limit": 5},
            headers=session_a,
        )
        storage_memory = next(
            memory
            for memory in recall_before.json()["memories"]
            if memory["content"] == "Use Neo4j as the primary storage engine."
        )

        without_cookies()
        cross_session_proposal = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": storage_memory["memory_id"],
                "proposed_content": "Attempt cross-session mutation.",
                "reason": "This must remain isolated.",
                "evidence_ids": [],
            },
            headers=session_b,
        )

        without_cookies()
        proposal = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": storage_memory["memory_id"],
                "proposed_content": "Use SQLite as the default storage engine.",
                "reason": "Preserve the local-first default.",
                "evidence_ids": [],
            },
            headers=session_a,
        )
        proposal_id = proposal.json()["proposal_id"]

        without_cookies()
        approved = client.post(
            f"/api/webmcp/proposals/{proposal_id}/review",
            json={"action": "approve", "approved_content": "Use SQLite by default; Neo4j remains optional."},
            headers=session_a,
        )
        without_cookies()
        applied = client.post(
            f"/api/webmcp/proposals/{proposal_id}/apply",
            json={"project_id": "waggle-webmcp"},
            headers=session_a,
        )
        without_cookies()
        recall_after = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture", "limit": 5},
            headers=session_a,
        )
        without_cookies()
        reset = client.post("/api/webmcp/demo/reset", json={}, headers=session_a)
        without_cookies()
        recall_reset = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "waggle-webmcp", "query": "storage architecture", "limit": 5},
            headers=session_a,
        )
        without_cookies()
        preflight = client.options(
            "/api/webmcp/recall-memory",
            headers={
                "Origin": "https://waggle-webmcp.onrender.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, X-Waggle-Demo-Session",
            },
        )

    assert brief.status_code == 200
    assert cross_session_proposal.status_code == 400
    assert proposal.status_code == 201
    assert approved.json()["approved_content"] == "Use SQLite by default; Neo4j remains optional."
    assert applied.json()["authoritative_memory"]["content"] == "Use SQLite by default; Neo4j remains optional."
    assert any(
        memory["content"] == "Use SQLite by default; Neo4j remains optional."
        for memory in recall_after.json()["memories"]
    )
    assert reset.status_code == 200
    assert any(
        memory["content"] == "Use Neo4j as the primary storage engine." for memory in recall_reset.json()["memories"]
    )
    assert preflight.status_code == 200
    assert "x-waggle-demo-session" in preflight.headers["access-control-allow-headers"].lower()


def test_demo_sessions_are_independent_and_reset_cannot_affect_another_browser(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        client.get("/")
        cookie_a = client.cookies.get("waggle_demo_session")
        client.cookies.clear()
        client.get("/")
        cookie_b = client.cookies.get("waggle_demo_session")

        def use_session(cookie: str) -> None:
            client.cookies.clear()
            client.cookies.set("waggle_demo_session", cookie)

        use_session(cookie_a)
        snapshot_a = client.get("/api/graph?project=waggle-webmcp").json()
        use_session(cookie_b)
        snapshot_b = client.get("/api/graph?project=waggle-webmcp").json()
        hero_a = next(node for node in snapshot_a["nodes"] if node["label"] == "Storage architecture")
        hero_b = next(node for node in snapshot_b["nodes"] if node["label"] == "Storage architecture")
        use_session(cookie_a)
        proposal_a = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": hero_a["id"],
                "proposed_content": "Use SQLite as the default storage engine.",
                "reason": "Local-first architecture.",
                "evidence_ids": [],
            },
        )
        use_session(cookie_b)
        proposals_b_before = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")
        cross_session_target = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": hero_a["id"],
                "proposed_content": "Attempt cross-session mutation.",
                "reason": "Should fail.",
                "evidence_ids": [],
            },
        )
        proposal_b = client.post(
            "/api/webmcp/proposals",
            json={
                "project_id": "waggle-webmcp",
                "memory_id": hero_b["id"],
                "proposed_content": "Judge B proposal survives Judge A reset.",
                "reason": "Isolation proof.",
                "evidence_ids": [],
            },
        )
        use_session(cookie_a)
        reset_a = client.post("/api/webmcp/demo/reset", json={})
        proposals_a_after = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")
        use_session(cookie_b)
        proposals_b_after = client.get("/api/webmcp/proposals?project_id=waggle-webmcp")
        use_session(cookie_a)
        reset_snapshot_a = client.get("/api/graph?project=waggle-webmcp").json()

    assert hero_a["id"] != hero_b["id"]
    assert proposal_a.status_code == 201
    assert proposals_b_before.json()["proposals"] == []
    assert cross_session_target.status_code == 400
    assert proposal_b.status_code == 201
    assert reset_a.json()["authoritative_memory_count"] == 24
    assert proposals_a_after.json()["proposals"] == []
    assert proposals_b_after.json()["proposals"][0]["proposal_id"] == proposal_b.json()["proposal_id"]
    reset_hero = next(node for node in reset_snapshot_a["nodes"] if node["label"] == "Storage architecture")
    assert reset_hero["id"] == hero_a["id"]
    assert reset_hero["content"] == "Use Neo4j as the primary storage engine."


def test_demo_webmcp_project_alias_cannot_escape_physical_namespace(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        client.get("/")
        other_project = client.post("/api/webmcp/project-brief", json={"project_id": "other-project"})
        guessed_physical = client.post(
            "/api/webmcp/recall-memory",
            json={"project_id": "demo_guessed_waggle-webmcp", "query": "storage"},
        )
        graph_escape = client.get("/api/graph?project=other-project")

    for response in (other_project, guessed_physical, graph_escape):
        assert response.status_code == 400
        assert "must be 'waggle-webmcp'" in response.json()["message"]


def test_split_demo_backend_allows_only_configured_frontend_with_credentials(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    config = make_split_demo_http_config(tmp_path)
    app_server = WaggleServer(graph=graph, config=config)
    app = create_http_application(app_server, config)

    with TestClient(app) as client:
        session = client.get("/api/graph?project=waggle-webmcp")
        allowed = client.options(
            "/api/webmcp/project-brief",
            headers={
                "Origin": "https://waggle-webmcp.onrender.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        denied = client.options(
            "/api/webmcp/project-brief",
            headers={
                "Origin": "https://example.invalid",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert allowed.status_code == 200
    assert "SameSite=None" in session.headers["set-cookie"]
    assert "Secure" in session.headers["set-cookie"]
    assert allowed.headers["access-control-allow-origin"] == "https://waggle-webmcp.onrender.com"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
