from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from waggle.abhi import load_abhi_document, validate_abhi_document
from waggle.config import AppConfig
from waggle.embeddings import EmbeddingModel
from waggle.errors import ValidationFailure
from waggle.github_event import ingest_github_event, validate_export_scope
from waggle.graph import MemoryGraph
from waggle.server import _build_parser, _run_ingest_github_event, main

FIXTURES = Path(__file__).parent / "fixtures" / "github_events"


def make_graph(tmp_path: Path, name: str = "memory.db") -> MemoryGraph:
    return MemoryGraph(
        tmp_path / name,
        EmbeddingModel("deterministic"),
        enable_dedup=False,
    )


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="all-MiniLM-L6-v2",
        db_path=str(tmp_path / "cli-memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="WARNING",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=str(tmp_path / "exports"),
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )


def make_args(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "command": "ingest-github-event",
        "event_path": str(FIXTURES / "issue.json"),
        "event_type": "issue",
        "repository": "octo/demo",
        "project": "octo/demo",
        "scope": "project",
        "session_id": "",
        "since_date": "",
        "output_context": str(tmp_path / "context.md"),
        "output_checkpoint": str(tmp_path / "memory.abhi"),
        "max_input_bytes": 1_048_576,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parser_exposes_ingest_github_event_contract() -> None:
    args = _build_parser().parse_args(
        [
            "ingest-github-event",
            "--event-path",
            "event.json",
            "--repository",
            "octo/demo",
            "--project",
            "octo/demo",
            "--scope",
            "project",
            "--output-context",
            "context.md",
            "--output-checkpoint",
            "memory.abhi",
        ]
    )

    assert args.event_path == "event.json"
    assert args.event_type == ""
    assert args.repository == "octo/demo"
    assert args.project == "octo/demo"
    assert args.scope == "project"
    assert args.output_context == "context.md"
    assert args.output_checkpoint == "memory.abhi"


@pytest.mark.parametrize(
    ("scope", "project", "session_id", "since_date", "message"),
    [
        ("project", "", "", "", "project scope requires --project"),
        ("session", "octo/demo", "", "", "session scope requires --session-id"),
        ("since-date", "octo/demo", "", "", "since-date scope requires --since-date"),
        ("unknown", "octo/demo", "", "", "scope must be one of"),
    ],
)
def test_validate_export_scope_rejects_invalid_combinations(
    scope: str, project: str, session_id: str, since_date: str, message: str
) -> None:
    with pytest.raises(ValidationFailure, match=message):
        validate_export_scope(scope, project=project, session_id=session_id, since_date=since_date)


@pytest.mark.parametrize(
    ("scope", "session_id", "since_date"),
    [
        ("all", "", ""),
        ("project", "", ""),
        ("session", "session-1", ""),
        ("since-date", "", "2025-01-01T00:00:00Z"),
    ],
)
def test_validate_export_scope_accepts_existing_modes(scope: str, session_id: str, since_date: str) -> None:
    assert validate_export_scope(scope, project="octo/demo", session_id=session_id, since_date=since_date) == scope


def test_ingest_github_event_writes_valid_checkpoint_and_context(tmp_path: Path) -> None:
    checkpoint = tmp_path / "memory.abhi"
    context = tmp_path / "context.md"

    result = ingest_github_event(
        make_graph(tmp_path),
        event_path=FIXTURES / "issue.json",
        event_type="issue",
        github_event_name="",
        repository="octo/demo",
        project="octo/demo",
        scope="project",
        session_id="",
        since_date="",
        output_context=context,
        output_checkpoint=checkpoint,
    )
    document = load_abhi_document(checkpoint)
    validation = validate_abhi_document(document, input_path=checkpoint)

    assert result.status == "ingested"
    assert result.nodes_added == 3
    assert result.edges_added == 2
    assert validation.valid is True
    assert document["manifest"]["scope"] == "project"
    assert document["manifest"]["project"] == "octo/demo"
    assert checkpoint.stat().st_size > 0
    assert context.read_text(encoding="utf-8").strip().startswith("# Waggle GitHub Context Handoff")
    assert "Handle adversarial workflow context" in context.read_text(encoding="utf-8")
    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in checkpoint.read_bytes().decode("latin-1")
    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in context.read_text(encoding="utf-8")


def test_deterministic_input_produces_byte_identical_outputs(tmp_path: Path) -> None:
    first_context = tmp_path / "first.md"
    first_checkpoint = tmp_path / "first.abhi"
    second_context = tmp_path / "second.md"
    second_checkpoint = tmp_path / "second.abhi"

    first = ingest_github_event(
        make_graph(tmp_path, "first.db"),
        event_path=FIXTURES / "issue.json",
        event_type="issue",
        github_event_name="",
        repository="octo/demo",
        project="octo/demo",
        scope="project",
        session_id="",
        since_date="",
        output_context=first_context,
        output_checkpoint=first_checkpoint,
    )
    second = ingest_github_event(
        make_graph(tmp_path, "second.db"),
        event_path=FIXTURES / "issue.json",
        event_type="issue",
        github_event_name="",
        repository="octo/demo",
        project="octo/demo",
        scope="project",
        session_id="",
        since_date="",
        output_context=second_context,
        output_checkpoint=second_checkpoint,
    )

    assert first.nodes_added == second.nodes_added == 3
    assert first.edges_added == second.edges_added == 2
    assert first_context.read_bytes() == second_context.read_bytes()
    assert first_checkpoint.read_bytes() == second_checkpoint.read_bytes()


def test_unsupported_event_returns_zero_additions_and_valid_outputs(tmp_path: Path) -> None:
    event_path = tmp_path / "fork.json"
    event_path.write_text('{"forkee":{"id":99}}', encoding="utf-8")
    checkpoint = tmp_path / "memory.abhi"
    context = tmp_path / "context.md"

    result = ingest_github_event(
        make_graph(tmp_path),
        event_path=event_path,
        event_type="",
        github_event_name="fork",
        repository="octo/demo",
        project="octo/demo",
        scope="project",
        session_id="",
        since_date="",
        output_context=context,
        output_checkpoint=checkpoint,
    )

    assert result.status == "unsupported"
    assert result.event_type == "fork"
    assert result.nodes_added == 0
    assert result.edges_added == 0
    assert validate_abhi_document(load_abhi_document(checkpoint), input_path=checkpoint).valid is True
    assert "unsupported" in context.read_text(encoding="utf-8").lower()
    assert "forkee" not in context.read_text(encoding="utf-8")


def test_malformed_event_does_not_replace_requested_outputs(tmp_path: Path) -> None:
    event_path = tmp_path / "broken.json"
    event_path.write_text("{broken", encoding="utf-8")
    checkpoint = tmp_path / "memory.abhi"
    context = tmp_path / "context.md"
    checkpoint.write_bytes(b"existing checkpoint")
    context.write_text("existing context", encoding="utf-8")

    with pytest.raises(ValidationFailure, match="valid JSON"):
        ingest_github_event(
            make_graph(tmp_path),
            event_path=event_path,
            event_type="issue",
            github_event_name="",
            repository="octo/demo",
            project="octo/demo",
            scope="project",
            session_id="",
            since_date="",
            output_context=context,
            output_checkpoint=checkpoint,
        )

    assert checkpoint.read_bytes() == b"existing checkpoint"
    assert context.read_text(encoding="utf-8") == "existing context"


def test_cli_runner_uses_deterministic_backend_and_emits_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _run_ingest_github_event(make_config(tmp_path), make_args(tmp_path))
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert result["status"] == "ingested"
    assert result["nodes_added"] == 3
    assert result["edges_added"] == 2


def test_cli_main_writes_only_result_json_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    config.transport = "http"
    config.log_level = "INFO"
    monkeypatch.setattr(AppConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "waggle-mcp",
            "ingest-github-event",
            "--event-path",
            str(FIXTURES / "issue.json"),
            "--repository",
            "octo/demo",
            "--project",
            "octo/demo",
            "--scope",
            "project",
            "--output-context",
            str(tmp_path / "main-context.md"),
            "--output-checkpoint",
            str(tmp_path / "main-memory.abhi"),
        ],
    )

    with pytest.raises(SystemExit, match="0"):
        main()

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "ingested"
    startup = json.loads(captured.err)
    assert startup["message"] == "waggle_startup"


def test_cli_runner_rejects_non_sqlite_backend(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path)
    config.backend = "neo4j"

    exit_code = _run_ingest_github_event(config, make_args(tmp_path))
    captured = capsys.readouterr()
    error = json.loads(captured.err)

    assert exit_code == 1
    assert captured.out == ""
    assert error["code"] == "validation_error"
    assert "SQLite" in error["message"]
