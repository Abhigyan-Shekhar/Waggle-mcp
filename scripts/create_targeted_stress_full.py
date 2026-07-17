#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "benchmarks" / "longmemeval" / "targeted_stress_v1.json"
SPLIT_PLAN_PATH = ROOT / "runs" / "longmemeval" / "targeted-stress" / "split-plan-stress-v1.json"


@dataclass(frozen=True)
class Session:
    session_id: str
    date: str
    user: str
    assistant: str


def _session(session_id: str, day: date, user: str, assistant: str) -> Session:
    return Session(session_id=session_id, date=day.strftime("%Y/%m/%d"), user=user, assistant=assistant)


def _case(
    *,
    case_id: str,
    stress_category: str,
    mechanism: str,
    question_type: str,
    question: str,
    question_date: date,
    answer: str,
    sessions: list[Session],
    answer_session_ids: list[str],
    gold_evidence: list[str],
    expected_failure_mode: str,
) -> dict[str, Any]:
    return {
        "question_id": case_id,
        "case_id": case_id,
        "stress_category": stress_category,
        "mechanism": mechanism,
        "question_type": question_type,
        "question": question,
        "question_date": question_date.strftime("%Y/%m/%d"),
        "answer": answer,
        "answer_session_ids": answer_session_ids,
        "gold_evidence": gold_evidence,
        "expected_failure_mode": expected_failure_mode,
        "haystack_session_ids": [session.session_id for session in sessions],
        "haystack_dates": [session.date for session in sessions],
        "haystack_sessions": [
            [
                {"role": "user", "content": session.user},
                {"role": "assistant", "content": session.assistant},
            ]
            for session in sessions
        ],
    }


def _adversarial_source_authority() -> list[dict[str, Any]]:
    rows = [
        ("hilton_free_nights", "Hilton free night stays", "one free night's stay", "two free night stays", "hotel points"),
        ("poster_size", "conference poster size", "A1", "A0", "conference printing"),
        ("tradein_credit", "laptop trade-in credit", "$250", "$420", "laptop upgrade"),
        ("parking_permits", "guest parking permits", "one permit", "three permits", "apartment move"),
        ("carryon_bags", "included carry-on bags", "one carry-on", "two carry-ons", "flight packing"),
        ("study_rooms", "reserved study rooms", "one room", "two rooms", "library booking"),
        ("meal_vouchers", "meal vouchers", "four vouchers", "six vouchers", "conference travel"),
        ("repair_tickets", "open repair tickets", "one ticket", "three tickets", "building maintenance"),
        ("trial_seats", "software trial seats", "five seats", "eight seats", "team onboarding"),
        ("poster_copies", "printed poster copies", "ten copies", "twelve copies", "event materials"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 3, 1)
    for index, (slug, label, old_value, correct_value, topic) in enumerate(rows, start=1):
        cid = f"stress_ac_sa_{index:02d}_{slug}"
        d = base + timedelta(days=index * 3)
        sessions = [
            _session(
                f"{cid}_s1",
                d,
                f"I am planning around {topic}. Can you estimate the current {label}?",
                f"Based on what you described, it sounds like the {label} may be {old_value}.",
            ),
            _session(
                f"{cid}_s2",
                d + timedelta(days=4),
                f"For the unrelated checklist, keep the schedule light. Also, the actual {label} is {correct_value}, so do not plan around {old_value}.",
                f"Understood. I will use {correct_value} as the current value for {label}.",
            ),
            _session(
                f"{cid}_s3",
                d + timedelta(days=5),
                f"Can you help with logistics for {topic}?",
                f"Yes. I will keep the logistics separate from the earlier rough estimate of {old_value}.",
            ),
            _session(
                f"{cid}_s4",
                d + timedelta(days=6),
                "Make the summary concise.",
                "I will keep the summary concise.",
            ),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="adversarial_contradictions",
                mechanism="source_authority_user_aside",
                question_type="knowledge-update",
                question=f"What is the user's current value for {label}?",
                question_date=d + timedelta(days=9),
                answer=correct_value,
                sessions=sessions,
                answer_session_ids=[f"{cid}_s2"],
                gold_evidence=[f"The user directly states that the actual {label} is {correct_value}."],
                expected_failure_mode="Reader trusts an earlier assistant-inferred value over a later direct user aside.",
            )
        )
    return cases


