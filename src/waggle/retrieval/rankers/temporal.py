"""Temporal ranking with topic gating for latest/oldest graph queries."""

from __future__ import annotations
# AFTER
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Literal, Protocol, TypeVar

from waggle.retrieval.scorers.topic_relevance import TopicScore, score_topic_relevance

TOPIC_THRESHOLD = 0.35
TemporalDirection = Literal["latest", "oldest"]


class HasTimestamp(Protocol):
    """Structural type for anything rank_temporal can sort by time."""

    ts: datetime | str


CandidateT = TypeVar("CandidateT", bound=HasTimestamp)


@dataclass(frozen=True)
class RankedTemporalCandidate(Generic[CandidateT]):
    """A temporal candidate paired with its topic score."""

    candidate: CandidateT
    topic_score: TopicScore
    timestamp: datetime


def parse_timestamp(value: datetime | str) -> datetime:
    """Parse candidate timestamps from datetime or ISO-8601 strings."""
    if isinstance(value, datetime):
        return value
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def rank_temporal(
    query: str,
    candidates: Sequence[CandidateT],
    *,
    direction: TemporalDirection,
    top_k: int,
    topic_threshold: float = TOPIC_THRESHOLD,
    semantic_similarity_fn: Callable[[str, str], float] | None = None,
) -> list[CandidateT]:
    """Rank temporal candidates within the queried topic.

    Candidates are first scored for topic relevance using:
        combined = (0.7 * semantic_similarity) + (0.3 * lexical_overlap)

    Candidates below ``topic_threshold`` are discarded before temporal sorting.
    If no candidate survives, the function falls back to the top ``top_k * 2``
    candidates by topic score, then applies temporal sorting to that fallback
    set. Latest sorts timestamps descending; oldest sorts ascending. Timestamp
    ties prefer higher topic score.
    """
    if top_k < 1:
        raise ValueError("top_k must be at least 1.")
    if direction not in {"latest", "oldest"}:
        raise ValueError("direction must be 'latest' or 'oldest'.")

    ranked = [
        RankedTemporalCandidate[CandidateT](
            candidate=candidate,
            topic_score=score_topic_relevance(
                query,
                candidate,
                semantic_similarity_fn=semantic_similarity_fn,
            ),
            timestamp=parse_timestamp(candidate.ts),
        )
        for candidate in candidates
    ]

    survivors = [item for item in ranked if item.topic_score.combined >= topic_threshold]
    if not survivors:
        fallback_limit = min(len(ranked), top_k * 2)
        survivors = sorted(
            ranked,
            key=lambda item: item.topic_score.combined,
            reverse=True,
        )[:fallback_limit]

    if direction == "latest":
        ordered = sorted(
            survivors,
            key=lambda item: (
                -item.timestamp.timestamp(),
                -item.topic_score.combined,
            ),
        )
    else:
        ordered = sorted(
            survivors,
            key=lambda item: (
                item.timestamp.timestamp(),
                -item.topic_score.combined,
            ),
        )

    return [item.candidate for item in ordered[:top_k]]
