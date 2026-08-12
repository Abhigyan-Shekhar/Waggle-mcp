from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waggle.embeddings import EmbeddingModel
from waggle.errors import ValidationFailure
from waggle.github_event import (
    ingest_normalized_event,
    load_event_payload,
    normalize_event_type,
    normalize_github_event,
    sanitize_generic_payload,
    sanitize_text,
)
from waggle.graph import MemoryGraph
from waggle.models import NodeType

FIXTURES = Path(__file__).parent / "fixtures" / "github_events"


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(
        tmp_path / "memory.db",
        EmbeddingModel("deterministic"),
        enable_dedup=False,
    )


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


def test_issue_with_non_list_labels_normalizes_to_empty_labels() -> None:
    payload = load_event_payload(FIXTURES / "issue.json")
    payload["issue"]["labels"] = None

    event = normalize_github_event(payload, event_type="issue", repository="octo/demo")

    assert event.metadata["labels"] == []


def test_generic_payload_removes_sensitive_keys_recursively() -> None:
    payload = load_event_payload(FIXTURES / "generic.json")

    sanitized = sanitize_generic_payload(payload)
    rendered = json.dumps(sanitized, sort_keys=True)

    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in rendered
    assert "password" not in rendered.lower()
    assert "authorization" not in rendered.lower()
    assert "token" not in rendered.lower()
    assert sanitized["nested"] == {"priority": "high"}


def test_workflow_dispatch_without_timestamp_uses_explicit_fallback() -> None:
    payload = load_event_payload(FIXTURES / "generic.json")
    fallback = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)

    event = normalize_github_event(
        payload,
        event_type="workflow_dispatch",
        repository="octo/demo",
        fallback_occurred_at=fallback,
    )

    assert event.event_type == "generic"
    assert event.occurred_at == fallback


def test_sanitize_text_removes_url_credentials_and_sensitive_query_values() -> None:
    value = "https://user:pass@example.com/path?token=FIXTURE_SECRET_DO_NOT_LEAK&view=public"

    sanitized = sanitize_text(value, max_chars=500)

    assert "user:pass" not in sanitized
    assert "FIXTURE_SECRET_DO_NOT_LEAK" not in sanitized
    assert "view=public" in sanitized


def test_sanitize_text_redacts_fine_grained_github_tokens() -> None:
    token = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"

    sanitized = sanitize_text(f"token: {token}", max_chars=500)

    assert token not in sanitized
    assert sanitized == "token: [REDACTED]"


def test_sanitize_text_redacts_urls_with_invalid_ports() -> None:
    sanitized = sanitize_text("See https://example.com:invalid/path", max_chars=500)

    assert sanitized == "See [REDACTED]"


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