def _adversarial_direct_correction() -> list[dict[str, Any]]:
    rows = [
        ("monitor_budget", "monitor budget", "$500", "$350"),
        ("workshop_time", "workshop start time", "9 AM", "10:30 AM"),
        ("guest_count", "guest count", "18 people", "14 people"),
        ("subscription_tier", "subscription tier", "Pro", "Team"),
        ("invoice_due", "invoice due date", "June 12", "June 15"),
        ("photo_count", "photos to deliver", "40 photos", "28 photos"),
        ("shirt_size", "shirt size", "medium", "large"),
        ("rental_days", "rental duration", "two days", "four days"),
        ("donation_amount", "donation amount", "$75", "$120"),
        ("pickup_location", "pickup location", "North Station", "South Station"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 4, 1)
    for index, (slug, label, old_value, correct_value) in enumerate(rows, start=1):
        cid = f"stress_ac_dc_{index:02d}_{slug}"
        d = base + timedelta(days=index * 2)
        sessions = [
            _session(
                f"{cid}_s1",
                d,
                f"Use {old_value} as the working value for the {label}.",
                f"Noted. I will use {old_value} for the {label}.",
            ),
            _session(
                f"{cid}_s2",
                d + timedelta(days=2),
                f"Correction: the {label} is {correct_value}, not {old_value}. Please update that.",
                f"Updated. The {label} is now {correct_value}.",
            ),
            _session(
                f"{cid}_s3",
                d + timedelta(days=3),
                f"Can you draft a reminder about the {label}?",
                "Yes. I will use the corrected value in the reminder.",
            ),
            _session(
                f"{cid}_s4",
                d + timedelta(days=4),
                "Ignore formatting for now.",
                "Understood.",
            ),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="adversarial_contradictions",
                mechanism="direct_user_correction",
                question_type="knowledge-update",
                question=f"What is the current {label}?",
                question_date=d + timedelta(days=7),
                answer=correct_value,
                sessions=sessions,
                answer_session_ids=[f"{cid}_s2"],
                gold_evidence=[f"The user corrects {label} from {old_value} to {correct_value}."],
                expected_failure_mode="Reader repeats the older prominent value instead of the direct correction.",
            )
        )
    return cases


def _adversarial_assistant_inference() -> list[dict[str, Any]]:
    rows = [
        ("dinner_constraint", "dinner plan", "vegetarian", "gluten-free", "my cousin has celiac"),
        ("travel_style", "travel itinerary", "luxury hotel focused", "budget hostel focused", "I am saving cash"),
        ("workout_limit", "workout plan", "high-impact", "low-impact", "my knee is still healing"),
        ("gift_preference", "gift list", "tech gadgets", "handmade gifts", "they dislike electronics"),
        ("meeting_format", "meeting format", "in-person", "remote", "the team is distributed"),
        ("reading_level", "lesson plan", "advanced", "beginner-friendly", "the audience is new"),
        ("decor_style", "room decor", "minimalist", "warm maximalist", "I want colorful layers"),
        ("transport_mode", "commute plan", "driving", "public transit", "I sold my car"),
        ("meal_timing", "meal prep", "breakfast prep", "late dinner prep", "I work nights"),
        ("pet_policy", "apartment shortlist", "no pets", "cat-friendly", "I am bringing my cat"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 5, 1)
    for index, (slug, label, inferred, correct, reason) in enumerate(rows, start=1):
        cid = f"stress_ac_ai_{index:02d}_{slug}"
        d = base + timedelta(days=index * 2)
        sessions = [
            _session(
                f"{cid}_s1",
                d,
                f"I need help with a {label}.",
                f"A {inferred} approach may be a safe default.",
            ),
            _session(
                f"{cid}_s2",
                d + timedelta(days=3),
                f"Quick correction: the right constraint for the {label} is {correct} because {reason}.",
                f"Thanks for clarifying. I will use {correct} for the {label}.",
            ),
            _session(
                f"{cid}_s3",
                d + timedelta(days=4),
                f"Can the {label} still feel polished?",
                "Yes. We can keep it polished while respecting the corrected constraint.",
            ),
            _session(
                f"{cid}_s4",
                d + timedelta(days=5),
                f"Give me three generic options for the {label}.",
                f"Here are three options, but the corrected constraint remains {correct}.",
            ),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="adversarial_contradictions",
                mechanism="assistant_inference_vs_user_constraint",
                question_type="single-session-preference",
                question=f"What constraint should the {label} respect?",
                question_date=d + timedelta(days=8),
                answer=correct,
                sessions=sessions,
                answer_session_ids=[f"{cid}_s2"],
                gold_evidence=[f"The user says the right constraint is {correct} because {reason}."],
                expected_failure_mode="Reader follows an assistant-inferred default instead of the user's explicit constraint.",
            )
        )
    return cases


