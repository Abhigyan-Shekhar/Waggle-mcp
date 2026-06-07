from __future__ import annotations

import pytest

from waggle.intelligence import extract_conversation_candidates
from waggle.models import NodeType


def _candidates(user_message: str, assistant_response: str = "") -> list[dict]:
    return extract_conversation_candidates(
        user_message=user_message,
        assistant_response=assistant_response,
    )


def _types(candidates: list[dict]) -> list[NodeType]:
    return [c["node_type"] for c in candidates]


# ---------------------------------------------------------------------------
# 1. TODO detection
# ---------------------------------------------------------------------------


def test_todo_detection_produces_node_with_todo_content() -> None:
    results = _candidates("TODO: ship the migration")
    assert results, "expected at least one candidate"
    assert any(str(c["content"]).startswith("TODO:") for c in results)


# ---------------------------------------------------------------------------
# 2. Decision detection
# ---------------------------------------------------------------------------


def test_decision_detection() -> None:
    results = _candidates("We decided to use PostgreSQL")
    assert NodeType.DECISION in _types(results)


# ---------------------------------------------------------------------------
# 3. Preference detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "I prefer dark mode",
        "I prefer TypeScript",
    ],
)
def test_preference_detection(message: str) -> None:
    results = _candidates(message)
    assert NodeType.PREFERENCE in _types(results), f"no PREFERENCE in {results!r}"


# ---------------------------------------------------------------------------
# 4. Question detection
# ---------------------------------------------------------------------------


def test_question_detection() -> None:
    results = _candidates("What's the deployment target?")
    assert NodeType.QUESTION in _types(results)


# ---------------------------------------------------------------------------
# 5. Fact extraction
# ---------------------------------------------------------------------------


def test_fact_extraction() -> None:
    results = _candidates("The API rate limit is 100 requests per minute")
    assert NodeType.FACT in _types(results)


# ---------------------------------------------------------------------------
# 6. Multiple candidates
# ---------------------------------------------------------------------------


def test_multiple_candidates_from_decision_and_todo() -> None:
    results = _candidates(
        "We decided to use PostgreSQL. TODO: migrate the old tables."
    )
    assert len(results) >= 2


# ---------------------------------------------------------------------------
# 7. Empty / whitespace input
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_list() -> None:
    assert _candidates("") == []


def test_whitespace_only_returns_empty_list() -> None:
    assert _candidates("   \t\n  ") == []


# ---------------------------------------------------------------------------
# 8. Non-English input — must not crash (lock current behaviour)
# ---------------------------------------------------------------------------


def test_japanese_input_does_not_crash() -> None:
    results = _candidates("データベースはPostgreSQLを使っています。")
    assert isinstance(results, list)


def test_german_input_does_not_crash() -> None:
    results = _candidates("Wir haben entschieden, PostgreSQL zu verwenden.")
    assert isinstance(results, list)
