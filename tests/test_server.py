from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import waggle
from waggle.config import AppConfig
from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType
from waggle.server import (
    AUTOMATIC_MEMORY_RULE_TEXT,
    WaggleServer,
    _assert_runtime_feature_parity,
    _build_parser,
    _default_graph,
    _run_admin_command,
    _run_doctor,
    _run_graph_editor_command,
    _run_setup,
    _setup_clients_from_args,
    _write_antigravity,
    _write_codex,
    _write_codex_agents,
    _write_gemini,
    _write_other,
)


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
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


def make_app(tmp_path: Path) -> WaggleServer:
    graph = MemoryGraph(tmp_path / "server-memory.db", FakeEmbeddingModel())
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(tmp_path / "server-memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )
    return WaggleServer(graph=graph, config=config)


def write_waggle_codex_config(home: Path, db_path: Path) -> None:
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    normalized_db_path = db_path.as_posix()
    (codex_dir / "config.toml").write_text(
        "\n".join(
            [
                "[mcp_servers.waggle]",
                'command = "waggle-mcp"',
                "",
                "[mcp_servers.waggle.env]",
                f'WAGGLE_DB_PATH = "{normalized_db_path}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_store_node_and_stats_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    result = app.handle_tool_call(
        "store_node",
        {
            "label": "User Preference",
            "content": "User prefers Python for backend development",
            "node_type": NodeType.PREFERENCE.value,
            "tags": ["python"],
        },
    )
    assert "Stored node" in result.content[0].text

    stats_result = app.handle_tool_call("get_stats", {})
    assert "Memory Graph Stats" in stats_result.content[0].text
    assert stats_result.structuredContent["total_nodes"] == 1
    assert stats_result.structuredContent["total_repos"] == 1
    assert stats_result.structuredContent["total_context_windows"] == 1
    assert "Context windows: 1" in stats_result.content[0].text


def test_tool_schemas_are_glama_friendly(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    for tool in app.build_tools():
        assert tool.description
        assert tool.inputSchema["type"] == "object"
        assert tool.inputSchema["additionalProperties"] is False
        assert isinstance(tool.inputSchema["properties"], dict)
        for field_name, field_schema in tool.inputSchema["properties"].items():
            assert field_schema.get("description"), f"{tool.name}.{field_name} is missing a description"


def test_parser_accepts_graph_editor_commands() -> None:
    parser = _build_parser()

    create_api_key_args = parser.parse_args(
        [
            "create-api-key",
            "--tenant-id",
            "workspace-a",
            "--name",
            "prod-agent",
            "--expires-in-days",
            "30",
            "--created-by",
            "ops@example.com",
            "--scopes",
            "graph:read,admin:read",
        ]
    )
    list_audit_args = parser.parse_args(
        ["list-audit-events", "--tenant-id", "workspace-a", "--type", "api_key.created", "--limit", "25"]
    )
    retention_status_args = parser.parse_args(["retention-status", "--tenant-id", "workspace-a"])
    set_retention_args = parser.parse_args(
        ["set-retention", "--tenant-id", "workspace-a", "--enabled", "--days", "90", "--interval-hours", "12"]
    )
    prune_retention_args = parser.parse_args(["prune-retention", "--tenant-id", "workspace-a", "--batch-size", "250"])
    edit_args = parser.parse_args(["edit-graph", "--port", "8787", "--no-open"])
    view_args = parser.parse_args(["view-graph"])
    diff_args = parser.parse_args(["diff", "--file-a", "a.abhi", "--file-b", "b.abhi"])
    merge_args = parser.parse_args(
        [
            "merge",
            "--base",
            "base.abhi",
            "--left",
            "left.abhi",
            "--right",
            "right.abhi",
            "--output",
            "merged.abhi",
            "--merge-strategy",
            "last_write_wins",
        ]
    )
    query_args = parser.parse_args(["query", "--input", "memory.abhi", "--query-id", "q1"])
    load_chunks_args = parser.parse_args(["load-chunks", "--input", "memory.abhi", "--chunk-id", "decision_1"])
    checkpoint_args = parser.parse_args(
        ["checkpoint-context", "--project", "MCP", "--session-id", "thread-1", "--output", "handoff.abhi"]
    )
    clear_session_args = parser.parse_args(["clear-session", "--session-id", "thread-1", "--yes"])
    clear_project_args = parser.parse_args(["clear-project", "--project", "MCP", "--yes"])
    clear_all_args = parser.parse_args(["clear-all", "--yes"])
    doctor_json_args = parser.parse_args(["doctor", "--json"])
    push_args = parser.parse_args(["push", "--client-secret-path", "client.json", "--folder-id", "folder123"])
    pull_args = parser.parse_args(["pull", "file123", "--client-secret-path", "client.json"])
    share_args = parser.parse_args(["share", "file123", "--client-secret-path", "client.json"])

    assert create_api_key_args.command == "create-api-key"
    assert create_api_key_args.expires_in_days == 30
    assert create_api_key_args.created_by == "ops@example.com"
    assert create_api_key_args.scopes == "graph:read,admin:read"
    assert list_audit_args.command == "list-audit-events"
    assert list_audit_args.event_type == "api_key.created"
    assert list_audit_args.limit == 25
    assert retention_status_args.command == "retention-status"
    assert retention_status_args.tenant_id == "workspace-a"
    assert set_retention_args.command == "set-retention"
    assert set_retention_args.enabled is True
    assert set_retention_args.days == 90
    assert set_retention_args.interval_hours == 12
    assert prune_retention_args.command == "prune-retention"
    assert prune_retention_args.batch_size == 250
    assert edit_args.command == "edit-graph"
    assert edit_args.port == 8787
    assert edit_args.open is False
    assert view_args.command == "view-graph"
    assert view_args.open is True
    assert diff_args.command == "diff"
    assert diff_args.input_path_a_flag == "a.abhi"
    assert merge_args.command == "merge"
    assert merge_args.merge_strategy == "last_write_wins"
    assert query_args.command == "query"
    assert query_args.query_id == "q1"
    assert load_chunks_args.command == "load-chunks"
    assert load_chunks_args.chunk_ids == ["decision_1"]
    assert checkpoint_args.command == "checkpoint-context"
    assert checkpoint_args.project == "MCP"
    assert checkpoint_args.session_id == "thread-1"
    assert clear_session_args.command == "clear-session"
    assert clear_session_args.session_id == "thread-1"
    assert clear_session_args.yes is True
    assert clear_project_args.command == "clear-project"
    assert clear_project_args.project == "MCP"
    assert clear_project_args.yes is True
    assert clear_all_args.command == "clear-all"
    assert clear_all_args.yes is True
    assert doctor_json_args.command == "doctor"
    assert doctor_json_args.json_output is True
    assert push_args.command == "push"
    assert push_args.encrypt is True
    assert push_args.folder_id == "folder123"
    assert pull_args.command == "pull"
    assert pull_args.file_ref == "file123"
    assert share_args.command == "share"
    assert share_args.file_ref == "file123"


def test_doctor_flags_mixed_embedding_model_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "server-memory.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    write_waggle_codex_config(tmp_path, db_path)
    graph = MemoryGraph(db_path, FakeEmbeddingModel())
    graph.observe_conversation(
        user_message="Use FastAPI.",
        assistant_response="Understood.",
        session_id="mixed-models",
        project="audit",
    )

    with graph._lock, graph._connect() as connection:
        connection.execute(
            """
            UPDATE transcript_records
            SET embedding_model_id = 'legacy-model:v0'
            WHERE tenant_id = ? AND session_id = ? AND role = 'assistant'
            """,
            (graph.tenant_id, "mixed-models"),
        )

    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(db_path),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    exit_code = _run_doctor(config)
    stdout = capsys.readouterr().out

    assert exit_code == 1
    assert "Mixed embedding model IDs detected" in stdout


def test_doctor_fix_reembeds_mixed_embedding_model_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "server-memory.db"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    write_waggle_codex_config(tmp_path, db_path)
    graph = MemoryGraph(db_path, FakeEmbeddingModel())
    graph.observe_conversation(
        user_message="Use FastAPI.",
        assistant_response="Understood.",
        session_id="mixed-models",
        project="audit",
    )

    with graph._lock, graph._connect() as connection:
        connection.execute(
            """
            UPDATE transcript_records
            SET embedding_model_id = 'legacy-model:v0'
            WHERE tenant_id = ? AND session_id = ? AND role = 'assistant'
            """,
            (graph.tenant_id, "mixed-models"),
        )

    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(db_path),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    exit_code = _run_doctor(config, fix=True)
    stdout = capsys.readouterr().out

    assert exit_code == 0
    assert "Re-embedded stale rows" in stdout
    repaired = graph.get_embedding_store_health()
    assert repaired["mixed_models"] is False
    assert repaired["transcript_stale_rows"] == 0


def test_doctor_json_output_reports_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))

    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="deterministic",
        db_path=str(tmp_path / "server-memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    exit_code = _run_doctor(config, json_output=True)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert "\x1b[" not in captured.out
    assert payload["schema_version"] == 1
    assert payload["platform"]
    assert payload["status"] == "issues_found"
    assert payload["warnings"] == []
    assert payload["fix_requested"] is False
    assert any("No MCP client config file" in issue for issue in payload["issues"])
    assert "Deterministic model — no download needed" in payload["successful_checks"]
    assert "waggle-mcp doctor" not in captured.out


def test_doctor_json_output_ok_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    db_path = tmp_path / "server-memory.db"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    write_waggle_codex_config(home, db_path)

    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="deterministic",
        db_path=str(db_path),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    exit_code = _run_doctor(config, json_output=True)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert "\x1b[" not in captured.out
    assert payload["schema_version"] == 1
    assert payload["status"] == "ok"
    assert payload["issues"] == []
    assert payload["warnings"] == []
    assert payload["fix_requested"] is False
    assert any(item.startswith("Waggle found in:") for item in payload["successful_checks"])


def test_doctor_json_output_warning_status_for_uncached_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    db_path = tmp_path / "server-memory.db"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    write_waggle_codex_config(home, db_path)

    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="sentence-transformers/not-cached-for-waggle-test",
        db_path=str(db_path),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    exit_code = _run_doctor(config, json_output=True)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["status"] == "warnings"
    assert payload["issues"] == []
    assert any("not found in cache" in warning for warning in payload["warnings"])


def test_create_and_list_api_keys_cli_redacts_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)

    create_args = SimpleNamespace(
        command="create-api-key",
        tenant_id="workspace-a",
        name="prod-agent",
        expires_in_days=30,
        created_by="ops@example.com",
    )
    exit_code = _run_admin_command(app.config, create_args)
    create_payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert create_payload["prefix"].startswith("sk_test_")
    assert create_payload["created_by"] == "ops@example.com"
    assert create_payload["scopes"] == ["graph:read", "graph:write", "admin:read", "admin:write"]
    assert "raw_api_key" in create_payload
    assert "key_hash" not in create_payload

    list_args = SimpleNamespace(command="list-api-keys", tenant_id="workspace-a")
    exit_code = _run_admin_command(app.config, list_args)
    listed = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert listed[0]["prefix"] == create_payload["prefix"]
    assert listed[0]["created_by"] == "ops@example.com"
    assert listed[0]["expires_at"] is not None
    assert listed[0]["scopes"] == ["graph:read", "graph:write", "admin:read", "admin:write"]
    assert "key_hash" not in listed[0]


def test_create_api_key_cli_uses_configured_live_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)
    app.config.api_key_environment = "live"

    create_args = SimpleNamespace(
        command="create-api-key",
        tenant_id="workspace-a",
        name="prod-agent",
        expires_in_days=30,
        created_by="ops@example.com",
    )
    exit_code = _run_admin_command(app.config, create_args)
    create_payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert create_payload["prefix"].startswith("sk_live_")
    assert create_payload["raw_api_key"].startswith(create_payload["prefix"])


def test_retention_admin_commands_update_and_prune(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)
    tenant_graph = app.graph.for_tenant("workspace-a")
    old_time = "2026-01-01T00:00:00+00:00"
    tenant_graph.add_node(label="Old fact", content="prune me", node_type=NodeType.FACT)
    with tenant_graph._lock, tenant_graph._connect() as connection:
        connection.execute("UPDATE nodes SET created_at = ?, updated_at = ?", (old_time, old_time))

    set_args = SimpleNamespace(
        command="set-retention",
        tenant_id="workspace-a",
        enabled=True,
        days=30,
        interval_hours=24,
    )
    exit_code = _run_admin_command(app.config, set_args)
    policy = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert policy["enabled"] is True
    assert policy["retention_days"] == 30

    prune_args = SimpleNamespace(
        command="prune-retention",
        tenant_id="workspace-a",
        batch_size=1000,
    )
    exit_code = _run_admin_command(app.config, prune_args)
    prune_payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert prune_payload["status"] == "completed"
    assert prune_payload["deleted_nodes"] == 1
    assert prune_payload["policy"]["last_pruned_at"] is not None

    status_args = SimpleNamespace(command="retention-status", tenant_id="workspace-a")
    exit_code = _run_admin_command(app.config, status_args)
    status_payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert status_payload["enabled"] is True
    assert status_payload["recent_runs"][0]["status"] == "completed"


def test_audit_events_are_queryable_from_admin_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)

    create_args = SimpleNamespace(
        command="create-api-key",
        tenant_id="workspace-a",
        name="prod-agent",
        expires_in_days=30,
        created_by="ops@example.com",
    )
    exit_code = _run_admin_command(app.config, create_args)
    assert exit_code == 0
    create_payload = json.loads(capsys.readouterr().out)

    audit_args = SimpleNamespace(
        command="list-audit-events",
        tenant_id="workspace-a",
        limit=20,
        event_type="api_key.created",
        actor_id="",
        resource_id=create_payload["api_key_id"],
        resource_type="api_key",
        status="",
    )
    exit_code = _run_admin_command(app.config, audit_args)
    events = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert events[0]["event_type"] == "api_key.created"
    assert events[0]["resource_id"] == create_payload["api_key_id"]
    assert events[0]["metadata"]["prefix"] == create_payload["prefix"]


def test_run_graph_editor_command_opens_browser_and_starts_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(tmp_path / "server-memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )
    args = SimpleNamespace(host="127.0.0.1", port=8787, open=True)
    opened: dict[str, str] = {}
    served: dict[str, object] = {}

    class ImmediateTimer:
        def __init__(self, interval: float, fn):
            self.interval = interval
            self.fn = fn
            self.daemon = False

        def start(self) -> None:
            self.fn()

    monkeypatch.setattr("waggle.server.webbrowser.open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr("waggle.server.threading.Timer", ImmediateTimer)
    monkeypatch.setattr(
        "waggle.server.uvicorn.run",
        lambda app, host, port, log_level: served.update(
            {"app": app, "host": host, "port": port, "log_level": log_level}
        ),
    )

    exit_code = _run_graph_editor_command(config, args)

    assert exit_code == 0
    assert opened["url"] == "http://127.0.0.1:8787/graph?mode=edit"
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8787


def test_run_view_graph_command_opens_read_only_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="fake-model",
        db_path=str(tmp_path / "server-memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )
    args = SimpleNamespace(command="view-graph", host="127.0.0.1", port=8787, open=True)
    opened: dict[str, str] = {}

    class ImmediateTimer:
        def __init__(self, interval: float, fn):
            self.fn = fn
            self.daemon = False

        def start(self) -> None:
            self.fn()

    monkeypatch.setattr("waggle.server.webbrowser.open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr("waggle.server.threading.Timer", ImmediateTimer)
    monkeypatch.setattr("waggle.server.uvicorn.run", lambda *args, **kwargs: None)

    exit_code = _run_graph_editor_command(config, args)

    assert exit_code == 0
    assert opened["url"] == "http://127.0.0.1:8787/graph?mode=view"


def test_memory_policy_prompt_and_resource(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    prompts = app.build_prompts()
    assert [prompt.name for prompt in prompts] == ["waggle_memory_policy"]

    prompt_result = app.get_prompt_result(
        "waggle_memory_policy",
        {"project": "MCP", "agent_id": "codex", "session_id": "thread-1"},
    )
    prompt_text = prompt_result.messages[0].content.text

    assert "The user should not manually manage memory" in prompt_text
    assert "Waggle should remember relevant conversational context automatically" in prompt_text
    assert "Use query_graph before answering" in prompt_text
    assert "Use observe_conversation after completed turns" in prompt_text
    assert "project: MCP" in prompt_text

    resource_text = app.read_resource_text("graph://memory-policy")
    assert "Waggle automatic memory policy" in resource_text
    assert "If memory looks empty" in resource_text
    assert "Do not call store_node for normal conversation memory" in resource_text


def test_memory_tools_describe_automatic_usage(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    tools = {tool.name: tool for tool in app.build_tools()}

    assert "Automatically search the memory graph before answering" in tools["query_graph"].description
    assert "Do not ask the user to trigger this" in tools["observe_conversation"].description
    assert "Automatically build a compact context brief" in tools["prime_context"].description


def test_export_graph_html_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "Visual Node",
            "content": "The graph should be exportable as HTML.",
            "node_type": NodeType.CONCEPT.value,
        },
    )

    result = app.handle_tool_call(
        "export_graph_html",
        {
            "output_path": str(tmp_path / "visualization.html"),
            "include_physics": False,
        },
    )

    assert result.isError is False
    assert result.structuredContent["total_nodes"] == 1
    assert Path(result.structuredContent["output_path"]).exists()
    assert "Exported graph visualization" in result.content[0].text


def test_window_graph_viz_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "Window Viz Node",
            "content": "The context-window graph should be exportable as HTML.",
            "node_type": NodeType.CONCEPT.value,
            "project": "alpha",
            "session_id": "sess-1",
        },
    )

    result = app.handle_tool_call(
        "window_graph_viz",
        {
            "project": "alpha",
            "output_path": str(tmp_path / "window-viz.html"),
            "include_physics": False,
        },
    )

    assert result.isError is False
    assert result.structuredContent["total_context_windows"] == 1
    assert Path(result.structuredContent["output_path"]).exists()
    assert "Exported context-window graph visualization" in result.content[0].text


def test_decompose_and_store_tool_persists_subgraph(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    result = app.handle_tool_call(
        "decompose_and_store",
        {
            "content": "- User prefers Python\n- Project uses FastAPI",
            "context": "Backend memory",
        },
    )

    assert result.isError is False
    assert result.structuredContent["total_nodes_in_graph"] >= 3
    assert len(result.structuredContent["edges"]) >= 2
    assert "Memory Graph Results" in result.content[0].text


def test_export_and_import_backup_tools(tmp_path: Path) -> None:
    source = make_app(tmp_path / "source")
    target = make_app(tmp_path / "target")
    source.handle_tool_call(
        "store_node",
        {
            "label": "Backup Tool Node",
            "content": "This node should appear after import.",
            "node_type": NodeType.NOTE.value,
        },
    )

    backup = source.handle_tool_call(
        "export_graph_backup",
        {"output_path": str(tmp_path / "graph-backup.json")},
    )
    imported = target.handle_tool_call(
        "import_graph_backup",
        {"input_path": backup.structuredContent["output_path"]},
    )

    assert backup.isError is False
    assert Path(backup.structuredContent["output_path"]).exists()
    assert imported.isError is False
    assert imported.structuredContent["nodes_created"] == 1
    assert target.graph.get_stats().total_nodes == 1


def test_export_validate_inspect_and_import_abhi_tools(tmp_path: Path) -> None:
    source = make_app(tmp_path / "source")
    target = make_app(tmp_path / "target")
    source.handle_tool_call(
        "store_node",
        {
            "label": "ABHI Tool Node",
            "content": "This node should survive an ABHI round trip.",
            "node_type": NodeType.NOTE.value,
        },
    )

    exported = source.handle_tool_call(
        "export_abhi",
        {"output_path": str(tmp_path / "memory.abhi")},
    )
    validated = source.handle_tool_call(
        "validate_abhi",
        {"input_path": exported.structuredContent["output_path"]},
    )
    inspected = source.handle_tool_call(
        "inspect_abhi",
        {"input_path": exported.structuredContent["output_path"]},
    )
    imported = target.handle_tool_call(
        "import_abhi",
        {"input_path": exported.structuredContent["output_path"]},
    )

    assert exported.isError is False
    assert exported.structuredContent["content_hash"].startswith("sha256:")
    assert validated.isError is False
    assert validated.structuredContent["valid"] is True
    assert inspected.isError is False
    assert inspected.structuredContent["node_count"] == 1
    assert imported.isError is False
    assert imported.structuredContent["hash_verified"] is True
    assert imported.structuredContent["nodes_created"] == 1


def test_export_abhi_tool_refuses_likely_secrets_without_force(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.graph.observe_conversation(
        user_message="My token is sk-abcdefghijklmnopqrstuvwxyz123456.",
        assistant_response="I will not repeat the token.",
        session_id="secret-session",
        project="security",
    )

    refused = app.handle_tool_call(
        "export_abhi",
        {"output_path": str(tmp_path / "secret.abhi"), "project": "security"},
    )
    forced = app.handle_tool_call(
        "export_abhi",
        {"output_path": str(tmp_path / "secret-forced.abhi"), "project": "security", "force": True},
    )

    assert refused.isError is True
    assert "appear to contain secrets" in refused.content[0].text
    assert forced.isError is False
    assert Path(forced.structuredContent["output_path"]).exists()


def test_diff_and_merge_abhi_tools(tmp_path: Path) -> None:
    base_app = make_app(tmp_path / "base")
    left_app = make_app(tmp_path / "left")
    right_app = make_app(tmp_path / "right")

    for app in (base_app, left_app, right_app):
        app.handle_tool_call(
            "store_node",
            {
                "label": "Decision",
                "content": "Use PostgreSQL",
                "node_type": NodeType.DECISION.value,
            },
        )

    left_app.handle_tool_call(
        "store_node",
        {
            "label": "Reason",
            "content": "Operational familiarity matters.",
            "node_type": NodeType.NOTE.value,
        },
    )
    right_app.graph.update_node(
        node_id=right_app.graph.list_recent_nodes(limit=1)[0].id,
        content="Use PostgreSQL with managed backups",
    )

    base_file = base_app.handle_tool_call("export_abhi", {"output_path": str(tmp_path / "base.abhi")})
    left_file = left_app.handle_tool_call("export_abhi", {"output_path": str(tmp_path / "left.abhi")})
    right_file = right_app.handle_tool_call("export_abhi", {"output_path": str(tmp_path / "right.abhi")})

    diff_result = base_app.handle_tool_call(
        "diff_abhi",
        {
            "input_path_a": left_file.structuredContent["output_path"],
            "input_path_b": right_file.structuredContent["output_path"],
        },
    )
    merge_result = base_app.handle_tool_call(
        "merge_abhi",
        {
            "base_input_path": base_file.structuredContent["output_path"],
            "left_input_path": left_file.structuredContent["output_path"],
            "right_input_path": right_file.structuredContent["output_path"],
            "output_path": str(tmp_path / "merged.abhi"),
        },
    )

    assert diff_result.isError is False
    assert diff_result.structuredContent["nodes_added"] or diff_result.structuredContent["nodes_removed"]
    assert merge_result.isError is False
    assert Path(merge_result.structuredContent["output_path"]).exists()
    assert merge_result.structuredContent["nodes_merged"] >= 1


def test_query_abhi_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "Decision",
            "content": "Use PostgreSQL",
            "node_type": NodeType.DECISION.value,
        },
    )
    exported = app.handle_tool_call("export_abhi", {"output_path": str(tmp_path / "memory.abhi")})
    queried = app.handle_tool_call(
        "query_abhi",
        {
            "input_path": exported.structuredContent["output_path"],
            "query_id": "q1",
        },
    )

    assert queried.isError is False
    assert queried.structuredContent["query_id"] == "q1"
    assert "queried_abhi" in queried.structuredContent["executed_actions"]


def test_load_abhi_chunks_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    for index in range(70):
        app.handle_tool_call(
            "store_node",
            {
                "label": f"Decision {index}",
                "content": f"Use PostgreSQL for service {index}",
                "node_type": NodeType.DECISION.value,
            },
        )
    exported = app.handle_tool_call("export_abhi", {"output_path": str(tmp_path / "memory.abhi")})
    loaded = app.handle_tool_call(
        "load_abhi_chunks",
        {
            "input_path": exported.structuredContent["output_path"],
            "query_text": "FIND nodes WHERE type='decision'",
        },
    )

    assert loaded.isError is False
    assert loaded.structuredContent["chunk_ids"]
    assert loaded.structuredContent["available_chunk_count"] >= 2


def test_export_context_bundle_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    decision = app.graph.add_node(
        label="Ship portable export",
        content="We decided to ship portable context export first.",
        node_type=NodeType.DECISION,
    ).node
    reason = app.graph.add_node(
        label="Rate limits block handoff",
        content="Rate limits force users to move context across AIs.",
        node_type=NodeType.FACT,
    ).node
    app.graph.add_edge(
        source_id=decision.id,
        target_id=reason.id,
        relationship=RelationType.DEPENDS_ON,
    )

    result = app.handle_tool_call(
        "export_context_bundle",
        {
            "mode": "query",
            "query": "why did we ship portable export",
            "format": "both",
            "output_path": str(tmp_path / "handoff"),
        },
    )

    assert result.isError is False
    assert result.structuredContent["mode"] == "query"
    assert result.structuredContent["node_count"] >= 1
    assert Path(result.structuredContent["markdown_path"]).exists()
    assert Path(result.structuredContent["json_path"]).exists()
    assert result.structuredContent["render_hints"]["token_estimate"] > 0
    assert "Context Bundle Export" in result.content[0].text


def test_commit_tool_git_vocabulary(tmp_path: Path) -> None:
    """waggle commit (new name) produces the same .abhi output as export_abhi (old name)."""
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Commit test decision",
        content="We decided to use the git vocabulary for the CLI.",
        node_type=NodeType.DECISION,
    )

    # New canonical name — should produce an .abhi file
    result = app.handle_tool_call("commit", {"output_path": str(tmp_path / "via_commit.abhi")})
    assert result.isError is False
    assert result.structuredContent["commit_format"] == "abhi"
    assert Path(result.structuredContent["output_path"]).exists()
    assert result.structuredContent["node_count"] >= 1

    # Legacy name — must still work and produce the same format
    result_legacy = app.handle_tool_call("export_abhi", {"output_path": str(tmp_path / "via_export_abhi.abhi")})
    assert result_legacy.isError is False
    assert result_legacy.structuredContent["commit_format"] == "abhi"
    assert Path(result_legacy.structuredContent["output_path"]).exists()


def test_export_context_bundle_alias_routes_to_bundle(tmp_path: Path) -> None:
    """export_context_bundle (legacy) → commit --commit_format=bundle; mode field present."""
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Bundle alias test",
        content="Portable context export ships first.",
        node_type=NodeType.DECISION,
    )

    # Legacy name with no commit_format — must default to bundle path
    result = app.handle_tool_call(
        "export_context_bundle",
        {"mode": "query", "query": "portable export", "format": "both", "output_path": str(tmp_path / "bundle")},
    )
    assert result.isError is False
    assert result.structuredContent["mode"] == "query"
    assert result.structuredContent["node_count"] >= 1
    assert Path(result.structuredContent["markdown_path"]).exists()


def test_export_context_bundle_caller_override_wins(tmp_path: Path) -> None:
    """Caller passing commit_format='abhi' via the legacy name overrides the default bundle format."""
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Override test",
        content="Caller-provided args must win over alias defaults.",
        node_type=NodeType.DECISION,
    )

    # Caller explicitly requests abhi format even though the legacy name defaults to bundle
    result = app.handle_tool_call(
        "export_context_bundle",
        {"commit_format": "abhi", "output_path": str(tmp_path / "override.abhi")},
    )
    assert result.isError is False
    assert result.structuredContent["commit_format"] == "abhi"
    assert Path(result.structuredContent["output_path"]).exists()