def _cross_temporal() -> list[dict[str, Any]]:
    rows = [
        ("novel_reading", "finishing the novel", "author reading", date(2024, 3, 4), date(2024, 3, 19)),
        ("prototype_demo", "finishing the prototype", "demo meeting", date(2024, 4, 2), date(2024, 4, 11)),
        ("planting_harvest", "planting the basil", "first harvest", date(2024, 5, 6), date(2024, 5, 27)),
        ("ticket_purchase", "buying the ticket", "concert", date(2024, 6, 3), date(2024, 6, 22)),
        ("draft_submit", "submitting the draft", "receiving feedback", date(2024, 7, 1), date(2024, 7, 13)),
        ("bike_repair", "dropping off the bike", "picking it up", date(2024, 8, 5), date(2024, 8, 17)),
        ("course_start", "starting the course", "taking the final quiz", date(2024, 9, 9), date(2024, 10, 1)),
        ("visa_apply", "submitting the visa form", "getting approval", date(2024, 10, 3), date(2024, 10, 18)),
        ("order_ship", "placing the furniture order", "delivery", date(2024, 11, 4), date(2024, 11, 26)),
        ("training_begin", "beginning training", "race day", date(2024, 12, 2), date(2024, 12, 29)),
    ]
    cases: list[dict[str, Any]] = []
    for index, (slug, first_event, second_event, first_day, second_day) in enumerate(rows, start=1):
        cid = f"stress_chain_tr_{index:02d}_{slug}"
        answer_days = (second_day - first_day).days
        sessions = [
            _session(
                f"{cid}_s1",
                first_day,
                f"I want to remember that I completed {first_event} on {first_day.strftime('%B %-d')}.",
                f"Noted. {first_event.capitalize()} happened on {first_day.strftime('%B %-d')}.",
            ),
            _session(
                f"{cid}_s2",
                first_day + timedelta(days=4),
                f"Can you help me plan around {first_event}?",
                "Yes. I can help with the planning details.",
            ),
            _session(
                f"{cid}_s3",
                second_day,
                f"Today was {second_event} on {second_day.strftime('%B %-d')}.",
                f"Recorded. {second_event.capitalize()} happened on {second_day.strftime('%B %-d')}.",
            ),
            _session(
                f"{cid}_s4",
                second_day + timedelta(days=1),
                "Summarize the timeline briefly.",
                "The timeline has two separate dated milestones.",
            ),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="cross_session_chains",
                mechanism="two_session_temporal_arithmetic",
                question_type="temporal-reasoning",
                question=f"How many days passed between {first_event} and {second_event}?",
                question_date=second_day + timedelta(days=3),
                answer=f"{answer_days} days",
                sessions=sessions,
                answer_session_ids=[f"{cid}_s1", f"{cid}_s3"],
                gold_evidence=[
                    f"{first_event} happened on {first_day.strftime('%B %-d')}.",
                    f"{second_event} happened on {second_day.strftime('%B %-d')}.",
                ],
                expected_failure_mode="Graph traversal retrieves one dated session but misses the second dated session.",
            )
        )
    return cases


