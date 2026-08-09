from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from waggle.context_compiler import CompactEvidenceCompiler
from waggle.retrieval.contracts import EvidenceType, EvidenceValidator
from waggle.retrieval.assembler import EvidenceAssembler
from waggle.retrieval.planner import DeterministicQueryPlanner, Operation, QueryType
from waggle.retrieval.temporal_slots import TemporalSlotRetriever


def hit(
    content: str,
    *,
    score: float = 1.0,
    source_id: str = "s1",
    observed_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        score=score,
        source="node",
        node_ids=[source_id],
        turn_pair_id=source_id,
        observed_at=observed_at,
    )


def test_planner_creates_independent_percentage_slots() -> None:
    plan = DeterministicQueryPlanner().plan(
        "What percentage of the 100 leadership positions are occupied by women?"
    )
    assert plan.query_type == QueryType.AGGREGATION
    assert plan.operation == Operation.PERCENTAGE
    assert [slot.name for slot in plan.slots] == ["numerator", "denominator"]


def test_planner_routes_current_historical_sum_and_enumeration() -> None:
    planner = DeterministicQueryPlanner()
    assert planner.plan("Where do I currently live?").query_type == QueryType.CURRENT_STATE
    assert planner.plan("Where did I live before Boston?").query_type == QueryType.HISTORICAL_STATE
    assert planner.plan("How much feed did I buy altogether?").operation == Operation.SUM
    assert planner.plan("List all the model kits I bought.").operation == Operation.SET_UNION
    assert planner.plan("How many MCU films did I watch in the last 3 months?").query_type == QueryType.CURRENT_STATE


def test_temporal_plan_uses_explicit_reference_date_without_retrieving_an_end_slot() -> None:
    plan = DeterministicQueryPlanner().plan(
        "How many days ago did I attend the event?",
        reference_date="2022/04/04 (Mon) 10:00",
    )

    assert [slot.name for slot in plan.slots] == ["start_date"]
    assert "2022/04/04" in plan.query


def test_planner_creates_independent_slots_for_derived_arrival_time() -> None:
    plan = DeterministicQueryPlanner().plan("What time did I reach the clinic on Monday?")

    assert plan.query_type == QueryType.GENERAL_MULTI_HOP
    assert plan.operation == Operation.TIME_OFFSET
    assert [slot.name for slot in plan.slots] == ["departure_time", "travel_duration"]
    assert plan.diagnostics["time_offset_direction"] == "add"


def test_clock_offset_combines_cross_session_departure_and_duration() -> None:
    plan = DeterministicQueryPlanner().plan("What time did I reach the clinic on Monday?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "departure_time": [
                hit("I left home at 7 AM on Monday for my doctor's appointment.", source_id="departure"),
                hit("My unrelated Monday book club starts at 7 PM.", score=1.2, source_id="distractor"),
            ],
            "travel_duration": [
                hit("It took me two hours to get from home to the clinic last time.", source_id="duration")
            ],
        },
    )

    assert assembled.calculation is not None
    assert assembled.calculation.expression == "7:00 AM + 2 hours = 9:00 AM"
    assert assembled.calculation.result == "9:00 AM"
    assert assembled.per_slot["departure_time"][0].turn_pair_id == "departure"


def test_clock_offset_supports_reverse_departure_calculation() -> None:
    plan = DeterministicQueryPlanner().plan(
        "What time did I leave home if I reached the clinic at 9 AM after a two-hour trip?"
    )
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "arrival_time": [hit("I reached the clinic at 9 AM.", source_id="arrival")],
            "travel_duration": [hit("The trip took two hours.", source_id="duration")],
        },
    )

    assert plan.diagnostics["time_offset_direction"] == "subtract"
    assert assembled.calculation is not None
    assert assembled.calculation.expression == "9:00 AM - 2 hours = 7:00 AM"


def test_clock_offset_remains_incomplete_without_both_operands() -> None:
    plan = DeterministicQueryPlanner().plan("What time did I reach the clinic on Monday?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "departure_time": [hit("I left home at 7 AM on Monday.", source_id="departure")],
            "travel_duration": [],
        },
    )

    assert assembled.calculation is None
    assert "travel_duration" in assembled.missing_slots


