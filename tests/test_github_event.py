from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waggle.errors import ValidationFailure
from waggle.github_event import (
    load_event_payload,
    normalize_event_type,
    normalize_github_event,
    sanitize_generic_payload,
    sanitize_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github_events"


@pytest.mark.parametrize(
    ("filename", "event_type", "subject_key"),
    [
        ("issue.json", "issue", "issue:42"),
        ("pull_request.json", "pull-request", "pull-request:17"),
        ("discussion.json", "discussion", "discussion:9"),
        ("release.json", "release", "release:501"),
        ("push.json", "push", "push:refs/heads/main:after-sha"),
    ],
)
def test_normalizes_supported_event(filename: str, event_type: str, subject_key: str) -> None:
    payload = load_event_payload(FIXTURES / filename)

    event = normalize_github_event(payload, event_type=event_type, repository="octo/demo")

    assert event.event_type == event_type
    assert event.subject_key == subject_key
    assert event.repository.name == "octo/demo"
    assert event.actor is not None
    assert event.actor.login == "octocat"
    assert event.occurred_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("filename", "want"),
    [
        ("issue.json", "issue"),
        ("pull_request.json", "pull-request"),
        ("discussion.json", "discussion"),
        ("release.json", "release"),
        ("push.json", "push"),
    ],
)
def test_infers_known_event_types(filename: str, want: str) -> None:
    payload = load_event_payload(FIXTURES / filename)

    assert normalize_event_type("", "", payload) == want


def test_explicit_event_type_wins_over_environment_and_inference() -> None:
    payload = load_event_payload(FIXTURES / "issue.json")

    assert normalize_event_type("generic", "pull_request", payload) == "generic"


def test_normalizes_github_environment_event_names() -> None:
    assert normalize_event_type("", "issues", {}) == "issue"
    assert normalize_event_type("", "pull_request", {}) == "pull-request"


def test_unknown_event_is_not_silently_generic() -> None:
    assert normalize_event_type("", "fork", {"forkee": {"id": 1}}) is None


def test_issue_sanitizes_secret_without_changing_multiline_shell_text() -> None:
    payload = load_event_payload(FIXTURES / "issue.json")

    event = normalize_github_event(payload, event_type="issue", repository="octo/demo")
    serialized = event.model_dump_json()

    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in serialized
    assert "[REDACTED]" in event.content
    assert "$(touch /tmp/waggle-owned)" in event.content
    assert "line one\nline two" in event.content
    assert "café" in event.content


def test_generic_payload_removes_sensitive_keys_recursively() -> None:
    payload = load_event_payload(FIXTURES / "generic.json")

    sanitized = sanitize_generic_payload(payload)
    rendered = json.dumps(sanitized, sort_keys=True)

    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in rendered
    assert "password" not in rendered.lower()
    assert "authorization" not in rendered.lower()
    assert "token" not in rendered.lower()
    assert sanitized["nested"] == {"priority": "high"}


def test_sanitize_text_removes_url_credentials_and_sensitive_query_values() -> None:
    value = "https://user:pass@example.com/path?token=FIXTURE_SECRET_DO_NOT_LEAK&view=public"

    sanitized = sanitize_text(value, max_chars=500)

    assert "user:pass" not in sanitized
    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in sanitized
    assert "view=public" in sanitized


def test_load_event_payload_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailure, match="not found"):
        load_event_payload(tmp_path / "missing.json")


def test_load_event_payload_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValidationFailure, match="valid JSON"):
        load_event_payload(path)


def test_load_event_payload_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValidationFailure, match="JSON object"):
        load_event_payload(path)


def test_load_event_payload_rejects_oversized_input(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text('{"body":"1234567890"}', encoding="utf-8")

    with pytest.raises(ValidationFailure, match="exceeds"):
        load_event_payload(path, max_input_bytes=10)


def test_normalize_known_event_rejects_missing_subject() -> None:
    with pytest.raises(ValidationFailure, match="issue"):
        normalize_github_event({}, event_type="issue", repository="octo/demo")


def test_generic_payload_rejects_excessive_depth() -> None:
    payload: dict[str, object] = {}
    cursor = payload
    for index in range(20):
        child: dict[str, object] = {"value": index}
        cursor["child"] = child
        cursor = child

    with pytest.raises(ValidationFailure, match="nesting depth"):
        sanitize_generic_payload(payload)