def test_ingest_issue_creates_repository_actor_event_and_provenance(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    event = normalize_github_event(
        load_event_payload(FIXTURES / "issue.json"),
        event_type="issue",
        repository="octo/demo",
    )

    result = ingest_normalized_event(graph, event, project="octo/demo")
    snapshot = graph.get_graph_snapshot(project="octo/demo")

    assert result.nodes_added == 3
    assert result.edges_added == 2
    assert len(snapshot["nodes"]) == 3
    assert len(snapshot["edges"]) == 2
    event_node = graph.get_node(result.event_node_id)
    assert event_node.node_type == NodeType.NOTE
    assert event_node.project == "octo/demo"
    assert event_node.created_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert event_node.metadata["repository"] == "octo/demo"
    assert event_node.metadata["event_type"] == "issue"
    assert event_node.metadata["actor"] == "octocat"
    assert event_node.metadata["source_url"] == "https://github.com/octo/demo/issues/42"
    assert event_node.metadata["number"] == "42"
    assert event_node.evidence_records[0].observed_at == event.occurred_at


def test_reingesting_same_event_is_idempotent(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    event = normalize_github_event(
        load_event_payload(FIXTURES / "issue.json"),
        event_type="issue",
        repository="octo/demo",
    )

    first = ingest_normalized_event(graph, event, project="octo/demo")
    second = ingest_normalized_event(graph, event, project="octo/demo")
    snapshot = graph.get_graph_snapshot(project="octo/demo")

    assert first.nodes_added == 3
    assert first.edges_added == 2
    assert second.nodes_added == 0
    assert second.edges_added == 0
    assert len(snapshot["nodes"]) == 3
    assert len(snapshot["edges"]) == 2


def test_ingestion_counts_edges_without_materializing_graph_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = make_graph(tmp_path)
    event = normalize_github_event(
        load_event_payload(FIXTURES / "issue.json"),
        event_type="issue",
        repository="octo/demo",
    )

    def reject_snapshot(*args: object, **kwargs: object) -> None:
        raise AssertionError("ingestion must not materialize the full graph")

    monkeypatch.setattr(graph, "get_graph_snapshot", reject_snapshot)

    result = ingest_normalized_event(graph, event, project="octo/demo")

    assert result.edges_added == 2


def test_ingesting_same_event_into_distinct_projects_keeps_projects_isolated(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    event = normalize_github_event(
        load_event_payload(FIXTURES / "issue.json"),
        event_type="issue",
        repository="octo/demo",
    )

    first = ingest_normalized_event(graph, event, project="project-one")
    second = ingest_normalized_event(graph, event, project="project-two")

    assert first.event_node_id != second.event_node_id
    assert second.nodes_added == 3
    assert second.edges_added == 2
    assert len(graph.get_graph_snapshot(project="project-one")["nodes"]) == 3
    assert len(graph.get_graph_snapshot(project="project-two")["nodes"]) == 3


def test_reingesting_edited_issue_updates_stable_event_node(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    original_payload = load_event_payload(FIXTURES / "issue.json")
    original = normalize_github_event(original_payload, event_type="issue", repository="octo/demo")
    first = ingest_normalized_event(graph, original, project="octo/demo")
    edited_payload = json.loads(json.dumps(original_payload))
    edited_payload["action"] = "edited"
    edited_payload["issue"]["body"] = "Edited body"
    edited_payload["issue"]["updated_at"] = "2025-01-03T04:05:06Z"
    edited = normalize_github_event(edited_payload, event_type="issue", repository="octo/demo")

    second = ingest_normalized_event(graph, edited, project="octo/demo")
    event_node = graph.get_node(first.event_node_id)

    assert second.event_node_id == first.event_node_id
    assert second.nodes_added == 0
    assert second.edges_added == 0
    assert "Edited body" in event_node.content
    assert event_node.metadata["action"] == "edited"
    assert event_node.updated_at == datetime(2025, 1, 3, 4, 5, 6, tzinfo=UTC)


def test_older_issue_redelivery_does_not_overwrite_newer_event_state(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    original_payload = load_event_payload(FIXTURES / "issue.json")
    newer_payload = json.loads(json.dumps(original_payload))
    newer_payload["action"] = "edited"
    newer_payload["issue"]["body"] = "Newest body"
    newer_payload["issue"]["updated_at"] = "2025-01-03T04:05:06Z"
    newer = normalize_github_event(newer_payload, event_type="issue", repository="octo/demo")
    older = normalize_github_event(original_payload, event_type="issue", repository="octo/demo")

    first = ingest_normalized_event(graph, newer, project="octo/demo")
    second = ingest_normalized_event(graph, older, project="octo/demo")
    event_node = graph.get_node(first.event_node_id)

    assert second.event_node_id == first.event_node_id
    assert second.nodes_added == 0
    assert second.edges_added == 0
    assert "Newest body" in event_node.content
    assert event_node.metadata["action"] == "edited"
    assert event_node.updated_at == datetime(2025, 1, 3, 4, 5, 6, tzinfo=UTC)


def test_ingest_push_creates_bounded_commit_children(tmp_path: Path) -> None:
    graph = make_graph(tmp_path)
    event = normalize_github_event(
        load_event_payload(FIXTURES / "push.json"),
        event_type="push",
        repository="octo/demo",
    )

    result = ingest_normalized_event(graph, event, project="octo/demo")
    snapshot = graph.get_graph_snapshot(project="octo/demo")

    assert result.nodes_added == 5
    assert result.edges_added == 4
    assert len([node for node in snapshot["nodes"] if "github-commit" in node["tags"]]) == 2
    assert sum(edge["relationship"] == "part_of" for edge in snapshot["edges"]) == 3


def test_normalize_branch_deletion_push_uses_repository_timestamp() -> None:
    event = normalize_github_event(
        load_event_payload(FIXTURES / "push_deleted.json"),
        event_type="push",
        repository="octo/demo",
    )

    assert event.occurred_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert event.metadata["deleted"] is True
    assert event.children == []