def test_clock_shape_must_survive_evidence_focusing() -> None:
    plan = DeterministicQueryPlanner().plan("What time did I reach the clinic on Monday?")
    oversized_distractor = (
        "user: I wrote a long unrelated travel diary about clinics and Monday plans. "
        + "travel " * 250
        + "assistant: A separate generic schedule starts at 10 AM."
    )
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "departure_time": [
                hit(oversized_distractor, score=2.0, source_id="distractor"),
                hit("user: I left home at 7 AM on Monday for my doctor's appointment.", source_id="departure"),
            ],
            "travel_duration": [
                hit("user: It took me two hours to get from home to the clinic.", source_id="duration")
            ],
        },
    )

    assert assembled.calculation is not None
    assert assembled.calculation.result == "9:00 AM"
    assert assembled.per_slot["departure_time"][0].turn_pair_id == "departure"


def test_percentage_is_calculated_only_with_complete_unambiguous_slots() -> None:
    plan = DeterministicQueryPlanner().plan("What percentage of leadership positions are occupied by women?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "numerator": [hit("Women occupy 20 leadership positions.", source_id="n")],
            "denominator": [hit("There are 100 leadership positions in total.", source_id="d")],
        },
    )
    assert assembled.calculation is not None
    assert assembled.calculation.result == 20
    assert assembled.calculation.unit == "%"

    ambiguous = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "numerator": [hit("Estimates mention both 18 and 20 positions.", source_id="n2")],
            "denominator": [hit("There are 100 positions.", source_id="d2")],
        },
    )
    assert ambiguous.calculation is None


def test_same_source_can_cover_multiple_required_slots_without_duplicate_rendering() -> None:
    plan = DeterministicQueryPlanner().plan("What percentage of leadership positions are occupied by women?")
    shared = hit("Women occupy 20 roles in an organization with 100 leadership positions.", source_id="shared")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={"numerator": [shared], "denominator": [shared]},
    )
    assert assembled.missing_slots == []
    compiled = CompactEvidenceCompiler().compile(assembled)
    assert compiled.text.count(shared.content) == 1


def test_sum_collects_distinct_event_operands() -> None:
    plan = DeterministicQueryPlanner().plan("How much feed did I buy altogether?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "events": [
                hit("Bought 50 pounds of feed.", source_id="purchase-1"),
                hit("Later bought 20 pounds of feed.", score=0.9, source_id="purchase-2"),
                hit("Bought 50 pounds of feed.", score=0.8, source_id="duplicate"),
            ]
        },
    )
    assert assembled.calculation is not None
    assert assembled.calculation.result == 70
    assert assembled.calculation.operands == (50.0, 20.0)
    assert len(assembled.dropped_duplicates) == 1


def test_numeric_extraction_ignores_document_date_and_prefers_units() -> None:
    plan = DeterministicQueryPlanner().plan("What is the total feed weight altogether?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "events": [
                hit("user: [documentDate: 2023/05/22 (Mon) 14:05] I bought a 50-pound batch of feed.", source_id="one"),
                hit("user: [documentDate: 2023/05/27 (Sat) 16:34] I bought 20 pounds of scratch grains.", source_id="two"),
            ]
        },
    )

    assert assembled.calculation is not None
    assert assembled.calculation.result == 70


def test_weight_sum_ignores_price_only_event_candidates() -> None:
    plan = DeterministicQueryPlanner().plan("What is the total feed weight altogether?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "events": [
                hit("I bought a 50-pound batch of feed for $120.", source_id="one"),
                hit("I also bought 20 pounds of scratch grains.", source_id="two"),
                hit("I received a 10% discount on the purchase.", source_id="discount"),
            ]
        },
    )

    assert assembled.calculation is not None
    assert set(assembled.calculation.operands) == {20.0, 50.0}
    assert assembled.calculation.result == 70


