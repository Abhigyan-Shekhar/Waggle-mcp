from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from waggle import telemetry

DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"


@pytest.fixture(autouse=True)
def isolated_telemetry_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(telemetry, "CONFIG_PATH", tmp_path / "telemetry.json")
    monkeypatch.setattr(telemetry, "QUEUE_PATH", tmp_path / "telemetry-queue.jsonl")
    monkeypatch.delenv("WAGGLE_TELEMETRY", raising=False)


def test_telemetry_defaults_to_disabled() -> None:
    config = telemetry.load_config()

    assert config.enabled is False
    assert config.installation_id
    assert config.overridden_by_env is False


def test_enable_disable_preserves_random_installation_id() -> None:
    enabled = telemetry.enable()
    disabled = telemetry.disable()

    assert enabled.enabled is True
    assert disabled.enabled is False
    assert disabled.installation_id == enabled.installation_id


def test_env_override_does_not_rewrite_local_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = telemetry.disable()
    monkeypatch.setenv("WAGGLE_TELEMETRY", "1")

    loaded = telemetry.load_config()
    persisted = json.loads(telemetry.CONFIG_PATH.read_text(encoding="utf-8"))

    assert loaded.enabled is True
    assert loaded.overridden_by_env is True
    assert loaded.installation_id == saved.installation_id
    assert persisted["enabled"] is False


def test_capture_with_env_enabled_persists_new_installation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAGGLE_TELEMETRY", "1")
    monkeypatch.setattr(telemetry, "flush", lambda: 0)

    telemetry.capture("memory_retrieved", waggle_version="0.1.test", properties={"success": True})

    persisted = json.loads(telemetry.CONFIG_PATH.read_text(encoding="utf-8"))
    queued = [json.loads(line) for line in telemetry.QUEUE_PATH.read_text(encoding="utf-8").splitlines()]
    assert persisted["enabled"] is True
    assert persisted["installation_id"] == queued[0]["installation_id"]


def test_preview_payload_sanitizes_forbidden_and_unknown_properties() -> None:
    payload = telemetry.preview_payload(
        "memory_retrieved",
        waggle_version="0.1.test",
        properties={
            "client": "codex",
            "backend": "sqlite",
            "success": True,
            "query": "what database did we choose?",
            "file_path": "/Users/example/project/secret.py",
            "unexpected": "ignored",
        },
    )

    properties = payload["properties"]
    assert properties["waggle_version"] == "0.1.test"
    assert properties["client"] == "codex"
    assert properties["backend"] == "sqlite"
    assert properties["success"] is True
    assert "query" not in properties
    assert "file_path" not in properties
    assert "unexpected" not in properties


def test_capture_queues_allowed_event_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry.enable()
    monkeypatch.setattr(telemetry, "flush", lambda: 0)

    telemetry.capture(
        "memory_retrieved",
        waggle_version="0.1.test",
        properties={"client": "codex", "result_count_bucket": "1-5"},
    )

    queued = [json.loads(line) for line in telemetry.QUEUE_PATH.read_text(encoding="utf-8").splitlines()]
    assert len(queued) == 1
    assert queued[0]["event"] == "memory_retrieved"
    assert queued[0]["properties"]["client"] == "codex"


def test_capture_ignores_disallowed_event() -> None:
    telemetry.enable()

    telemetry.capture("conversation_observed", waggle_version="0.1.test")

    assert not telemetry.QUEUE_PATH.exists()


def test_read_queue_drops_events_older_than_retention() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    telemetry.QUEUE_PATH.write_text(
        "\n".join(
            [
                json.dumps({"event": "memory_retrieved", "timestamp": old.isoformat(), "properties": {}}),
                json.dumps({"event": "memory_retrieved", "timestamp": now.isoformat(), "properties": {}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    queued = telemetry._read_queue()

    assert len(queued) == 1
    assert queued[0]["timestamp"] == now.isoformat()


def test_capture_tool_event_maps_retrieval_without_content(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, *, waggle_version, properties=None: captured.append((event, properties or {})),
    )

    telemetry.capture_tool_event(
        "query_graph",
        structured={
            "query": "secret user query",
            "nodes": [{"content": "secret memory text"}],
            "replay_hits": [{"transcript_text": "secret transcript"}],
        },
        is_error=False,
        waggle_version="0.1.test",
        transport="stdio",
        backend="sqlite",
        embedding_mode="local",
    )

    assert captured == [
        (
            "memory_retrieved",
            {
                "success": True,
                "transport": "stdio",
                "backend": "sqlite",
                "embedding_mode": "local",
                "result_count_bucket": "1-5",
            },
        )
    ]


def test_capture_tool_event_maps_store_and_prime(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, *, waggle_version, properties=None: captured.append(event),
    )

    telemetry.capture_tool_event(
        "observe_conversation",
        structured={"created_count": 3, "stored_nodes": [{"content": "not sent"}]},
        is_error=False,
        waggle_version="0.1.test",
        transport="stdio",
        backend="sqlite",
        embedding_mode="local",
    )
    telemetry.capture_tool_event(
        "prime_context",
        structured={"nodes": [{"content": "not sent"}]},
        is_error=False,
        waggle_version="0.1.test",
        transport="stdio",
        backend="sqlite",
        embedding_mode="local",
    )

    assert captured == ["memory_stored", "context_primed"]


def test_capture_tool_event_maps_failures_to_safe_error_category(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, *, waggle_version, properties=None: captured.append((event, properties or {})),
    )

    telemetry.capture_tool_event(
        "query_graph",
        structured={
            "error_type": "Validation Failure",
            "exception_message": "do not send /Users/example/project",
            "query": "do not send query",
        },
        is_error=True,
        waggle_version="0.1.test",
        transport="http",
        backend="sqlite",
        embedding_mode="deterministic",
    )

    assert captured[0][0] == "operation_failed"
    assert captured[0][1]["success"] is False
    assert captured[0][1]["error_category"] == "validation_failure"
    assert "exception_message" not in captured[0][1]
    assert "query" not in captured[0][1]


def test_telemetry_doc_lists_allowlists_and_forbidden_examples() -> None:
    text = (DOCS_ROOT / "telemetry.md").read_text(encoding="utf-8")

    for event in telemetry.ALLOWED_EVENTS:
        assert f"`{event}`" in text
    for property_name in telemetry.ALLOWED_PROPERTIES:
        assert f"`{property_name}`" in text
    for forbidden in ["query text", "prompts", "memory text", "file paths", "repository names", "stack traces"]:
        assert forbidden in text
