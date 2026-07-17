#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "benchmarks" / "longmemeval" / "targeted_stress_seed.json"
SPLIT_PLAN_PATH = ROOT / "runs" / "longmemeval" / "targeted-stress" / "split-plan-stress-seed.json"


def _session(
    session_id: str,
    date: str,
    user: str,
    assistant: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "date": date,
        "messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
    }


def _case(
    *,
    case_id: str,
    stress_category: str,
    mechanism: str,
    question_type: str,
    question: str,
    question_date: str,
    answer: str,
    sessions: list[dict[str, Any]],
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
        "question_date": question_date,
        "answer": answer,
        "answer_session_ids": answer_session_ids,
        "gold_evidence": gold_evidence,
        "expected_failure_mode": expected_failure_mode,
        "haystack_session_ids": [session["session_id"] for session in sessions],
        "haystack_dates": [session["date"] for session in sessions],
        "haystack_sessions": [session["messages"] for session in sessions],
    }


def _cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    cases.append(
        _case(
            case_id="stress_ac_001_user_aside_overrides_assistant",
            stress_category="adversarial_contradictions",
            mechanism="source_authority_user_aside",
            question_type="knowledge-update",
            question="How many free Hilton night stays does the user currently have enough points for?",
            question_date="2024/03/20",
            answer="two free night stays",
            answer_session_ids=["stress_ac_001_s2"],
            gold_evidence=[
                "The user later states: I have enough Hilton points for two free night stays."
            ],
            expected_failure_mode=(
                "Reader trusts an earlier assistant-inferred single-night claim over a later user-stated aside."
            ),
            sessions=[
                _session(
                    "stress_ac_001_s1",
                    "2024/03/01",
                    "I'm thinking about using hotel points for a short Chicago trip.",
                    "With your current points, it sounds like you may have a single free night's stay available.",
                ),
                _session(
                    "stress_ac_001_s2",
                    "2024/03/15",
                    "For the Paris itinerary, keep the museum day light. Also, I have enough Hilton points for two free night stays, so lodging should not drive the schedule.",
                    "Understood. I will keep the museum day lighter and avoid over-weighting hotel cost in the Paris plan.",
                ),
                _session(
                    "stress_ac_001_s3",
                    "2024/03/18",
                    "Can you help me compare train options for Paris?",
                    "Yes. The main tradeoff is speed versus flexibility on departure time.",
                ),
            ],
        )
    )
    cases.append(
        _case(
            case_id="stress_ac_002_direct_user_correction",
            stress_category="adversarial_contradictions",
            mechanism="direct_user_correction",
            question_type="knowledge-update",
            question="What is the user's current maximum budget for the monitor?",
            question_date="2024/04/08",
            answer="$350",
            answer_session_ids=["stress_ac_002_s2"],
            gold_evidence=["The user corrects the budget from $500 to $350."],
            expected_failure_mode="Reader repeats the older budget because it is stated more prominently.",
            sessions=[
                _session(
                    "stress_ac_002_s1",
                    "2024/04/01",
                    "I might spend up to $500 on a 27 inch monitor.",
                    "That budget gives you room for higher refresh-rate options.",
                ),
                _session(
                    "stress_ac_002_s2",
                    "2024/04/04",
                    "Actually, my real monitor limit is $350 after rent came in higher than expected.",
                    "Got it. I will keep monitor options under $350.",
                ),
                _session(
                    "stress_ac_002_s3",
                    "2024/04/06",
                    "What desk width do I need for a 27 inch monitor?",
                    "A desk at least 42 inches wide is usually comfortable.",
                ),
            ],
        )
    )
    cases.append(
        _case(
            case_id="stress_ac_003_assistant_hallucinated_preference",
            stress_category="adversarial_contradictions",
            mechanism="assistant_inference_vs_user_constraint",
            question_type="single-session-preference",
            question="What dietary constraint should the dinner plan respect?",
            question_date="2024/05/12",
            answer="gluten-free",
            answer_session_ids=["stress_ac_003_s2"],
            gold_evidence=["The user says they are not vegetarian and need gluten-free options."],
            expected_failure_mode="Reader follows the assistant's earlier vegetarian inference instead of the user correction.",
            sessions=[
                _session(
                    "stress_ac_003_s1",
                    "2024/05/01",
                    "I'm planning a dinner where one guest asked for lighter food.",
                    "A vegetarian menu may be the safest default for a lighter dinner.",
                ),
                _session(
                    "stress_ac_003_s2",
                    "2024/05/07",
                    "Quick correction: nobody is vegetarian. The real constraint is gluten-free because my cousin has celiac.",
                    "Thanks for clarifying. I will make the dinner plan gluten-free rather than vegetarian.",
                ),
                _session(
                    "stress_ac_003_s3",
                    "2024/05/09",
                    "Can dessert still feel special?",
                    "Yes. A flourless chocolate cake or fruit pavlova can work well.",
                ),
            ],
        )
    )

    cases.append(
        _case(
            case_id="stress_chain_001_days_between_events",
            stress_category="cross_session_chains",
            mechanism="two_session_temporal_arithmetic",
            question_type="temporal-reasoning",
            question="How many days passed between finishing the novel and attending the author reading?",
            question_date="2024/02/25",
            answer="15 days",
            answer_session_ids=["stress_chain_001_s1", "stress_chain_001_s3"],
            gold_evidence=[
                "The user finished the novel on March 4.",
                "The author reading was on March 19.",
            ],
            expected_failure_mode="Graph traversal retrieves one date session but misses the second independent date session.",
            sessions=[
                _session(
                    "stress_chain_001_s1",
                    "2024/03/04",
                    "I finished The Glass Hotel tonight, March 4, and want to remember that date.",
                    "Noted. You finished The Glass Hotel on March 4.",
                ),
                _session(
                    "stress_chain_001_s2",
                    "2024/03/11",
                    "Can you suggest another literary novel?",
                    "You might like Station Eleven if you enjoyed Emily St. John Mandel's style.",
                ),
                _session(
                    "stress_chain_001_s3",
                    "2024/03/19",
                    "I attended the author reading on March 19 and took notes on the Q&A.",
                    "Recorded. The author reading happened on March 19.",
                ),
            ],
        )
    )
    cases.append(
        _case(
            case_id="stress_chain_002_inventory_update",
            stress_category="cross_session_chains",
            mechanism="multi_session_count_chain",
            question_type="multi-session",
            question="How many printer cartridges should the user have now?",
            question_date="2024/06/10",
            answer="7 cartridges",
            answer_session_ids=["stress_chain_002_s1", "stress_chain_002_s2", "stress_chain_002_s3"],
            gold_evidence=[
                "The user started with 6 cartridges.",
                "The user used 2 cartridges.",
                "The user bought 3 replacement cartridges.",
            ],
            expected_failure_mode="Retrieval gets a subset of sessions and answers from an incomplete arithmetic chain.",
            sessions=[
                _session(
                    "stress_chain_002_s1",
                    "2024/06/01",
                    "Inventory note: the supply closet has 6 printer cartridges left.",
                    "Noted: 6 printer cartridges in the supply closet.",
                ),
                _session(
                    "stress_chain_002_s2",
                    "2024/06/04",
                    "We used 2 printer cartridges during the client packet print run.",
                    "That would bring the cartridge count down by 2.",
                ),
                _session(
                    "stress_chain_002_s3",
                    "2024/06/08",
                    "I bought 3 replacement printer cartridges today.",
                    "Recorded: 3 replacement cartridges were added.",
                ),
                _session(
                    "stress_chain_002_s4",
                    "2024/06/09",
                    "The stapler inventory is fine.",
                    "Good. No action needed for staplers.",
                ),
            ],
        )
    )
    cases.append(
        _case(
            case_id="stress_chain_003_dependency_deadline",
            stress_category="cross_session_chains",
            mechanism="decision_dependency_chain",
            question_type="multi-session",
            question="What is the final date the user needs to submit the grant budget?",
            question_date="2024/07/18",
            answer="July 22",
            answer_session_ids=["stress_chain_003_s1", "stress_chain_003_s3"],
            gold_evidence=[
                "The budget deadline was July 19.",
                "The program officer extended the budget deadline to July 22.",
            ],
            expected_failure_mode="System retrieves the original deadline but misses the later dependency update.",
            sessions=[
                _session(
                    "stress_chain_003_s1",
                    "2024/07/10",
                    "The grant narrative is due July 17, and the budget is due July 19.",
                    "I'll track July 17 for the narrative and July 19 for the budget.",
                ),
                _session(
                    "stress_chain_003_s2",
                    "2024/07/12",
                    "Can you help outline the staffing section?",
                    "Yes. Start with roles, effort percentage, and budget justification.",
                ),
                _session(
                    "stress_chain_003_s3",
                    "2024/07/16",
                    "The program officer extended only the budget deadline to July 22; the narrative is still due July 17.",
                    "Noted. Budget deadline is now July 22, while the narrative deadline remains July 17.",
                ),
            ],
        )
    )

    cases.append(
        _case(
            case_id="stress_agent_001_decision_reason",
            stress_category="agent_decision_memory",
            mechanism="decision_plus_rationale",
            question_type="multi-session",
            question="Which database did the user decide to use, and why?",
            question_date="2024/08/03",
            answer="SQLite, because the tool needs local/offline operation and simple setup.",
            answer_session_ids=["stress_agent_001_s2"],
            gold_evidence=["The user decided on SQLite because local/offline operation mattered more than multi-user access."],
            expected_failure_mode="Reader recalls the decision but drops the rationale.",
            sessions=[
                _session(
                    "stress_agent_001_s1",
                    "2024/08/01",
                    "For this prototype, compare Postgres and SQLite.",
                    "Postgres is stronger for multi-user services, while SQLite is simpler for local tools.",
                ),
                _session(
                    "stress_agent_001_s2",
                    "2024/08/02",
                    "Let's use SQLite. The reason is local/offline operation and simple setup matter more than multi-user access for this prototype.",
                    "Decision recorded: use SQLite for local/offline operation and simple setup.",
                ),
                _session(
                    "stress_agent_001_s3",
                    "2024/08/02",
                    "Add a note that migrations can wait.",
                    "Noted. Migrations are not part of the first prototype.",
                ),
            ],
        )
    )
    cases.append(
        _case(
            case_id="stress_agent_002_exact_compound_term",
            stress_category="agent_decision_memory",
            mechanism="exact_compound_answer_fidelity",
            question_type="single-session-user",
            question="What exact feature name did the user settle on?",
            question_date="2024/09/05",
            answer="semantic conflict guardrail",
            answer_session_ids=["stress_agent_002_s2"],
            gold_evidence=["The user says the exact phrase should be semantic conflict guardrail."],
            expected_failure_mode="Reader drops a qualifier and answers with an incomplete compound phrase.",
            sessions=[
                _session(
                    "stress_agent_002_s1",
                    "2024/09/01",
                    "Maybe call the feature a conflict guardrail.",
                    "Conflict guardrail is concise and clear.",
                ),
                _session(
                    "stress_agent_002_s2",
                    "2024/09/04",
                    "Use the exact phrase semantic conflict guardrail. Do not shorten it to conflict guardrail in the README.",
                    "Got it. The exact feature name is semantic conflict guardrail.",
                ),
                _session(
                    "stress_agent_002_s3",
                    "2024/09/05",
                    "The README should be short.",
                    "I'll keep the README concise while preserving exact feature names.",
                ),
            ],
        )
    )
    cases.append(
        _case(
            case_id="stress_agent_003_constraint_update",
            stress_category="agent_decision_memory",
            mechanism="project_constraint_update",
            question_type="knowledge-update",
            question="What visibility should the repository use now?",
            question_date="2024/10/11",
            answer="private",
            answer_session_ids=["stress_agent_003_s2"],
            gold_evidence=["The user changed the repo visibility requirement from public to private."],
            expected_failure_mode="System repeats an older project constraint after a later user correction.",
            sessions=[
                _session(
                    "stress_agent_003_s1",
                    "2024/10/01",
                    "Create the repo as public so people can inspect the plan.",
                    "Understood. I will plan for a public repository.",
                ),
                _session(
                    "stress_agent_003_s2",
                    "2024/10/09",
                    "Change that: the repo needs to be private because the artifacts include dataset-derived traces.",
                    "Updated constraint: the repository should be private.",
                ),
                _session(
                    "stress_agent_003_s3",
                    "2024/10/10",
                    "Add Lisa once I have her GitHub username.",
                    "I will wait for Lisa's GitHub username before adding her as a collaborator.",
                ),
            ],
        )
    )

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
    split_refs = [
        {
            "case_id": case["case_id"],
            "category": case["stress_category"],
            "mechanism": case["mechanism"],
            "question_type": case["question_type"],
        }
        for case in cases
    ]
    split_plan = {
        "dataset_path": str(DATASET_PATH),
        "dataset_sha256": _sha256(DATASET_PATH),
        "seed": "hand-authored-targeted-stress-seed-v1",
        "case_count": len(cases),
        "splits": {"stress": split_refs},
        "notes": [
            "Supplementary stress cases are not official LongMemEval-S rows.",
            "Rows produced from this split must use suite=supplementary_stress and official_table_eligible=false.",
            "This seed set is a 9-case smoke suite. The planned full stress suite is 90 cases, 30 per category.",
        ],
    }
    SPLIT_PLAN_PATH.write_text(json.dumps(split_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(cases)} cases to {DATASET_PATH}")
    print(f"Wrote split plan to {SPLIT_PLAN_PATH}")
    print(f"Dataset SHA-256: {split_plan['dataset_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