def test_git_vocabulary_pull_aliases(tmp_path: Path) -> None:
    """import_abhi and import_graph_backup both route to pull with the right pull_format."""
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Pull alias test",
        content="Both import aliases must route correctly.",
        node_type=NodeType.DECISION,
    )

    # Commit an .abhi file to pull back in
    commit_result = app.handle_tool_call("commit", {"output_path": str(tmp_path / "mem.abhi")})
    assert commit_result.isError is False
    abhi_path = commit_result.structuredContent["output_path"]

    # import_abhi → pull --pull_format=abhi
    pull_result = app.handle_tool_call("import_abhi", {"input_path": abhi_path})
    assert pull_result.isError is False
    assert pull_result.structuredContent["pull_format"] == "abhi"

    app = make_app(tmp_path)
    observed = app.handle_tool_call(
        "observe_conversation",
        {
            "user_message": "We chose PostgreSQL over MySQL because ACID matters.",
            "assistant_response": "I'll remember that decision.",
        },
    )
    decision = next(node for node in observed.structuredContent["stored_nodes"] if node["label"] == "Database decision")

    result = app.handle_tool_call("get_node_history", {"node_id": decision["id"], "max_depth": 1})

    assert result.isError is False
    assert result.structuredContent["node"]["evidence_records"]
    assert "Node History" in result.content[0].text


