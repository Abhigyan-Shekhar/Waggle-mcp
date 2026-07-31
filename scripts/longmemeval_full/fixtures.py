from __future__ import annotations

from typing import Any


def capability_fixtures() -> list[dict[str, Any]]:
    """Deterministic no-cost fixtures for capability validation."""
    return [
        {
            "question_id": "fixture_exact_transcript_detail",
            "question_type": "single-session-user",
            "question": "What is the complete release package name?",
            "answer": "Phantom Harbor DLC",
            "gold_support_ids": ["fixture_exact_s1"],
            "haystack_session_ids": ["fixture_exact_s1"],
            "haystack_dates": ["2026-03-01"],
            "haystack_sessions": [
                [
                    {
                        "role": "user",
                        "content": "The release package is named Phantom Harbor DLC. Preserve the full three-word name.",
                    },
                    {"role": "assistant", "content": "Confirmed: Phantom Harbor DLC."},
                ]
            ],
        },
        {
            "question_id": "fixture_update_supersession",
            "question_type": "knowledge-update",
            "question": "Which database should Project Nova currently use?",
            "answer": "CockroachDB",
            "gold_support_ids": ["fixture_update_s2"],
            "haystack_session_ids": ["fixture_update_s1", "fixture_update_s2"],
            "haystack_dates": ["2026-04-01", "2026-04-10"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "Project Nova will use PostgreSQL."},
                    {"role": "assistant", "content": "Understood. Project Nova will use PostgreSQL."},
                ],
                [
                    {"role": "user", "content": "Replace that decision. Project Nova must now use CockroachDB."},
                    {"role": "assistant", "content": "Updated. Project Nova will use CockroachDB."},
                ],
            ],
        },
        {
            "question_id": "fixture_source_authority",
            "question_type": "knowledge-update",
            "question": "What is the confirmed launch date?",
            "answer": "October 14",
            "gold_support_ids": ["fixture_authority_s2"],
            "haystack_session_ids": ["fixture_authority_s1", "fixture_authority_s2"],
            "haystack_dates": ["2026-05-01", "2026-05-12"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "The launch date has not been selected."},
                    {"role": "assistant", "content": "It may be September based on the draft plan."},
                ],
                [
                    {"role": "user", "content": "The confirmed launch date is October 14."},
                    {"role": "assistant", "content": "Confirmed: October 14."},
                ],
            ],
        },
        {
            "question_id": "fixture_graph_dependency",
            "question_type": "multi-session",
            "question": "Which database indirectly supports Project Atlas?",
            "answer": "PostgreSQL",
            "gold_support_ids": ["fixture_dependency_s1", "fixture_dependency_s2"],
            "haystack_session_ids": ["fixture_dependency_s1", "fixture_dependency_s2"],
            "haystack_dates": ["2026-06-01", "2026-06-02"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "Project Atlas depends on Service Orion."},
                    {"role": "assistant", "content": "Noted: Atlas depends on Orion."},
                ],
                [
                    {"role": "user", "content": "Service Orion uses PostgreSQL."},
                    {"role": "assistant", "content": "Noted: Orion uses PostgreSQL."},
                ],
            ],
        },
        {
            "question_id": "fixture_transcript_only_minor_detail",
            "question_type": "single-session-user",
            "question": "What color was the staging badge?",
            "answer": "copper",
            "gold_support_ids": ["fixture_transcript_s1"],
            "haystack_session_ids": ["fixture_transcript_s1"],
            "haystack_dates": ["2026-07-01"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "Small note for later: the staging badge color was copper."},
                    {"role": "assistant", "content": "Okay."},
                ]
            ],
        },
        {
            "question_id": "fixture_prime_relevance",
            "question_type": "knowledge-update",
            "question": "Which API style did Project River choose?",
            "answer": "GraphQL",
            "gold_support_ids": ["fixture_prime_s1"],
            "haystack_session_ids": ["fixture_prime_s1", "fixture_prime_s2", "fixture_prime_s3"],
            "haystack_dates": ["2026-07-02", "2026-07-03", "2026-07-04"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "Decision: Project River will use GraphQL for the public API."},
                    {"role": "assistant", "content": "Saved: Project River uses GraphQL."},
                ],
                [
                    {"role": "user", "content": "Unrelated note: book dentist appointment."},
                    {"role": "assistant", "content": "Noted."},
                ],
                [
                    {"role": "user", "content": "Unrelated note: compare train tickets."},
                    {"role": "assistant", "content": "Noted."},
                ],
            ],
        },
        {
            "question_id": "fixture_exact_compound_phrase",
            "question_type": "single-session-user",
            "question": "What exact show title did I say Morgan recommended?",
            "answer": "Doc Martin",
            "gold_support_ids": ["fixture_compound_s1"],
            "haystack_session_ids": ["fixture_compound_s1", "fixture_compound_s2"],
            "haystack_dates": ["2026-07-05", "2026-07-06"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "Morgan recommended Doc Martin, not just any medical drama. Keep the exact title."},
                    {"role": "assistant", "content": "Saved the exact recommendation: Doc Martin."},
                ],
                [
                    {"role": "user", "content": "Unrelated: I also need to renew the library card."},
                    {"role": "assistant", "content": "Noted."},
                ],
            ],
        },
        {
            "question_id": "fixture_middle_context_update",
            "question_type": "knowledge-update",
            "question": "Which deployment window is currently confirmed?",
            "answer": "Wednesday morning",
            "gold_support_ids": ["fixture_window_s2"],
            "haystack_session_ids": ["fixture_window_s1", "fixture_window_s2", "fixture_window_s3"],
            "haystack_dates": ["2026-07-07", "2026-07-08", "2026-07-09"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "The deployment window might be Tuesday afternoon, but that is tentative."},
                    {"role": "assistant", "content": "I will treat Tuesday afternoon as tentative."},
                ],
                [
                    {"role": "user", "content": "Confirmed update: the deployment window is Wednesday morning."},
                    {"role": "assistant", "content": "Confirmed current deployment window: Wednesday morning."},
                ],
                [
                    {"role": "user", "content": "Also remind me to clean up the staging notes after launch."},
                    {"role": "assistant", "content": "Noted."},
                ],
            ],
        },
    ]
