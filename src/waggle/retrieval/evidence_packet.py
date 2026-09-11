"""Opt-in, label-blind evidence-union delivery. No retrieval or path selection.

Callers supply ranked candidates and exact source spans (e.g. contiguous cards
or canonical whole-message spans). The compiler never searches or clips text.
The required counter must count the target model's serialized evidence payload;
there is deliberately no heuristic/tokenizer fallback. Prompt framing and output
reserve are the caller's responsibility.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceSpan:
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    text: str
    sha256: str


@dataclass(frozen=True)
class CandidateSource:
    source_id: str
    date: str
    raw_text: str
    raw_sha256: str
    # Input order is extraction priority, not chronological order.
    spans: tuple[SourceSpan, ...]


@dataclass(frozen=True)
class PacketDecision:
    source_id: str
    span_index: int | None
    reason: str


@dataclass(frozen=True)
class EvidencePacket:
    text: str
    token_count: int
    tokenizer_id: str
    sha256: str
    source_ids: tuple[str, ...]
    decisions: tuple[PacketDecision, ...]


def _validate(source: CandidateSource) -> None:
    if not source.source_id or digest(source.raw_text) != source.raw_sha256:
        raise ValueError("Missing source ID or source SHA mismatch")
    encoded = source.raw_text.encode("utf-8")
    for span in source.spans:
        if not (0 <= span.char_start < span.char_end <= len(source.raw_text)):
            raise ValueError("Invalid character offsets")
        if not (0 <= span.byte_start < span.byte_end <= len(encoded)):
            raise ValueError("Invalid byte offsets")
        if (
            len(source.raw_text[: span.char_start].encode("utf-8")) != span.byte_start
            or len(source.raw_text[: span.char_end].encode("utf-8")) != span.byte_end
        ):
            raise ValueError("Character/byte offset mismatch")
        if (
            source.raw_text[span.char_start : span.char_end] != span.text
            or encoded[span.byte_start : span.byte_end] != span.text.encode("utf-8")
            or digest(span.text) != span.sha256
        ):
            raise ValueError("Span provenance mismatch")


def _render(sources: Sequence[CandidateSource], selected: dict[str, list[SourceSpan]]) -> str:
    blocks = []
    for source in sources:
        spans = selected.get(source.source_id, [])
        if not spans:
            continue
        # Metadata is JSON escaped; evidence text is appended verbatim.
        blocks.append(
            json.dumps(
                {"source_id": source.source_id, "date": source.date, "source_sha256": source.raw_sha256}, sort_keys=True
            )
        )
        for span in sorted(spans, key=lambda s: s.char_start):
            blocks.append(
                json.dumps(
                    {
                        "chars": [span.char_start, span.char_end],
                        "bytes": [span.byte_start, span.byte_end],
                        "sha256": span.sha256,
                    },
                    sort_keys=True,
                )
            )
            blocks.append(span.text)
    return "\n\n".join(blocks)


def compile_evidence_packet(
    candidates: Sequence[CandidateSource],
    *,
    count_tokens: Callable[[str], int],
    tokenizer_id: str,
    max_tokens: int = 8000,
    max_sources: int = 10,
    per_source_tokens: int = 800,
) -> EvidencePacket:
    """Pack ranked sources in rounds, preserving whole spans and provenance.

    Each source gets a chance at one fitting span before enrichment. Oversized
    spans are skipped, not truncated. All supplied provenance is validated before
    selection, including candidates subsequently excluded by budgets.
    """
    if not tokenizer_id.strip() or min(max_tokens, max_sources, per_source_tokens) <= 0:
        raise ValueError("Positive budgets and an explicit tokenizer ID are required")

    def count(payload: str) -> int:
        value = count_tokens(payload)
        if type(value) is not int or value < 0 or (payload and value == 0):
            raise ValueError("Token counter returned an invalid count")
        return value

    sources: list[CandidateSource] = []
    seen: dict[str, CandidateSource] = {}
    decisions: list[PacketDecision] = []
    for source in candidates:
        _validate(source)
        if source.source_id in seen:
            if seen[source.source_id] != source:
                raise ValueError("Conflicting duplicate source ID")
            decisions.append(PacketDecision(source.source_id, None, "duplicate_source"))
            continue
        seen[source.source_id] = source
        sources.append(source)

    selected: dict[str, list[SourceSpan]] = {}
    pending = {s.source_id: list(enumerate(s.spans)) for s in sources}
    while any(pending.values()):
        for source in sources:
            queue = pending[source.source_id]
            while queue:
                index, span = queue.pop(0)
                admitted = selected.get(source.source_id, [])
                reason = ""
                if not admitted and len(selected) >= max_sources:
                    reason = "source_limit"
                elif any(span.char_start < s.char_end and s.char_start < span.char_end for s in admitted):
                    reason = "overlap_or_duplicate"
                else:
                    proposed = {**selected, source.source_id: [*admitted, span]}
                    if count(_render([source], proposed)) > per_source_tokens:
                        reason = "source_budget"
                    elif count(_render(sources, proposed)) > max_tokens:
                        reason = "packet_budget"
                    else:
                        selected = proposed
                decisions.append(PacketDecision(source.source_id, index, reason or "included"))
                if not reason:
                    break  # one admission per source per round
            if not source.spans and not any(d.source_id == source.source_id for d in decisions):
                decisions.append(PacketDecision(source.source_id, None, "no_spans"))
    # Empty sources also need accounting when the entire pool has no spans.
    for source in sources:
        if not source.spans and not any(d.source_id == source.source_id for d in decisions):
            decisions.append(PacketDecision(source.source_id, None, "no_spans"))
    text = _render(sources, selected)
    tokens = count(text)
    if tokens > max_tokens:
        raise ValueError("Final packet exceeds budget (token counter must be deterministic)")
    return EvidencePacket(
        text,
        tokens,
        tokenizer_id,
        digest(text),
        tuple(s.source_id for s in sources if s.source_id in selected),
        tuple(decisions),
    )