def test_timeline_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    decision = app.graph.add_node(
        label="Use PostgreSQL",
        content="We chose PostgreSQL.",
        node_type=NodeType.DECISION,
    ).node
    app.graph.observe_conversation(
        user_message="We chose PostgreSQL.",
        assistant_response="I'll remember that decision.",
    )

    result = app.handle_tool_call(
        "timeline",
        {"node_id": decision.id, "limit": 10, "include_evidence": True},
    )

    assert result.isError is False
    assert result.structuredContent["scope"] == f"node:{decision.id}"
    assert any(item["kind"] == "evidence" for item in result.structuredContent["items"])
    assert "Timeline" in result.content[0].text


def test_list_conflicts_and_resolve_conflict_tools(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "REST Preference",
            "content": "User prefers REST APIs for backend services",
            "node_type": NodeType.PREFERENCE.value,
        },
    )
    app.handle_tool_call(
        "store_node",
        {
            "label": "GraphQL Preference",
            "content": "User prefers GraphQL APIs for backend services",
            "node_type": NodeType.PREFERENCE.value,
        },
    )

    listed = app.handle_tool_call("list_conflicts", {})
    edge_id = listed.structuredContent["conflicts"][0]["edge"]["id"]
    resolved = app.handle_tool_call(
        "resolve_conflict",
        {"edge_id": edge_id, "resolution_note": "Superseded by the newer API decision."},
    )
    unresolved_after = app.handle_tool_call("list_conflicts", {})
    resolved_after = app.handle_tool_call("list_conflicts", {"include_resolved": True})

    assert listed.isError is False
    assert len(listed.structuredContent["conflicts"]) == 1
    assert resolved.isError is False
    assert resolved.structuredContent["resolved"] is True
    assert unresolved_after.structuredContent["conflicts"] == []
    assert resolved_after.structuredContent["conflicts"][0]["resolved"] is True