def _cross_inventory() -> list[dict[str, Any]]:
    rows = [
        ("printer_cartridges", "printer cartridges", 6, 2, 3),
        ("notebook_packs", "notebook packs", 12, 5, 4),
        ("coffee_bags", "coffee bags", 9, 3, 6),
        ("raffle_tickets", "raffle tickets", 30, 8, 12),
        ("canvas_panels", "canvas panels", 18, 7, 5),
        ("name_badges", "name badges", 45, 16, 10),
        ("seed_packets", "seed packets", 20, 9, 7),
        ("usb_drives", "USB drives", 14, 4, 8),
        ("postcard_sets", "postcard sets", 25, 10, 6),
        ("lab_vials", "lab vials", 60, 22, 15),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 6, 1)
    for index, (slug, item, start, used, added) in enumerate(rows, start=1):
        cid = f"stress_chain_ct_{index:02d}_{slug}"
        d = base + timedelta(days=index * 3)
        answer_count = start - used + added
        sessions = [
            _session(f"{cid}_s1", d, f"Inventory note: we have {start} {item} in storage.", f"Recorded: {start} {item}."),
            _session(f"{cid}_s2", d + timedelta(days=2), f"We used {used} {item} for the event.", f"That reduces the count by {used}."),
            _session(f"{cid}_s3", d + timedelta(days=5), f"I bought {added} more {item} today.", f"Recorded: {added} {item} added."),
            _session(f"{cid}_s4", d + timedelta(days=6), f"The labels for the {item} are in a separate drawer.", "Noted."),
            _session(f"{cid}_s5", d + timedelta(days=7), "The stapler inventory is unrelated but fine.", "No action needed."),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="cross_session_chains",
                mechanism="multi_session_count_chain",
                question_type="multi-session",
                question=f"How many {item} should the user have now?",
                question_date=d + timedelta(days=9),
                answer=f"{answer_count} {item}",
                sessions=sessions,
                answer_session_ids=[f"{cid}_s1", f"{cid}_s2", f"{cid}_s3"],
                gold_evidence=[f"Started with {start}.", f"Used {used}.", f"Added {added}."],
                expected_failure_mode="Retrieval returns only part of the arithmetic chain.",
            )
        )
    return cases


def _cross_dependency() -> list[dict[str, Any]]:
    rows = [
        ("grant_budget", "grant budget", "July 19", "July 22", "program officer"),
        ("poster_upload", "poster upload", "August 8", "August 11", "conference chair"),
        ("venue_deposit", "venue deposit", "May 5", "May 9", "venue manager"),
        ("app_release", "app release", "September 3", "September 6", "QA lead"),
        ("print_order", "print order", "October 14", "October 17", "print shop"),
        ("survey_close", "survey close date", "November 1", "November 4", "research coordinator"),
        ("abstract_revision", "abstract revision", "June 21", "June 24", "track chair"),
        ("invoice_submission", "invoice submission", "December 10", "December 13", "finance team"),
        ("portfolio_review", "portfolio review", "April 12", "April 15", "review panel"),
        ("training_roster", "training roster", "March 18", "March 20", "operations lead"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 7, 1)
    for index, (slug, deliverable, old_date, new_date, authority) in enumerate(rows, start=1):
        cid = f"stress_chain_dep_{index:02d}_{slug}"
        d = base + timedelta(days=index * 4)
        sessions = [
            _session(f"{cid}_s1", d, f"The {deliverable} is due {old_date}.", f"I will track {old_date} for the {deliverable}."),
            _session(f"{cid}_s2", d + timedelta(days=1), f"Help outline the supporting notes for the {deliverable}.", "Yes. Start with status, owner, and dependencies."),
            _session(f"{cid}_s3", d + timedelta(days=3), f"The {authority} extended only the {deliverable} deadline to {new_date}.", f"Updated. The {deliverable} deadline is now {new_date}."),
            _session(f"{cid}_s4", d + timedelta(days=4), "Make sure unrelated deadlines stay unchanged.", "Understood."),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="cross_session_chains",
                mechanism="decision_dependency_chain",
                question_type="multi-session",
                question=f"What is the final deadline for the {deliverable}?",
                question_date=d + timedelta(days=6),
                answer=new_date,
                sessions=sessions,
                answer_session_ids=[f"{cid}_s1", f"{cid}_s3"],
                gold_evidence=[f"The original deadline was {old_date}.", f"The later extension moved it to {new_date}."],
                expected_failure_mode="System retrieves the original dependency but misses the later update session.",
            )
        )
    return cases


