from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from waggle.config import AppConfig
from waggle.embeddings import EmbeddingModel
from waggle.errors import ValidationFailure
from waggle.graph import MemoryGraph
from waggle.models import NodeType
from waggle.server import WaggleServer, create_http_application
from waggle.webmcp import (
    ProposalRepository,
    apply_approved_memory_change,
    compile_project_brief,
    normalize_git_remote,
    propose_memory_change,
    recall_authoritative_memory,
    refresh_project_context,
    register_project,
    resolve_active_project,
    resolve_project_identity,
    review_memory_change,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repo(tmp_path: Path, name: str, remote: str = "") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Waggle Tests")
    if remote:
        _git(root, "remote", "add", "origin", remote)
    (root / "README.md").write_text(f"# {name}\n\n{name} keeps durable project context.\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "example"\n[project.scripts]\ntest-example = "example:test"\n',
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial project")
    return root


def _graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", EmbeddingModel("deterministic"), tenant_id="tenant-a")


def _http_config(tmp_path: Path, workspace: Path) -> AppConfig:
    return AppConfig(
        backend="sqlite",
        transport="http",
        model_name="deterministic",
        db_path=str(tmp_path / "memory.db"),
        default_tenant_id="tenant-a",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=4,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
        webmcp_workspace_path=str(workspace),
    )


def test_remote_normalization_and_identity_are_stable_across_checkouts(tmp_path: Path) -> None:
    assert normalize_git_remote("git@GitHub.com:Example/Project.git") == "https://github.com/Example/Project"
    first = _repo(tmp_path, "first", "git@GitHub.com:Example/Project.git")
    second = _repo(tmp_path, "second", "https://github.com/Example/Project/")

    first_identity = resolve_project_identity(first)
    second_identity = resolve_project_identity(second)

    assert first_identity.project_id == second_identity.project_id
    assert first_identity.identity_source == "git_remote"
    assert first_identity.repository == "https://github.com/Example/Project"


def test_path_fallback_is_stable_and_different_repositories_do_not_collide(tmp_path: Path) -> None:
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    first = resolve_project_identity(workspace)
    second = resolve_project_identity(workspace / ".")
    other = _repo(tmp_path, "other", "https://github.com/acme/other.git")

    assert first.project_id == second.project_id
    assert first.identity_source == "workspace_path"
    assert first.project_id != resolve_project_identity(other).project_id


def test_repeated_refresh_handles_reverts_deletions_and_tenant_isolation(tmp_path: Path) -> None:
    root = _repo(tmp_path, "repeat")
    graph = _graph(tmp_path)
    project_id = register_project(graph, root)["project"]["project_id"]
    other_tenant = graph.for_tenant("tenant-b")
    assert register_project(other_tenant, root)["project"]["project_id"] == project_id
    assert len(register_project(graph, root)["refresh"]["added_memory_ids"]) == 0

    original = (root / "README.md").read_text()
    (root / "README.md").write_text("# Repeat\n\nChanged purpose.\n")
    refresh_project_context(graph, project_id)
    (root / "README.md").write_text(original)
    refresh_project_context(graph, project_id)
    assert compile_project_brief(graph)["purpose"] == "repeat keeps durable project context."
    (root / "README.md").unlink()
    deleted = refresh_project_context(graph, project_id)
    assert "purpose" in deleted["changed_categories"]
    assert refresh_project_context(graph, project_id)["added_memory_ids"] == []
    assert compile_project_brief(other_tenant)["purpose"] == "repeat keeps durable project context."


def test_another_checkout_reuses_memory_and_refreshes_the_open_checkout(tmp_path: Path) -> None:
    first = _repo(tmp_path, "first", "git@github.com:acme/shared.git")
    second = _repo(tmp_path, "second", "https://github.com/acme/shared.git")
    graph = _graph(tmp_path)
    project_id = register_project(graph, first)["project"]["project_id"]
    assert register_project(graph, second)["created"] is False
    brief = compile_project_brief(graph)
    assert brief["project"]["root"] == str(second)
    assert brief["project"]["id"] == project_id
    assert brief["purpose"] == "second keeps durable project context."


def test_register_reuses_project_and_persists_repository_context(tmp_path: Path) -> None:
    root = _repo(tmp_path, "product", "https://github.com/acme/product.git")
    graph = _graph(tmp_path)

    first = register_project(graph, root)
    second = register_project(graph, root)
    project_id = first["project"]["project_id"]

    assert first["created"] is True
    assert second["created"] is False
    assert resolve_active_project(graph) == project_id
    brief = compile_project_brief(graph)
    assert brief["project"]["name"] == "product"
    assert brief["project"]["repository"] == "https://github.com/acme/product"
    assert brief["purpose"] == "product keeps durable project context."
    assert brief["tech_stack"] == ["Python"]
    assert brief["architecture"]
    assert {item["category"] for item in brief["repository_context"]} >= {
        "purpose",
        "stack",
        "commands",
        "recent_commits",
    }
    assert all(item["authority"] == "source_observation" for item in brief["repository_context"])
    assert not any(item["authority"] == "source_observation" for item in brief["decisions"])

    reopened = MemoryGraph(graph.db_path, EmbeddingModel("deterministic"), tenant_id="tenant-a")
    assert compile_project_brief(reopened)["project"]["id"] == project_id


def test_repository_purpose_skips_html_branding_and_tolerates_malformed_manifest(tmp_path: Path) -> None:
    root = _repo(tmp_path, "branded")
    (root / "README.md").write_text(
        '<!-- registry-only marker -->\n<p><img src="logo.png" /></p>\n\n'
        "<p><strong>Durable project memory.</strong><br/>For humans &amp; agents.</p>\n"
    )
    (root / "pyproject.toml").write_text('project = "malformed project table"\n')
    graph = _graph(tmp_path)
    register_project(graph, root)
    brief = compile_project_brief(graph)
    assert brief["purpose"] == "Durable project memory. For humans & agents."
    assert brief["purpose_authority"] == "source_observation"
    assert brief["purpose_provenance"]["provenance"]["path"] == "README.md"


def test_http_bootstrap_and_brief_default_to_configured_workspace(tmp_path: Path) -> None:
    root = _repo(tmp_path, "connected", "https://github.com/acme/connected.git")
    graph = _graph(tmp_path)
    config = _http_config(tmp_path, root)
    server = WaggleServer(graph=graph, config=config)
    app = create_http_application(server, config)

    with TestClient(app) as client:
        brief_response = client.post("/api/webmcp/project-brief", json={})
        refresh_response = client.post("/api/webmcp/projects/refresh", json={})
        denied = client.post("/api/webmcp/projects/register", json={"workspace_path": str(tmp_path)})
        cross_site = client.post("/api/webmcp/projects/refresh", json={}, headers={"Origin": "https://other.test"})

    assert brief_response.status_code == 200
    assert brief_response.json()["project"]["repository"] == "https://github.com/acme/connected"
    assert refresh_response.status_code == 200
    assert refresh_response.json()["project"]["project_id"] == brief_response.json()["project"]["id"]
    assert denied.status_code == 400
    assert cross_site.status_code == 400


def test_sqlite_webmcp_http_is_allowed_only_on_loopback(tmp_path: Path) -> None:
    root = _repo(tmp_path, "loopback")
    config = _http_config(tmp_path, root)
    config.validate()
    config.http_host = "0.0.0.0"
    with pytest.raises(ValidationFailure, match="local-only"):
        config.validate()


def test_repository_governance_http_flow_survives_new_session(tmp_path: Path) -> None:
    root = _repo(tmp_path, "governance", "https://github.com/acme/governance.git")
    graph = _graph(tmp_path)
    project_id = register_project(graph, root)["project"]["project_id"]
    # A pre-existing human decision, not an automatically trusted README claim.
    target = graph.add_node(
        label="Storage architecture",
        content="SQLite is the default; Neo4j remains optional.",
        node_type=NodeType.DECISION,
        project=project_id,
        force_new=True,
    ).node
    other = _repo(tmp_path, "other")
    register_project(graph, other)
    config = _http_config(tmp_path, root)
    with TestClient(create_http_application(WaggleServer(graph=graph, config=config), config)) as client:
        assert client.post("/api/webmcp/project-brief", json={}).json()["project"]["id"] == project_id
        before = client.post("/api/webmcp/recall-memory", json={"query": "storage architecture"}).json()
        assert before["memories"][0]["memory_id"] == target.id
        proposal = client.post(
            "/api/webmcp/proposals",
            json={
                "memory_id": target.id,
                "proposed_content": "Use Neo4j as the default persistence backend.",
            },
        ).json()
        proposal_path = f"/api/webmcp/proposals/{proposal['proposal_id']}"
        assert client.post(f"{proposal_path}/apply", json={}).status_code == 409
        assert client.post(f"{proposal_path}/review", json={"action": "approve"}).status_code == 200
        assert client.post(f"{proposal_path}/apply", json={"content": "unreviewed"}).status_code == 400
        applied = client.post(f"{proposal_path}/apply", json={}).json()
        assert applied["authoritative_memory"]["content"] == proposal["proposed_content"]
        assert client.post(f"{proposal_path}/apply", json={}).json()["already_applied"] is True

    reopened = MemoryGraph(graph.db_path, EmbeddingModel("deterministic"), tenant_id="tenant-a")
    with TestClient(create_http_application(WaggleServer(graph=reopened, config=config), config)) as client:
        brief = client.post("/api/webmcp/project-brief", json={}).json()
        assert [item["content"] for item in brief["authoritative_decisions"]] == [proposal["proposed_content"]]
        assert before["memories"][0]["content"] not in str(brief["authoritative_decisions"])


def test_project_isolation_and_governed_change_flow_use_active_project(tmp_path: Path) -> None:
    first_root = _repo(tmp_path, "alpha", "https://github.com/acme/alpha.git")
    second_root = _repo(tmp_path, "beta", "https://github.com/acme/beta.git")
    graph = _graph(tmp_path)
    first_id = register_project(graph, first_root)["project"]["project_id"]
    second_id = register_project(graph, second_root)["project"]["project_id"]
    target = graph.add_node(
        label="Storage architecture",
        content="Use PostgreSQL for durable state.",
        node_type=NodeType.DECISION,
        project=first_id,
        tags=["storage"],
        force_new=True,
    ).node
    graph.add_node(
        label="Other project secret",
        content="Use an unrelated private service.",
        node_type=NodeType.DECISION,
        project=second_id,
        force_new=True,
    )

    recall = recall_authoritative_memory(graph, project_id=first_id, query="storage", limit=5)
    assert [memory["content"] for memory in recall["memories"]] == ["Use PostgreSQL for durable state."]
    assert "unrelated private service" not in str(compile_project_brief(graph, project_id=first_id))

    repository = ProposalRepository(graph.db_path)
    proposal, _ = propose_memory_change(
        graph,
        repository,
        project_id=first_id,
        memory_id=target.id,
        proposed_content="Use SQLite for local-first durable state.",
    )
    review_memory_change(graph, repository, proposal_id=proposal["proposal_id"], action="approve")
    applied = apply_approved_memory_change(
        graph,
        repository,
        proposal_id=proposal["proposal_id"],
        project_id=first_id,
    )
    assert applied["authoritative_memory"]["content"] == "Use SQLite for local-first durable state."
    corrected = recall_authoritative_memory(graph, project_id=first_id, query="storage", limit=5)
    assert corrected["memories"][0]["content"] == "Use SQLite for local-first durable state."


def test_refresh_adds_observation_lineage_without_rewriting_authoritative_memory(tmp_path: Path) -> None:
    root = _repo(tmp_path, "refresh", "https://github.com/acme/refresh.git")
    graph = _graph(tmp_path)
    project_id = register_project(graph, root)["project"]["project_id"]
    decision = graph.add_node(
        label="Product purpose",
        content="Keep the human-approved product purpose.",
        node_type=NodeType.DECISION,
        project=project_id,
        force_new=True,
    ).node

    (root / "README.md").write_text("# Refresh\n\nThe repository now describes a different implementation direction.\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "change repository purpose")
    refreshed = refresh_project_context(graph, project_id)

    assert "purpose" in refreshed["changed_categories"]
    assert refreshed["conflicts"]
    assert any(item.get("authoritative_memory_id") == decision.id for item in refreshed["conflicts"])
    assert graph.get_node(decision.id).content == "Keep the human-approved product purpose."
    brief = compile_project_brief(graph, project_id=project_id)
    assert brief["decisions"][0]["content"] == "Keep the human-approved product purpose."
    assert brief["repository_conflicts"][0]["category"] in {"purpose", "recent_commits"}
    purpose = next(item for item in brief["repository_context"] if item["category"] == "purpose")
    assert "different implementation direction" in purpose["content"]