def test_list_context_scopes_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "Scoped node",
            "content": "This belongs to the alpha workspace.",
            "node_type": NodeType.NOTE.value,
            "agent_id": "codex",
            "project": "alpha",
            "session_id": "sess-1",
        },
    )

    result = app.handle_tool_call("list_context_scopes", {})

    assert result.isError is False
    assert result.structuredContent["agent_ids"] == ["codex"]
    assert result.structuredContent["projects"] == ["alpha"]
    assert result.structuredContent["session_ids"] == ["sess-1"]


def test_context_window_tools(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    stored = app.handle_tool_call(
        "store_node",
        {
            "label": "Window Tool Node",
            "content": "Context window tools should expose session containers.",
            "node_type": NodeType.FACT.value,
            "project": "alpha",
            "session_id": "sess-1",
        },
    )
    window_id = stored.structuredContent["context_window_id"]

    listed = app.handle_tool_call("list_context_windows", {"project": "alpha"})
    fetched = app.handle_tool_call("get_context_window", {"window_id": window_id})
    closed = app.handle_tool_call("close_context_window", {"window_id": window_id})

    assert listed.isError is False
    assert listed.structuredContent["windows"][0]["id"] == window_id
    assert fetched.isError is False
    assert fetched.structuredContent["window"]["id"] == window_id
    assert fetched.structuredContent["nodes"][0]["label"] == "Window Tool Node"
    assert closed.isError is False
    assert closed.structuredContent["window"]["status"] == "closed"


def test_debug_retrieval_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "Debug Retrieval Node",
            "content": "Debug retrieval should expose flat and tiered comparison details.",
            "node_type": NodeType.FACT.value,
            "project": "alpha",
            "session_id": "sess-1",
        },
    )

    result = app.handle_tool_call("debug_retrieval", {"query": "debug retrieval details", "project": "alpha"})

    assert result.isError is False
    assert result.structuredContent["retrieval_mode"] == "hybrid"
    assert result.structuredContent["layers"]["vector_transcript"] == []
    assert result.structuredContent["layers"]["vector_node"]
    assert result.structuredContent["layers"]["lexical"]
    assert result.structuredContent["hybrid_top_hits"]