def test_focused_content_prefers_authoritative_user_price_over_assistant_ranges() -> None:
    content = (
        "user: I learned that the Acme shoes originally retailed for $500. "
        "assistant: Sustainable brands have broad regular price ranges: "
        "$20-$100, $10-$50, and $50-$200."
    )

    focused = EvidenceAssembler._focused_content(
        "How much did I save on the Acme shoes?",
        "original retail price Acme shoes",
        content,
        slot_name="original_value",
    )

    assert focused.startswith("user:")
    assert "$500" in focused
    assert "$20-$100" not in focused


def test_comparison_uses_cue_bound_prices_amid_unrelated_ranges() -> None:
    plan = DeterministicQueryPlanner().plan("How much did I save on the Acme shoes?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "original_value": [
                hit(
                    "The Acme shoes originally retailed for $500. "
                    "Other products range from $20-$100 and $50-$200.",
                    source_id="original",
                )
            ],
            "current_value": [
                hit("I got the Acme shoes at an outlet for $200.", source_id="current")
            ],
        },
    )

    assert assembled.calculation is not None
    assert assembled.calculation.expression == "500 - 200 = 300"


def test_temporal_difference_normalizes_explicit_dates() -> None:
    plan = DeterministicQueryPlanner().plan("How many days elapsed between the event and the question?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "start_date": [hit("Source date: 2022-03-09", source_id="start")],
            "end_date": [hit("Question date: 2022-04-04", source_id="end")],
        },
    )
    assert assembled.calculation is not None
    assert assembled.calculation.result == 26
    assert assembled.calculation.unit == "days"


def test_temporal_difference_resolves_today_against_source_not_question_date() -> None:
    plan = DeterministicQueryPlanner().plan(
        "As of 2022-04-04, how many days ago was the networking event?"
    )
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "start_date": [
                hit(
                    'The user said "I attended the networking event today."',
                    source_id="event",
                    observed_at=datetime(2022, 3, 9, tzinfo=UTC),
                )
            ],
            "end_date": [],
        },
    )
    assert assembled.calculation is not None
    assert assembled.calculation.result == 26


def test_compiler_places_verified_evidence_and_calculation_first() -> None:
    plan = DeterministicQueryPlanner().plan("How much feed did I buy altogether?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "events": [
                hit("Bought 50 pounds of feed.", source_id="one"),
                hit("Bought 20 pounds of feed.", source_id="two"),
            ]
        },
    )
    compiled = CompactEvidenceCompiler().compile(assembled, max_tokens=200)
    assert "QUESTION TYPE: AGGREGATION" in compiled.text
    assert "VERIFIED COMPUTATION" in compiled.text
    assert "50 + 20 = 70" in compiled.text
    assert compiled.estimated_tokens <= 200


def test_missing_required_slot_is_explicit_and_blocks_calculation() -> None:
    plan = DeterministicQueryPlanner().plan("How much did I save compared with the original price?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={"current_value": [hit("The sale price was 200 dollars.")]},
    )
    compiled = CompactEvidenceCompiler().compile(assembled)
    assert assembled.calculation is None
    assert assembled.missing_slots == ["original_value"]
    assert "MISSING REQUIRED EVIDENCE" in compiled.text


def test_planner_builds_structural_contracts_without_case_vocabulary() -> None:
    planner = DeterministicQueryPlanner()

    indexed = planner.plan("What was the 12th item in the deployment checklist you gave me?")
    assert indexed.slots[0].evidence_type == EvidenceType.LIST_ITEM
    assert indexed.slots[0].target_index == 12
    assert indexed.slots[0].required_role == "assistant"

    table = planner.plan("Which shift was Priya assigned on Tuesday?")
    assert table.slots[0].evidence_type == EvidenceType.TABLE_CELL
    assert table.slots[0].target_key == "Priya"
    assert table.slots[0].row_key == "Tuesday"

    recommendation = planner.plan("Which backend languages did you recommend I learn?")
    assert recommendation.slots[0].evidence_type == EvidenceType.ASSISTANT_ANSWER
    assert recommendation.slots[0].required_role == "assistant"

    mentioned = planner.plan("You mentioned several authentication methods. Which ones were they?")
    assert mentioned.slots[0].evidence_type == EvidenceType.ASSISTANT_ANSWER
    assert mentioned.slots[0].required_role == "assistant"


