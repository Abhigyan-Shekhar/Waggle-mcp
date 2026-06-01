from datetime import datetime

from waggle.evidence import build_observation_evidence


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
