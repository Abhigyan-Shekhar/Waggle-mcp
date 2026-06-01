from __future__ import annotations

from datetime import datetime

from waggle.models import EvidenceRecord, Node


def merge_evidence_records(
    existing: list[EvidenceRecord],
    incoming: list[EvidenceRecord],
) -> list[EvidenceRecord]:
    merged: list[EvidenceRecord] = []
    seen: set[tuple[str, int, str, str, int | None, int | None]] = set()
    for record in [*existing, *incoming]:
        key = (
            record.session_id,
            record.turn_index,
            record.source_role,
            record.source_text,
            record.source_span_start,
            record.source_span_end,
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
    merged.sort(
        key=lambda item: (
            item.observed_at,
            item.turn_index,
            item.source_role,
            item.source_text,
        ),
        reverse=True,
    )
    return merged


def merge_validity_windows(
    existing_from: datetime | None,
    incoming_from: datetime | None,
    existing_to: datetime | None,
    incoming_to: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    valid_from_candidates = [value for value in (existing_from, incoming_from) if value is not None]
    valid_to_candidates = [value for value in (existing_to, incoming_to) if value is not None]
    valid_from = min(valid_from_candidates) if valid_from_candidates else None
    valid_to = max(valid_to_candidates) if valid_to_candidates else None
    return valid_from, valid_to


def build_observation_evidence(
    *,
    transcript: str,
    source_text: str,
    speaker: str,
    turn_index: int,
    observed_at: datetime,
    session_id: str = "",
    search_start: int = 0,
) -> EvidenceRecord:
    start = transcript.find(source_text, search_start)
    if start < 0:
        start = None
        end = None
    else:
        end = start + len(source_text)
    return EvidenceRecord(
        session_id=session_id,
        turn_index=turn_index,
        source_role=speaker,
        source_text=source_text,
        source_span_start=start,
        source_span_end=end,
        observed_at=observed_at,
    )


def rank_node_evidence(node: Node, *, query: str = "", limit: int = 3) -> list[EvidenceRecord]:
    lowered_query = query.strip().lower()
    scored: list[tuple[float, datetime, EvidenceRecord]] = []
    for record in node.evidence_records:
        lexical = 0.0
        if lowered_query:
            query_tokens = set(lowered_query.split())
            evidence_tokens = set(record.source_text.lower().split())
            lexical = len(query_tokens & evidence_tokens) / max(len(query_tokens), 1)
        scored.append((lexical, record.observed_at, record))
    ordered = [
        item[2]
        for item in sorted(
            scored,
            key=lambda item: (item[0], item[1]),
            reverse=True,
        )
    ]
    return ordered[:limit] if limit > 0 else ordered