def test_export_context_bundle_cli_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)
    app.graph.add_node(
        label="CLI Export Decision",
        content="Use the CLI export command for handoff workflows.",
        node_type=NodeType.DECISION,
    )

    args = SimpleNamespace(
        command="export-context-bundle",
        mode="graph",
        query="",
        project="",
        max_nodes=25,
        max_depth=2,
        format="both",
        output_path=str(tmp_path / "cli-handoff"),
        include_edges=True,
        include_timestamps=True,
        include_source_prompt=False,
        audience="llm",
    )
    exit_code = _run_admin_command(app.config, args)
    captured = capsys.readouterr().out
    payload = json.loads(captured)

    assert exit_code == 0
    assert payload["mode"] == "graph"
    assert Path(payload["markdown_path"]).exists()
    assert Path(payload["json_path"]).exists()


def test_checkpoint_context_cli_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Checkpoint Decision",
        content="Use scoped checkpoints during context switches.",
        node_type=NodeType.DECISION,
        project="MCP",
        session_id="thread-1",
    )

    args = SimpleNamespace(
        command="checkpoint-context",
        output_path=str(tmp_path / "handoff.abhi"),
        project="MCP",
        agent_id="",
        session_id="thread-1",
        scope="",
        since_date="",
        include_embeddings=True,
        encrypt=False,
        sign=False,
        signing_key_dir="~/.waggle/keys",
        redact_patterns=[],
        passphrase_env="",
        force=False,
    )
    exit_code = _run_admin_command(app.config, args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["checkpoint_scope"] == "session"


def test_clear_session_project_and_all_tools_require_confirm_and_delete_data(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.graph.observe_conversation(
        user_message="Use Redis for caching.",
        assistant_response="Noted.",
        project="alpha",
        session_id="sess-1",
    )
    app.graph.observe_conversation(
        user_message="Use Kafka for ingestion.",
        assistant_response="Noted.",
        project="beta",
        session_id="sess-2",
    )

    denied = app.handle_tool_call("clear_session", {"session_id": "sess-1"})
    assert denied.isError is True

    cleared_session = app.handle_tool_call("clear_session", {"session_id": "sess-1", "confirm": True})
    assert cleared_session.structuredContent["scope"] == "session"
    assert app.graph.query(query="redis", project="alpha", session_id="sess-1", max_nodes=5).nodes == []
    assert app.graph.query(query="kafka", project="beta", session_id="sess-2", max_nodes=5).nodes

    cleared_project = app.handle_tool_call("clear_project", {"project": "beta", "confirm": True})
    assert cleared_project.structuredContent["scope"] == "project"
    assert app.graph.query(query="kafka", project="beta", max_nodes=5).nodes == []

    app.graph.observe_conversation(
        user_message="Use Postgres for storage.",
        assistant_response="Noted.",
        project="gamma",
        session_id="sess-3",
    )
    cleared_all = app.handle_tool_call("clear_all", {"confirm": True})
    assert cleared_all.structuredContent["scope"] == "all"
    assert app.graph.get_stats().total_nodes == 0


def test_markdown_vault_tool_and_cli_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Vault Decision",
        content="Export this node to a markdown vault.",
        node_type=NodeType.DECISION,
        project="alpha",
    )

    tool_result = app.handle_tool_call(
        "export_markdown_vault",
        {"root_path": str(tmp_path / "vault"), "project": "alpha"},
    )
    assert tool_result.isError is False
    assert tool_result.structuredContent["files_written"]

    args = SimpleNamespace(
        command="export-markdown-vault", root_path=str(tmp_path / "vault-cli"), project="", agent_id="", session_id=""
    )
    exit_code = _run_admin_command(app.config, args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["files_written"]


def test_runtime_feature_parity_check_passes_for_current_memory_graph() -> None:
    _assert_runtime_feature_parity()


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert f"waggle-mcp {waggle.__version__}" in captured.out


def test_store_node_reports_deduplication(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    first = app.handle_tool_call(
        "store_node",
        {
            "label": "Session Preference",
            "content": "This session prefers persistent graph memory.",
            "node_type": NodeType.PREFERENCE.value,
        },
    )
    second = app.handle_tool_call(
        "store_node",
        {
            "label": "Session Preference",
            "content": "This session prefers persistent graph memory.",
            "node_type": NodeType.PREFERENCE.value,
            "tags": ["deduped"],
        },
    )

    assert first.structuredContent["created"] is True
    assert second.structuredContent["created"] is False
    assert second.structuredContent["dedup_reason"] == "exact_content"
    assert "Reused existing node" in second.content[0].text


def test_store_node_reports_conflicts(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "REST Preference",
            "content": "User prefers REST APIs for backend work",
            "node_type": NodeType.PREFERENCE.value,
        },
    )

    result = app.handle_tool_call(
        "store_node",
        {
            "label": "GraphQL Preference",
            "content": "User prefers GraphQL APIs for backend work",
            "node_type": NodeType.PREFERENCE.value,
        },
    )

    assert result.isError is False
    assert result.structuredContent["conflicts"]


def test_observe_conversation_tool(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    result = app.handle_tool_call(
        "observe_conversation",
        {
            "user_message": "I prefer Python for backend work.",
            "assistant_response": "Let's use FastAPI and update src/server.py.",
        },
    )

    assert result.isError is False
    assert result.structuredContent["created_count"] >= 2
    assert any(node["evidence_records"] for node in result.structuredContent["stored_nodes"])
    assert "Conversation Observation" in result.content[0].text


def test_observe_conversation_tool_reports_database_conflicts(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "observe_conversation",
        {
            "user_message": "We chose PostgreSQL over MySQL because MySQL replication has been painful.",
            "assistant_response": "Understood.",
        },
    )

    result = app.handle_tool_call(
        "observe_conversation",
        {
            "user_message": "The team is more familiar with MySQL, so we may switch to MySQL.",
            "assistant_response": "Understood.",
        },
    )

    assert result.isError is False
    assert result.structuredContent["conflicts"]


def test_graph_diff_prime_context_and_topics_tools(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.handle_tool_call(
        "store_node",
        {
            "label": "Alpha Project",
            "content": "Project Alpha uses FastAPI",
            "node_type": NodeType.ENTITY.value,
            "tags": ["alpha"],
        },
    )
    diff = app.handle_tool_call("graph_diff", {"since": "24h"})
    prime = app.handle_tool_call("prime_context", {"project": "alpha"})
    topics = app.handle_tool_call("get_topics", {})

    assert diff.isError is False
    assert diff.structuredContent["added_nodes"]
    assert prime.isError is False
    assert prime.structuredContent["nodes"]
    assert topics.isError is False
    assert topics.structuredContent["total_clusters"] >= 1


def test_recent_resource_serialization(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Architecture",
        content="The project uses SQLite and NetworkX",
        node_type=NodeType.CONCEPT,
    )

    resource_text = app.read_resource_text("graph://recent")
    assert "Recent Memory Nodes" in resource_text
    assert "Architecture" in resource_text


def test_context_windows_resource_serialization(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.graph.add_node(
        label="Architecture",
        content="The project uses SQLite and NetworkX",
        node_type=NodeType.CONCEPT,
        project="alpha",
        session_id="sess-1",
    )

    resource_text = app.read_resource_text("graph://windows")

    assert "Context Windows" in resource_text
    assert "sess-1" in resource_text


def test_unknown_tool_raises(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    result = app.handle_tool_call("does_not_exist", {})
    assert result.isError is True
    assert result.structuredContent["error_type"] == "ValidationFailure"


def test_invalid_tool_inputs_return_structured_errors(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    empty_query = app.handle_tool_call("query_graph", {"query": ""})
    assert empty_query.isError is True
    assert "Query cannot be empty" in empty_query.content[0].text

    missing_node_edge = app.handle_tool_call(
        "store_edge",
        {
            "source_id": "missing-a",
            "target_id": "missing-b",
            "relationship": "relates_to",
        },
    )
    assert missing_node_edge.isError is True
    assert missing_node_edge.structuredContent["error_type"] == "ValueError"


def test_tool_payload_limit_is_enforced(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.config.max_payload_bytes = 8

    result = app.handle_tool_call(
        "store_node",
        {
            "label": "Too Large",
            "content": "this payload is definitely larger than eight bytes",
            "node_type": NodeType.NOTE.value,
        },
    )

    assert result.isError is True
    assert result.structuredContent["error_code"] == "payload_too_large"


def test_default_graph_uses_sqlite_backend_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAGGLE_BACKEND", raising=False)
    monkeypatch.setenv("WAGGLE_DB_PATH", str(tmp_path / "sqlite-memory.db"))

    graph = _default_graph()

    assert isinstance(graph, MemoryGraph)
    assert graph.db_path == tmp_path / "sqlite-memory.db"


def test_default_graph_uses_home_scoped_sqlite_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WAGGLE_BACKEND", raising=False)
    monkeypatch.delenv("WAGGLE_DB_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    expected_db = tmp_path / ".waggle" / "waggle.db"
    graph = _default_graph()

    assert isinstance(graph, MemoryGraph)
    assert graph.db_path == expected_db


def test_default_graph_can_build_neo4j_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeNeo4jGraph:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import waggle.neo4j_graph as neo4j_graph_module

    monkeypatch.setattr(neo4j_graph_module, "Neo4jMemoryGraph", FakeNeo4jGraph)
    monkeypatch.setenv("WAGGLE_BACKEND", "neo4j")
    monkeypatch.setenv("WAGGLE_MODEL", "fake-model")
    monkeypatch.setenv("WAGGLE_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("WAGGLE_NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("WAGGLE_NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("WAGGLE_NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("WAGGLE_NEO4J_DATABASE", "memory")

    graph = _default_graph()

    assert isinstance(graph, FakeNeo4jGraph)
    assert captured["uri"] == "bolt://localhost:7687"
    assert captured["username"] == "neo4j"
    assert captured["password"] == "secret"
    assert captured["database"] == "memory"
    assert captured["export_dir"] == str(tmp_path / "exports")


def test_default_graph_prefers_codex_waggle_db_path_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WAGGLE_BACKEND", raising=False)
    monkeypatch.delenv("WAGGLE_DB_PATH", raising=False)

    configured_db = tmp_path / ".waggle" / "memory.db"
    write_waggle_codex_config(tmp_path, configured_db)

    # Directly set WAGGLE_DB_PATH to the configured value
    monkeypatch.setenv("WAGGLE_DB_PATH", str(configured_db))

    graph = _default_graph()

    assert isinstance(graph, MemoryGraph)
    assert graph.db_path == configured_db


def test_default_graph_requires_neo4j_connection_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAGGLE_BACKEND", "neo4j")
    monkeypatch.delenv("WAGGLE_NEO4J_URI", raising=False)
    monkeypatch.delenv("WAGGLE_NEO4J_USERNAME", raising=False)
    monkeypatch.delenv("WAGGLE_NEO4J_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="Neo4j backend requires"):
        _default_graph()


def test_write_other_config_no_longer_uses_pythonpath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    config_path = _write_other(str(tmp_path / "memory.db"), "/tmp/fake-python")
    contents = config_path.read_text()
    payload = json.loads(contents)

    assert "PYTHONPATH" not in contents
    assert payload["command"] == "waggle-mcp"
    assert payload["args"] == ["serve", "--transport", "stdio"]


def test_write_codex_config_no_longer_uses_pythonpath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    config_path = _write_codex(str(tmp_path / "memory.db"), "/tmp/fake-python")
    contents = config_path.read_text()

    assert config_path == tmp_path / ".codex" / "config.toml"
    assert "PYTHONPATH" not in contents
    assert 'command = "waggle-mcp"' in contents
    assert 'args = ["serve", "--transport", "stdio"]' in contents
    assert "[mcp_servers.waggle.env]" in contents


def test_parser_exposes_non_interactive_setup_command() -> None:
    parser = _build_parser()
    args = parser.parse_args(["setup", "--yes", "--clients", "codex,cursor", "--model", "deterministic"])

    assert args.command == "setup"
    assert args.yes is True
    assert args.clients == "codex,cursor"
    assert args.model == "deterministic"


def test_setup_client_arg_normalization() -> None:
    assert _setup_clients_from_args("codex,gemini,antigravity") == ["Codex", "Gemini CLI", "Antigravity"]


def test_write_gemini_config_preserves_existing_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    settings_file = tmp_path / ".gemini" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))

    config_path = _write_gemini(str(tmp_path / "memory.db"), "/tmp/fake-python")
    payload = json.loads(config_path.read_text())

    assert payload["theme"] == "dark"
    assert "other" in payload["mcpServers"]
    assert payload["mcpServers"]["waggle"]["command"] == "waggle-mcp"
    assert payload["mcpServers"]["waggle"]["args"] == ["serve", "--transport", "stdio"]
    assert payload["mcpServers"]["waggle"]["trust"] is False


def test_write_antigravity_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    config_path = _write_antigravity(str(tmp_path / "memory.db"), "/tmp/fake-python")
    payload = json.loads(config_path.read_text())

    assert config_path == tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    assert payload["mcpServers"]["waggle"]["command"] == "waggle-mcp"
    assert payload["mcpServers"]["waggle"]["args"] == ["serve", "--transport", "stdio"]


def test_run_setup_writes_codex_config_and_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _run_setup(
        SimpleNamespace(
            yes=True,
            dry_run=False,
            clients="codex",
            db=str(tmp_path / "memory.db"),
            model="deterministic",
            project_instructions=True,
            run_doctor=False,
        )
    )

    assert result == 0
    config_text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.waggle]" in config_text
    assert 'WAGGLE_MODEL = "deterministic"' in config_text
    assert AUTOMATIC_MEMORY_RULE_TEXT.strip() in (tmp_path / "AGENTS.md").read_text()


def test_write_codex_config_updates_existing_file_without_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    config_file = tmp_path / ".codex" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        "[profile.default]\n"
        'model = "gpt-5.4"\n\n'
        "[mcp_servers.waggle]\n"
        'command = "/old/python"\n'
        'args = ["-m", "waggle.server"]\n\n'
        "[mcp_servers.waggle.env]\n"
        'WAGGLE_DB_PATH = "/old/memory.db"\n\n'
        "[mcp_servers.playwright]\n"
        'command = "npx"\n'
    )

    config_path = _write_codex(str(tmp_path / "memory.db"), "/tmp/fake-python")
    contents = config_path.read_text()

    assert contents.count("[mcp_servers.waggle]") == 1
    assert contents.count("[mcp_servers.waggle.env]") == 1
    assert '[profile.default]\nmodel = "gpt-5.4"' in contents
    assert '[mcp_servers.playwright]\ncommand = "npx"' in contents
    assert 'command = "waggle-mcp"' in contents
    assert 'args = ["serve", "--transport", "stdio"]' in contents
    assert f'WAGGLE_DB_PATH = "{tmp_path / "memory.db"}"' in contents
    assert "/old/python" not in contents
    assert "/old/memory.db" not in contents


def test_write_codex_agents_creates_managed_block(tmp_path: Path) -> None:
    agents_path = _write_codex_agents(tmp_path)
    contents = agents_path.read_text()

    assert agents_path == tmp_path / "AGENTS.md"
    assert "## Waggle Automatic Memory" in contents
    assert AUTOMATIC_MEMORY_RULE_TEXT.strip() in contents
    assert "<!-- waggle:auto-memory:start -->" in contents
    assert "<!-- waggle:auto-memory:end -->" in contents


def test_write_codex_agents_updates_existing_block_without_duplication(tmp_path: Path) -> None:
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text(
        "# Project Instructions\n\n"
        "<!-- waggle:auto-memory:start -->\n"
        "old instructions\n"
        "<!-- waggle:auto-memory:end -->\n\n"
        "Keep this note.\n"
    )

    _write_codex_agents(tmp_path)
    contents = agents_file.read_text()

    assert contents.count("<!-- waggle:auto-memory:start -->") == 1
    assert contents.count("<!-- waggle:auto-memory:end -->") == 1
    assert "old instructions" not in contents
    assert "Keep this note." in contents
    assert AUTOMATIC_MEMORY_RULE_TEXT.strip() in contents
    assert "build_context before answers and on_assistant_turn after answers" in contents