def _agent_decision_reason() -> list[dict[str, Any]]:
    rows = [
        ("sqlite", "SQLite", "local/offline operation and simple setup", "Postgres"),
        ("playwright", "Playwright", "cross-browser traces and reliable screenshots", "Selenium"),
        ("markdown", "Markdown files", "plain-text review and easy diffs", "Notion pages"),
        ("duckdb", "DuckDB", "fast local analytics without a server", "BigQuery"),
        ("ruff", "Ruff", "one fast tool for linting and formatting", "Flake8 plus Black"),
        ("github_actions", "GitHub Actions", "it already runs in the repo", "CircleCI"),
        ("jsonl", "JSONL", "append-only logs and per-row provenance", "CSV"),
        ("mini_lm", "all-MiniLM-L6-v2", "embedding parity with the flat baseline", "OpenAI embeddings"),
        ("private_repo", "a private repository", "dataset-derived traces should not be public", "a public repository"),
        ("heuristic_gate", "a deterministic preflight gate", "runs should fail before spending API budget", "manual checking"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 8, 1)
    for index, (slug, decision, reason, alternative) in enumerate(rows, start=1):
        cid = f"stress_agent_dr_{index:02d}_{slug}"
        d = base + timedelta(days=index * 2)
        sessions = [
            _session(f"{cid}_s1", d, f"Compare {decision} and {alternative} for this project.", f"{alternative} has advantages, but {decision} may fit depending on constraints."),
            _session(f"{cid}_s2", d + timedelta(days=1), f"Decision: use {decision}. The reason is {reason}.", f"Recorded: use {decision} because {reason}."),
            _session(f"{cid}_s3", d + timedelta(days=2), "Do not over-explain this in the README.", "Understood. I will keep it concise."),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="agent_decision_memory",
                mechanism="decision_plus_rationale",
                question_type="multi-session",
                question=f"What did the user decide to use, and why?",
                question_date=d + timedelta(days=5),
                answer=f"{decision}, because {reason}.",
                sessions=sessions,
                answer_session_ids=[f"{cid}_s2"],
                gold_evidence=[f"The user decided to use {decision} because {reason}."],
                expected_failure_mode="Reader recalls the decision but drops the rationale.",
            )
        )
    return cases


def _agent_exact_terms() -> list[dict[str, Any]]:
    rows = [
        ("semantic_conflict_guardrail", "semantic conflict guardrail", "conflict guardrail"),
        ("source_authority_resolver", "source authority resolver", "authority resolver"),
        ("context_budget_arbiter", "context budget arbiter", "budget arbiter"),
        ("narrative_anchor_preserver", "narrative anchor preserver", "anchor preserver"),
        ("session_coverage_safety_net", "session coverage safety net", "coverage safety net"),
        ("evidence_first_answerer", "evidence-first answerer", "answerer"),
        ("gold_support_audit_log", "gold support audit log", "support log"),
        ("typed_temporal_graph", "typed temporal graph", "temporal graph"),
        ("exact_phrase_guard", "exact phrase guard", "phrase guard"),
        ("blind_slice_freezer", "blind slice freezer", "slice freezer"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 9, 1)
    for index, (slug, exact, shortened) in enumerate(rows, start=1):
        cid = f"stress_agent_et_{index:02d}_{slug}"
        d = base + timedelta(days=index * 2)
        sessions = [
            _session(f"{cid}_s1", d, f"Maybe call the feature {shortened}.", f"{shortened} is concise."),
            _session(f"{cid}_s2", d + timedelta(days=1), f"Use the exact phrase {exact}. Do not shorten it to {shortened}.", f"Got it. The exact phrase is {exact}."),
            _session(f"{cid}_s3", d + timedelta(days=2), "The README can be short, but exact names matter.", "I will preserve exact names."),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="agent_decision_memory",
                mechanism="exact_compound_answer_fidelity",
                question_type="single-session-user",
                question="What exact feature name did the user settle on?",
                question_date=d + timedelta(days=4),
                answer=exact,
                sessions=sessions,
                answer_session_ids=[f"{cid}_s2"],
                gold_evidence=[f"The user says the exact phrase is {exact}."],
                expected_failure_mode="Reader drops a qualifier and returns an incomplete compound term.",
            )
        )
    return cases