def test_planner_routes_generic_difference_active_set_and_preference_contracts() -> None:
    planner = DeterministicQueryPlanner()
    difference = planner.plan("How many minutes did I exceed my target time by?")
    assert difference.operation == Operation.DIFFERENCE
    assert [slot.name for slot in difference.slots] == ["reference_value", "observed_value"]

    active_set = planner.plan("How many newsletter subscriptions do I currently have?")
    assert active_set.operation == Operation.COUNT
    assert [slot.name for slot in active_set.slots] == ["active_members", "removed_members"]

    preference = planner.plan("I am stuck with pottery. Any ideas for finding inspiration?")
    assert preference.query_type == QueryType.PREFERENCE
    assert preference.slots[0].evidence_type == EvidenceType.PREFERENCE


def test_indexed_list_atom_preserves_index_value_and_assistant_role() -> None:
    plan = DeterministicQueryPlanner().plan(
        "What was the 12th item in the deployment checklist you gave me?"
    )
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "indexed_item": [
                hit(
                    "assistant: Deployment checklist:\n"
                    "11. Verify backups\n"
                    "12. Rotate credentials\n"
                    "13. Notify stakeholders",
                )
            ]
        },
    )

    item = assembled.per_slot["indexed_item"][0]
    assert item.evidence_type == EvidenceType.LIST_ITEM
    assert item.source_role == "assistant"
    assert item.structure["list_index"] == 12
    assert item.structure["value"] == "Rotate credentials"
    assert item.structure["list_name"] == "Deployment checklist"
    assert [entry["list_index"] for entry in item.structure["neighbours"]] == [11, 13]
    assert EvidenceValidator().validate(assembled) == []
    compiled = CompactEvidenceCompiler().compile(assembled)
    assert "ITEM 12: Rotate credentials" in compiled.text


def test_table_atom_never_emits_cell_without_row_and_column_labels() -> None:
    plan = DeterministicQueryPlanner().plan("Which shift was Priya assigned on Tuesday?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "table_cell": [
                hit(
                    "assistant: | Day | 8 am - 4 pm | 4 pm - 12 am |\n"
                    "| --- | --- | --- |\n"
                    "| Monday | Lee | Omar |\n"
                    "| Tuesday | Priya | Lee |"
                )
            ]
        },
    )

    item = assembled.per_slot["table_cell"][0]
    assert item.structure == {
        "row_key": "Tuesday",
        "column_name": "8 am - 4 pm",
        "cell_value": "Priya",
    }
    compiled = CompactEvidenceCompiler().compile(assembled)
    assert "ROW: Tuesday | COLUMN: 8 am - 4 pm | VALUE: Priya" in compiled.text


def test_active_set_count_applies_additions_and_cancellations() -> None:
    plan = DeterministicQueryPlanner().plan("How many newsletter subscriptions do I currently have?")
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "active_members": [
                hit("user: I subscribe to The Weekly Review.", source_id="weekly"),
                hit("user: I also subscribe to Science Monthly.", source_id="science"),
                hit("user: I used to subscribe to Finance Daily.", source_id="finance-old"),
            ],
            "removed_members": [
                hit("user: I canceled my Finance Daily subscription.", source_id="finance-cancel"),
            ],
        },
    )

    assert assembled.calculation is not None
    assert assembled.calculation.result == 2
    assert set(assembled.active_set_members) == {"The Weekly Review", "Science Monthly"}
    assembled.expanded_slots.add("active_members")
    assert EvidenceValidator().validate(assembled) == []


def test_preference_atom_labels_explicit_history_and_scope() -> None:
    plan = DeterministicQueryPlanner().plan(
        "I am stuck with pottery. Any ideas for finding inspiration?"
    )
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={
            "preferences": [
                hit("user: I prefer nature-inspired pottery and enjoy carving leaf patterns.")
            ]
        },
    )
    item = assembled.per_slot["preferences"][0]
    assert item.evidence_type == EvidenceType.PREFERENCE
    assert item.structure["grounding"] == "explicit"
    assert item.structure["scope"] == "pottery"
    assert "EXPLICIT PREFERENCE" in CompactEvidenceCompiler().compile(assembled).text


