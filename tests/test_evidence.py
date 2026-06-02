"""Unit tests for the pure functions in ``src/waggle/evidence.py``.

These are exercised indirectly during observation integration tests, but doesn't catch:
- Regressions in the deduplication key
- Validity-window logic
- Span lookup,
- Ranking sort order.

This file covers each of the four functions directly, with no database / server required:

- ``merge_evidence_records``: composite-key dedup and recency sort.
- ``merge_validity_windows``: earliest ``valid_from`` / latest ``valid_to``.
- ``build_observation_evidence``: locating a ``source_text`` span in a transcript.
- ``rank_node_evidence``: lexical-overlap ranking with a recency fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from waggle.evidence import (
    build_observation_evidence,
    merge_evidence_records,
    merge_validity_windows,
    rank_node_evidence,
)
from waggle.models import EvidenceRecord, Node, NodeType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    """Return a deterministic UTC timestamp offset from ``BASE_TIME``."""
    return BASE_TIME + timedelta(minutes=minutes)


def _record(
    *,
    session_id: str = "s1",
    turn_index: int = 0,
    source_role: str = "user",
    source_text: str = "hello world",
    source_span_start: int | None = 0,
    source_span_end: int | None = 11,
    observed_at: datetime | None = None,
) -> EvidenceRecord:
    """Build an ``EvidenceRecord`` with sensible, overridable defaults."""
    return EvidenceRecord(
        session_id=session_id,
        turn_index=turn_index,
        source_role=source_role,
        source_text=source_text,
        source_span_start=source_span_start,
        source_span_end=source_span_end,
        observed_at=observed_at if observed_at is not None else BASE_TIME,
    )


def _node_with_evidence(records: list[EvidenceRecord]) -> Node:
    """Build a minimal ``Node`` carrying the given evidence records."""
    return Node(
        label="test-node",
        content="test content",
        node_type=NodeType.FACT,
        evidence_records=records,
    )


# ---------------------------------------------------------------------------
# merge_evidence_records
# ---------------------------------------------------------------------------


def test_merge_dedupes_records_sharing_a_composite_key() -> None:
    # Same key fields (session_id, turn_index, source_role, source_text, spans)
    # even though evidence_id and observed_at differ -> collapse to one.
    existing = _record(observed_at=_at(0))
    incoming = _record(observed_at=_at(5))  # distinct evidence_id, same key

    merged = merge_evidence_records([existing], [incoming])

    assert len(merged) == 1
    # The first occurrence (the existing record) is the one that survives.
    assert merged[0].evidence_id == existing.evidence_id


def test_merge_keeps_records_with_different_keys() -> None:
    # Differ in turn_index -> different composite key -> both survive.
    a = _record(turn_index=0, observed_at=_at(0))
    b = _record(turn_index=1, observed_at=_at(0))

    merged = merge_evidence_records([a], [b])

    assert len(merged) == 2
    assert {r.evidence_id for r in merged} == {a.evidence_id, b.evidence_id}


def test_merge_sorts_by_observed_at_descending() -> None:
    # Three distinct keys with distinct timestamps, fed in scrambled order.
    oldest = _record(turn_index=1, observed_at=_at(0))
    middle = _record(turn_index=2, observed_at=_at(10))
    newest = _record(turn_index=3, observed_at=_at(20))

    merged = merge_evidence_records([middle, oldest, newest], [])

    observed = [r.observed_at for r in merged]
    assert observed == [_at(20), _at(10), _at(0)]
    assert merged[0].evidence_id == newest.evidence_id
    assert merged[-1].evidence_id == oldest.evidence_id


def test_merge_handles_empty_inputs() -> None:
    assert merge_evidence_records([], []) == []


# ---------------------------------------------------------------------------
# merge_validity_windows
# ---------------------------------------------------------------------------


def test_validity_windows_all_none_returns_none_none() -> None:
    assert merge_validity_windows(None, None, None, None) == (None, None)


def test_validity_windows_single_side_set_returns_that_value() -> None:
    only_from = _at(0)
    # Only an existing valid_from is provided; everything else is None.
    assert merge_validity_windows(only_from, None, None, None) == (only_from, None)

    only_to = _at(30)
    # Only an incoming valid_to is provided.
    assert merge_validity_windows(None, None, None, only_to) == (None, only_to)


def test_validity_windows_both_sides_set_takes_min_from_and_max_to() -> None:
    existing_from, incoming_from = _at(10), _at(0)
    existing_to, incoming_to = _at(40), _at(90)

    valid_from, valid_to = merge_validity_windows(
        existing_from,
        incoming_from,
        existing_to,
        incoming_to,
    )

    assert valid_from == _at(0)  # earliest of the two valid_from candidates
    assert valid_to == _at(90)  # latest of the two valid_to candidates


# ---------------------------------------------------------------------------
# build_observation_evidence
# ---------------------------------------------------------------------------


def test_build_observation_evidence_found_returns_correct_span() -> None:
    transcript = "Alice likes coffee and tea"
    source_text = "likes coffee"

    record = build_observation_evidence(
        transcript=transcript,
        source_text=source_text,
        speaker="user",
        turn_index=2,
        observed_at=BASE_TIME,
        session_id="s1",
    )

    assert record.source_span_start == 6
    assert record.source_span_end == 18
    assert transcript[record.source_span_start : record.source_span_end] == source_text
    # Other fields are propagated through.
    assert record.source_role == "user"
    assert record.turn_index == 2
    assert record.source_text == source_text


def test_build_observation_evidence_not_found_returns_none_spans() -> None:
    record = build_observation_evidence(
        transcript="Alice likes coffee and tea",
        source_text="missing phrase",
        speaker="assistant",
        turn_index=0,
        observed_at=BASE_TIME,
    )

    assert record.source_span_start is None
    assert record.source_span_end is None


# ---------------------------------------------------------------------------
# rank_node_evidence
# ---------------------------------------------------------------------------


def test_rank_query_match_outranks_more_recent_nonmatch() -> None:
    # The matching record is older, the non-matching record is newer.
    # Lexical overlap is the primary sort key, so the match must win.
    match = _record(source_text="python testing is great", observed_at=_at(0))
    nonmatch = _record(source_text="completely unrelated content", observed_at=_at(60))
    node = _node_with_evidence([nonmatch, match])

    ranked = rank_node_evidence(node, query="python testing")

    assert ranked[0].evidence_id == match.evidence_id
    assert ranked[1].evidence_id == nonmatch.evidence_id


def test_rank_empty_query_falls_back_to_observed_at() -> None:
    older = _record(source_text="anything at all", observed_at=_at(0))
    newer = _record(source_text="something else entirely", observed_at=_at(30))
    node = _node_with_evidence([older, newer])

    ranked = rank_node_evidence(node, query="")

    # With no query every lexical score is 0, so ordering is recency-descending.
    assert [r.evidence_id for r in ranked] == [newer.evidence_id, older.evidence_id]


def test_rank_limit_caps_output() -> None:
    records = [_record(turn_index=i, observed_at=_at(i)) for i in range(5)]
    node = _node_with_evidence(records)

    assert len(rank_node_evidence(node, query="", limit=2)) == 2
    assert len(rank_node_evidence(node, query="", limit=1)) == 1


def test_rank_nonpositive_limit_returns_all() -> None:
    # limit <= 0 disables the cap and returns the full ranked list.
    records = [_record(turn_index=i, observed_at=_at(i)) for i in range(4)]
    node = _node_with_evidence(records)

    assert len(rank_node_evidence(node, query="", limit=0)) == 4


def test_build_observation_evidence_repeated_snippet():
    transcript = "hello world hello world"

    evidence = build_observation_evidence(
        transcript=transcript,
        source_text="hello world",
        speaker="user",
        turn_index=0,
        observed_at=datetime.now(),
        search_start=12,
    )

    assert evidence.source_span_start == 12
    assert evidence.source_span_end == 23


def test_build_observation_evidence_not_found():
    evidence = build_observation_evidence(
        transcript="hello world",
        source_text="missing text",
        speaker="user",
        turn_index=0,
        observed_at=datetime.now(),
    )

    assert evidence.source_span_start is None
    assert evidence.source_span_end is None