def _agent_constraint_update() -> list[dict[str, Any]]:
    rows = [
        ("repo_visibility", "repository visibility", "public", "private", "dataset-derived traces should not be public"),
        ("license_scope", "release license", "MIT", "Apache-2.0", "patent grant language matters"),
        ("run_policy", "run policy", "rerun inspected cases", "draw a fresh slice", "avoid tuning on spent rows"),
        ("budget_cap", "paid-run cap", "$200", "$180", "Lisa's $20 buffer should stay untouched"),
        ("judge_model", "judge model policy", "same model is fine", "use a second judge for disagreements", "same-model judging already produced errors"),
        ("stress_reporting", "stress reporting policy", "merge with LongMemEval-S", "report separately", "stress rows are supplementary"),
        ("qwen_full_context", "Qwen full-context row", "pre-approved", "not pre-approved", "mock token totals must fit the cap first"),
        ("user_studies", "user-study scope", "include now", "out of scope", "transparency studies need separate ethics design"),
        ("repo_collab", "Lisa collaborator action", "add immediately", "wait for her GitHub username", "the username is not known yet"),
        ("dataset_download", "LongMemEval-M download", "download now", "defer in favor of stress tests", "Lisa chose targeted stress tests"),
    ]
    cases: list[dict[str, Any]] = []
    base = date(2024, 10, 1)
    for index, (slug, label, old_value, correct_value, reason) in enumerate(rows, start=1):
        cid = f"stress_agent_cu_{index:02d}_{slug}"
        d = base + timedelta(days=index * 2)
        sessions = [
            _session(f"{cid}_s1", d, f"Initial constraint: {label} should be {old_value}.", f"Recorded: {label} is {old_value}."),
            _session(f"{cid}_s2", d + timedelta(days=1), f"Change that: {label} should be {correct_value} because {reason}.", f"Updated: {label} is {correct_value}."),
            _session(f"{cid}_s3", d + timedelta(days=2), "Keep the provenance note clear.", "I will keep the reason attached to the updated constraint."),
        ]
        cases.append(
            _case(
                case_id=cid,
                stress_category="agent_decision_memory",
                mechanism="project_constraint_update",
                question_type="knowledge-update",
                question=f"What is the current {label}?",
                question_date=d + timedelta(days=4),
                answer=correct_value,
                sessions=sessions,
                answer_session_ids=[f"{cid}_s2"],
                gold_evidence=[f"The user updates {label} from {old_value} to {correct_value} because {reason}."],
                expected_failure_mode="System repeats an older project constraint after a later correction.",
            )
        )
    return cases


def _cases() -> list[dict[str, Any]]:
    cases = [
        *_adversarial_source_authority(),
        *_adversarial_direct_correction(),
        *_adversarial_assistant_inference(),
        *_cross_temporal(),
        *_cross_inventory(),
        *_cross_dependency(),
        *_agent_decision_reason(),
        *_agent_exact_terms(),
        *_agent_constraint_update(),
    ]
    ids = [case["case_id"] for case in cases]
    if len(cases) != 90:
        raise RuntimeError(f"expected 90 cases, generated {len(cases)}")
    if len(ids) != len(set(ids)):
        raise RuntimeError("generated duplicate case IDs")
    return cases


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    cases = _cases()
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPLIT_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATASET_PATH.write_text(json.dumps(cases, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    split_plan = {
        "dataset_path": str(DATASET_PATH),
        "dataset_sha256": _sha256(DATASET_PATH),
        "seed": "deterministic-targeted-stress-v1",
        "case_count": len(cases),
        "splits": {
            "stress": [
                {
                    "case_id": case["case_id"],
                    "category": case["stress_category"],
                    "mechanism": case["mechanism"],
                    "question_type": case["question_type"],
                }
                for case in cases
            ]
        },
        "notes": [
            "Supplementary stress cases are not official LongMemEval-S rows.",
            "Rows produced from this split must use suite=supplementary_stress and official_table_eligible=false.",
            "This v1 suite contains 90 cases, 30 per stress category.",
        ],
    }
    SPLIT_PLAN_PATH.write_text(json.dumps(split_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(cases)} cases to {DATASET_PATH}")
    print(f"Wrote split plan to {SPLIT_PLAN_PATH}")
    print(f"Dataset SHA-256: {split_plan['dataset_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
