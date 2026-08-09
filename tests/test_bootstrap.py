from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from waggle.bootstrap import bootstrap_repository, plan_repository_bootstrap
from waggle.models import ContextTimelineItem, Node, NodeStoreResult, NodeType, TimelineResult
from waggle.server.cli import _build_parser, _run_timeline, _snippet, _timeline_event_matches

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeBootstrapGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def add_node(self, **kwargs: object) -> NodeStoreResult:
        self.calls.append(kwargs)
        return NodeStoreResult(
            node=Node(
                label=str(kwargs["label"]),
                content=str(kwargs["content"]),
                node_type=kwargs["node_type"],  # type: ignore[arg-type]
                tags=kwargs.get("tags") or [],  # type: ignore[arg-type]
                project=str(kwargs.get("project") or ""),
                agent_id=str(kwargs.get("agent_id") or ""),
                session_id=str(kwargs.get("session_id") or ""),
                metadata=kwargs.get("metadata") or {},  # type: ignore[arg-type]
            ),
            created=True,
        )


def test_plan_repository_bootstrap_reads_high_signal_files_only(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n\nUse PostgreSQL for app data.\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "secret.py").write_text("API_KEY = 'do-not-bootstrap'\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text("ADR: local-first memory.\n", encoding="utf-8")

    candidates = plan_repository_bootstrap(tmp_path, include_git=False)

    paths = {candidate.path for candidate in candidates}
    assert "README.md" in paths
    assert "pyproject.toml" in paths
    assert "docs/architecture.md" in paths
    assert "src/secret.py" not in paths
    assert all("project-bootstrap" in candidate.tags for candidate in candidates)


def test_plan_repository_bootstrap_limits_file_size(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("a" * 100, encoding="utf-8")

    [candidate] = plan_repository_bootstrap(tmp_path, include_git=False, max_file_bytes=10)

    assert len(candidate.content) < 80
    assert candidate.metadata["bytes_read"] == 10


def test_plan_repository_bootstrap_keeps_truncated_utf8_prefix(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("abcérest", encoding="utf-8")

    [candidate] = plan_repository_bootstrap(tmp_path, include_git=False, max_file_bytes=4)

    assert candidate.content == "Source file: README.md\n\nabc"


def test_bootstrap_repository_dry_run_does_not_write(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    graph = FakeBootstrapGraph()

    result = bootstrap_repository(graph, tmp_path, dry_run=True)

    assert result.project == tmp_path.name
    assert result.nodes_created == 0
    assert graph.calls == []


def test_bootstrap_repository_writes_project_scoped_nodes(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Use Waggle automatically.\n", encoding="utf-8")
    graph = FakeBootstrapGraph()

    result = bootstrap_repository(graph, tmp_path, project="MCP", agent_id="tester", session_id="bootstrap")

    assert result.nodes_created == 1
    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call["project"] == "MCP"
    assert call["agent_id"] == "tester"
    assert call["session_id"] == "bootstrap"
    assert call["node_type"] == NodeType.PREFERENCE
    assert "agent-instructions" in call["tags"]


def test_timeline_filters_before_applying_requested_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class TimelineGraph:
        requested_limits: list[int] = []

        def timeline(self, **kwargs):
            self.requested_limits.append(kwargs["limit"])
            items = [
                ContextTimelineItem(kind="node_updated", timestamp=datetime.now(UTC), label=f"updated-{index}")
                for index in range(90)
            ]
            items.extend(
                ContextTimelineItem(kind="node_created", timestamp=datetime.now(UTC), label=f"created-{index}")
                for index in range(10)
            )
            return TimelineResult(scope="tenant", items=items[: kwargs["limit"]])

    graph = TimelineGraph()
    monkeypatch.setattr("waggle.server.cli._build_backend", lambda _config: graph)
    args = SimpleNamespace(
        db="",
        model="",
        node_id="",
        query="",
        limit=3,
        max_depth=2,
        include_evidence=True,
        events="created",
        json_output=True,
    )

    assert _run_timeline(SimpleNamespace(db_path="memory.db"), args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert graph.requested_limits == [100]
    assert [item["kind"] for item in payload["items"]] == ["node_created", "node_created", "node_created"]


def test_parser_exposes_project_memory_search_command() -> None:
    parser = _build_parser()

    args = parser.parse_args(
        [
            "search",
            "database decision",
            "--project",
            "MCP",
            "--mode",
            "hybrid",
            "--max-nodes",
            "3",
            "--model",
            "deterministic",
        ]
    )

    assert args.command == "search"
    assert args.query == "database decision"
    assert args.project == "MCP"
    assert args.mode == "hybrid"
    assert args.max_nodes == 3


def test_parser_exposes_project_memory_stats_command() -> None:
    parser = _build_parser()

    args = parser.parse_args(["stats", "--db", "/tmp/waggle.db", "--model", "deterministic", "--json"])

    assert args.command == "stats"
    assert args.db == "/tmp/waggle.db"
    assert args.model == "deterministic"
    assert args.json_output is True


def test_parser_exposes_project_memory_timeline_command() -> None:
    parser = _build_parser()

    args = parser.parse_args(
        [
            "timeline",
            "--query",
            "database decision",
            "--limit",
            "5",
            "--max-depth",
            "1",
            "--events",
            "created",
            "--no-include-evidence",
        ]
    )

    assert args.command == "timeline"
    assert args.query == "database decision"
    assert args.limit == 5
    assert args.max_depth == 1
    assert args.events == "created"
    assert args.include_evidence is False


def test_parser_exposes_inspect_node_command() -> None:
    parser = _build_parser()

    args = parser.parse_args(
        [
            "inspect-node",
            "node-123",
            "--max-depth",
            "2",
            "--db",
            "/tmp/waggle.db",
            "--model",
            "deterministic",
            "--full",
            "--json",
        ]
    )

    assert args.command == "inspect-node"
    assert args.node_id == "node-123"
    assert args.max_depth == 2
    assert args.db == "/tmp/waggle.db"
    assert args.model == "deterministic"
    assert args.full is True
    assert args.json_output is True


def test_timeline_event_filter_matches_expected_kinds() -> None:
    assert _timeline_event_matches("node_created", "created") is True
    assert _timeline_event_matches("node_updated", "created") is False
    assert _timeline_event_matches("edge_depends_on", "edges") is True
    assert _timeline_event_matches("evidence", "evidence") is True
    assert _timeline_event_matches("node_updated", "all") is True


def test_project_memory_cli_docs_cover_terminal_workflow() -> None:
    commands = ["bootstrap", "stats", "search", "timeline", "inspect-node"]
    docs_text = (REPO_ROOT / "docs" / "project-memory-cli.md").read_text(encoding="utf-8")
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    reference_text = (REPO_ROOT / "docs" / "reference.md").read_text(encoding="utf-8")

    for command in commands:
        assert f"waggle-mcp {command}" in docs_text
        assert f"waggle-mcp {command}" in readme_text
        assert f"waggle-mcp {command}" in reference_text


def test_snippet_normalizes_whitespace_and_truncates() -> None:
    snippet = _snippet("alpha\n\n beta\tgamma delta", limit=18)

    assert snippet == "alpha beta gamm..."
