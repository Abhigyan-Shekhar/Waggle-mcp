#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import plan_longmemeval_run
import run_longmemeval_waggle_phase as waggle_runner
from waggle.intelligence import extract_conversation_candidates


NARRATIVE_MARKERS: tuple[tuple[str, str], ...] = (
    ("remember", r"\bremember(?:ed|ing)?\b"),
    ("memory", r"\bmemories?\b"),
    ("nostalgic", r"\bnostalgic\b"),
    ("used_to", r"\bused to\b"),
    ("when_i_was", r"\bwhen i was\b"),
    ("as_a_kid", r"\bas a kid\b"),
    ("childhood", r"\bchildhood\b"),
    ("growing_up", r"\bgrowing up\b"),
    ("high_school", r"\bhigh school\b"),
    ("college", r"\bcollege\b"),
    ("university", r"\buniversity\b"),
    ("school", r"\bschool\b"),
    ("debate_team", r"\bdebate team\b"),
    ("advanced_placement", r"\badvanced placement\b|\bAP\b"),
    ("reunion", r"\breunion\b"),
    ("old_friends", r"\bold friends?\b"),
    ("family", r"\bfamily\b"),
    ("parents", r"\bparents?\b|\bmother\b|\bfather\b|\bmom\b|\bdad\b"),
    ("grandparent", r"\bgrand(?:mother|father|parent)s?\b"),
    ("hometown", r"\bhometown\b"),
    ("grew_up", r"\bgrew up\b"),
    ("past_experience", r"\bpast experiences?\b|\bexperience(?:d)?\b"),
    ("emotion_happy", r"\bhappy\b|\bglad\b|\bproud\b"),
    ("emotion_sad", r"\bsad\b|\bmiss\b|\bmissed\b"),
    ("emotion_surprised", r"\bsurprised\b"),
    ("loved_enjoyed", r"\bloved\b|\benjoyed\b"),
)

STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "before",
    "could",
    "doing",
    "from",
    "have",
    "into",
    "just",
    "like",
    "more",
    "much",
    "need",
    "only",
    "really",
    "should",
    "some",
    "such",
    "that",
    "their",
    "there",
    "these",
    "thing",
    "think",
    "this",
    "those",
    "want",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "with",
    "would",
}


def _tokens(text: str) -> set[str]:
    return {token for token in waggle_runner._content_tokens(text) if len(token) >= 5 and token not in STOPWORDS}


def _matched_markers(text: str) -> list[str]:
    return [
        name
        for name, pattern in NARRATIVE_MARKERS
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]


def _candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(str(candidate.get(field, "") or "") for field in ("label", "content", "node_type", "tags"))


def _extract_for_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    text = waggle_runner._message_text(message)
    role = waggle_runner._message_role(message)
    kwargs = {"user_message": "", "assistant_response": ""}
    if role == "assistant":
        kwargs["assistant_response"] = text
    else:
        kwargs["user_message"] = text
    return extract_conversation_candidates(**kwargs)


def _audit_turn(case_id: str, case: dict[str, Any], session_id: str, message: dict[str, Any]) -> dict[str, Any]:
    source_text = waggle_runner._message_text(message)
    source_tokens = _tokens(source_text)
    markers = _matched_markers(source_text)
    candidates = _extract_for_message(message)
    combined_candidates = "\n".join(_candidate_text(candidate) for candidate in candidates)
    candidate_tokens = _tokens(combined_candidates)
    preserved_markers = _matched_markers(combined_candidates)
    missing_markers = sorted(set(markers) - set(preserved_markers))
    overlap = sorted(source_tokens & candidate_tokens)
    missing_tokens = sorted(source_tokens - candidate_tokens)

    return {
        "case_id": case_id,
        "category": waggle_runner._task(case),
        "question": waggle_runner._question(case),
        "session_id": session_id,
        "role": waggle_runner._message_role(message),
        "narrative_like": bool(markers),
        "markers": markers,
        "preserved_markers": preserved_markers,
        "missing_markers": missing_markers,
        "source_token_count": len(source_tokens),
        "candidate_token_count": len(candidate_tokens),
        "overlap_count": len(overlap),
        "overlap_rate": round(len(overlap) / max(1, len(source_tokens)), 4),
        "missing_distinctive_tokens": missing_tokens[:40],
        "source_text": " ".join(source_text.split())[:800],
        "candidates": [
            {
                "node_type": str(candidate.get("node_type", "")),
                "label": " ".join(str(candidate.get("label", "")).split())[:220],
                "content": " ".join(str(candidate.get("content", "")).split())[:320],
                "tags": candidate.get("tags", []),
            }
            for candidate in candidates
        ],
    }


def audit_dataset(dataset: Path) -> dict[str, Any]:
    cases = plan_longmemeval_run._load_cases(dataset)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        if waggle_runner._task(case) != "single-session-preference":
            continue
        case_id = plan_longmemeval_run._case_id(case, index)
        gold_sessions = set(waggle_runner._gold_support_ids(case))
        for session_id, messages in zip(case.get("haystack_session_ids") or [], case.get("haystack_sessions") or []):
            if session_id not in gold_sessions:
                continue
            for message in messages:
                if isinstance(message, dict) and message.get("has_answer"):
                    rows.append(_audit_turn(case_id, case, session_id, message))

    narrative_rows = [row for row in rows if row["narrative_like"]]
    weak_preservation = [
        row
        for row in narrative_rows
        if row["missing_markers"] or row["overlap_rate"] < 0.35
    ]
    return {
        "dataset": str(dataset),
        "scope": "all LongMemEval-S single-session-preference has_answer turns",
        "summary": {
            "ssp_has_answer_turns": len(rows),
            "narrative_like_turns": len(narrative_rows),
            "weak_narrative_preservation_turns": len(weak_preservation),
            "narrative_preservation_rate": round(
                (len(narrative_rows) - len(weak_preservation)) / max(1, len(narrative_rows)), 4
            ),
        },
        "weak_narrative_preservation": weak_preservation,
        "turns": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit SSP autobiographical extraction preservation.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = audit_dataset(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    summary = payload["summary"]
    print(
        "SSP turns={ssp_has_answer_turns} narrative_like={narrative_like_turns} "
        "weak_narrative_preservation={weak_narrative_preservation_turns} "
        "preservation_rate={narrative_preservation_rate}".format(**summary)
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