def test_preference_contract_rejects_unrelated_domain_evidence() -> None:
    plan = DeterministicQueryPlanner().plan(
        "I am stuck with pottery. Any ideas for finding inspiration?"
    )
    assembled = EvidenceAssembler().select(
        plan=plan,
        hits_by_slot={"preferences": [hit("user: I enjoy marathon training and trail running.")]},
    )

    assert "incompatible_preference_scope" in {
        issue.code for issue in EvidenceValidator().validate(assembled)
    }


def test_current_set_requires_one_bounded_enumeration_pass() -> None:
    class FakeRetriever:
        def __init__(self) -> None:
            self.calls = 0

        def retrieve_debug(self, *, top_k: int, **_: object) -> dict[str, object]:
            self.calls += 1
            if top_k == 20:
                return {"hits": [hit("user: I subscribe to The Weekly Review.", source_id="weekly")]}
            return {
                "hits": [
                    hit("user: I subscribe to The Weekly Review.", source_id="weekly"),
                    hit("user: I subscribe to Science Monthly.", source_id="science"),
                ]
            }

    class FakeGraph:
        def __init__(self) -> None:
            self.retriever = FakeRetriever()

        def hybrid_retriever(self) -> FakeRetriever:
            return self.retriever

    result = TemporalSlotRetriever(FakeGraph()).retrieve(
        query="How many newsletter subscriptions do I currently have?",
        max_context_tokens=500,
    )

    assert result.fallback_used is True
    assert result.validation_issues == ()
    assert result.assembled.calculation is not None
    assert result.assembled.calculation.result == 2


def test_retriever_runs_one_micro_expansion_when_structural_contract_is_missing() -> None:
    class FakeRetriever:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def retrieve_debug(self, *, query: str, top_k: int, **_: object) -> dict[str, object]:
            self.calls.append((query, top_k))
            if len(self.calls) == 1:
                return {"hits": [hit("user: Please give me a deployment checklist.")]}
            return {
                "hits": [
                    hit(
                        "assistant: Deployment checklist:\n"
                        "11. Verify backups\n12. Rotate credentials\n13. Notify stakeholders",
                        source_id="expanded",
                    )
                ]
            }

    class FakeGraph:
        def __init__(self) -> None:
            self.retriever = FakeRetriever()

        def hybrid_retriever(self) -> FakeRetriever:
            return self.retriever

    graph = FakeGraph()
    result = TemporalSlotRetriever(graph).retrieve(
        query="What was the 12th item in the deployment checklist you gave me?",
        max_context_tokens=500,
    )

    assert result.fallback_used is True
    assert result.validation_issues == ()
    assert len(graph.retriever.calls) == 2
    assert graph.retriever.calls[0][1] == 20
    assert graph.retriever.calls[1][1] == 40
    assert "ITEM 12: Rotate credentials" in result.context.text


def test_duration_contract_rejects_adjacent_entity_and_recovers_exact_entity() -> None:
    class FakeRetriever:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def retrieve_debug(self, *, top_k: int, **_: object) -> dict[str, object]:
            self.calls.append(top_k)
            if top_k == 20:
                return {"hits": [hit("user: I have collected vintage cameras since last month.")]}
            return {"hits": [hit("user: I have been collecting vintage films for twelve years.")]}

    class FakeGraph:
        def __init__(self) -> None:
            self.retriever = FakeRetriever()

        def hybrid_retriever(self) -> FakeRetriever:
            return self.retriever

    result = TemporalSlotRetriever(FakeGraph()).retrieve(
        query="How long have I been collecting vintage films?",
        max_context_tokens=500,
    )

    assert result.plan.slots[0].required_terms == ("vintage", "films")
    assert result.fallback_used is True
    assert result.validation_issues == ()
    assert "twelve years" in result.context.text
    assert "vintage cameras" not in result.context.text
