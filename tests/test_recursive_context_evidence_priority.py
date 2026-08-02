from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from src.waggle.recursive_context import RecursiveContextController


def test_constraint_query_prioritizes_correction_evidence() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    hits = [
        SimpleNamespace(
            transcript_snippet="user: I need help with a travel itinerary. assistant: A luxury hotel focused approach may be a safe default."
        ),
        SimpleNamespace(
            transcript_snippet="user: Can the travel itinerary still feel polished? assistant: Yes, while respecting the corrected constraint."
        ),
        SimpleNamespace(
            transcript_snippet="user: Quick correction: the right constraint for the travel itinerary is budget hostel focused because I am saving cash."
        ),
    ]

    ordered = controller._prioritize_transcript_hits("What constraint should the travel itinerary respect?", hits)

    assert "budget hostel focused" in controller._transcript_snippet(ordered[0])


def test_count_query_prioritizes_all_arithmetic_steps_over_labels() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    hits = [
        SimpleNamespace(
            transcript_snippet="user: I bought 4 more notebook packs today. assistant: Recorded: 4 notebook packs added."
        ),
        SimpleNamespace(
            transcript_snippet="user: The labels for the notebook packs are in a separate drawer. assistant: Noted."
        ),
        SimpleNamespace(
            transcript_snippet="user: Inventory note: we have 12 notebook packs in storage. assistant: Recorded: 12 notebook packs."
        ),
        SimpleNamespace(
            transcript_snippet="user: We used 5 notebook packs for the event. assistant: That reduces the count by 5."
        ),
    ]

    ordered = controller._prioritize_transcript_hits("How many notebook packs should the user have now?", hits)
    snippets = [controller._transcript_snippet(hit) for hit in ordered[:3]]

    assert any("used 5 notebook packs" in snippet for snippet in snippets)
    assert any("bought 4 more notebook packs" in snippet for snippet in snippets)
    assert any("Inventory note" in snippet for snippet in snippets)
    assert all("labels" not in snippet for snippet in snippets)


def test_exact_recall_topic_extraction_uses_about_clause() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    topic = controller._extract_topic(
        "I'm checking our previous chat about the shift rotation sheet for GM social media agents. "
        "Can you remind me what was the rotation for Admon on a Sunday?"
    )

    assert topic == "shift rotation sheet for GM social media agents"


def test_exact_recall_context_puts_top_ranked_memories_before_type_buckets() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    relevant_other = SimpleNamespace(
        node_id="relevant",
        node_type="",
        label="Sunday Admon rotation",
        content="Sunday | Admon | Magdy | Ehab | Sara. Admon is assigned to 8 am - 4 pm.",
        score=0.99,
        is_superseded=False,
        updates_ids=[],
        raw_node=None,
    )
    irrelevant_decision = SimpleNamespace(
        node_id="irrelevant",
        node_type="decision",
        label="Twitter chat plan",
        content="Use Twitter polls for sustainable beauty engagement.",
        score=0.20,
        is_superseded=False,
        updates_ids=[],
        raw_node=None,
    )

    context, _nodes = controller._compress_to_budget(
        query="Can you remind me what was the rotation for Admon on a Sunday?",
        hits=[relevant_other, irrelevant_decision],
        conflicts=[],
        transcript_hits=[],
        token_budget=512,
    )

    assert "Most relevant memories:" in context
    assert context.index("Sunday Admon rotation") < context.index("Twitter chat plan")


def test_exact_recall_transcript_priority_uses_query_terms() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    hits = [
        SimpleNamespace(
            session_id="paint", transcript_snippet="user: I need help matching paint colors for a model aircraft."
        ),
        SimpleNamespace(
            session_id="shift",
            transcript_snippet="user: can u create a shift rotation sheet for GM social media agents",
        ),
    ]

    ordered = controller._prioritize_transcript_hits(
        "Can you remind me what was the rotation for Admon on a Sunday?",
        hits,
    )

    assert ordered[0].session_id == "shift"


def test_exact_recall_session_ids_can_come_from_node_evidence_records() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    hit = SimpleNamespace(
        session_id="",
        raw_node=SimpleNamespace(evidence_records=[SimpleNamespace(session_id="support-session")]),
    )

    assert controller._hit_session_ids(hit) == ["support-session"]


def test_build_context_includes_direct_transcript_evidence_when_graph_is_noisy() -> None:
    class NoisyGraph:
        def __init__(self) -> None:
            self.search_calls = []

        def query(self, **kwargs):
            noisy_node = SimpleNamespace(
                id="n1",
                label="unrelated NFT note",
                content="NFTs and closet organization",
                node_type=SimpleNamespace(value="note"),
                final_score=0.2,
                similarity_score=0.2,
                created_at=None,
                valid_to=None,
                evidence_records=[],
            )
            return SimpleNamespace(nodes=[noisy_node], edges=[])

        def get_related(self, **kwargs):
            return SimpleNamespace(nodes=[], edges=[])

        def search_transcript_records(self, **kwargs):
            self.search_calls.append(kwargs)
            return [
                SimpleNamespace(
                    session_id="run-session",
                    transcript_snippet="user: My charity 5K personal best is now 25:50.",
                )
            ]

    graph = NoisyGraph()
    controller = RecursiveContextController(graph=graph)

    result = controller.build_context(
        query="What was my personal best time in the charity 5K run?",
        project="project",
        agent_id="agent",
        token_budget=512,
    )

    assert graph.search_calls
    assert "25:50" in result.context_pack


def test_answer_category_detects_table_fact_and_temporal_queries() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    assert (
        controller._detect_answer_category("Can you remind me what was the rotation for Admon on a Sunday?")
        == "table_lookup"
    )
    assert controller._detect_answer_category("What degree did I graduate with?") == "short_personal_fact"
    assert controller._detect_answer_category("How long is my daily commute to work?") == "short_personal_fact"
    assert (
        controller._detect_answer_category("What time do I stop checking work emails and messages?")
        == "short_personal_fact"
    )
    assert (
        controller._detect_answer_category(
            "Which three events happened in the order from first to last: "
            "the day I helped my friend prepare the nursery, "
            "the day I helped my cousin pick out stuff for her baby shower, "
            "and the day I ordered a customized phone case?"
        )
        == "temporal_ordering"
    )
    assert (
        controller._detect_answer_category(
            "How many days had passed between the day I bought a gift for my brother's graduation ceremony "
            "and the day I bought a birthday gift for my best friend?"
        )
        == "temporal_ordering"
    )
    assert (
        controller._detect_answer_category(
            "Can you remind me what color was the scaly body of the Plesiosaur in the image?"
        )
        == "exact_detail"
    )
    assert (
        controller._detect_answer_category("Can you remind me of that unique dessert shop with the giant milkshakes?")
        == "exact_detail"
    )
    assert (
        controller._detect_answer_category(
            "Can you remind me what kind of processes are used at the Lake Charles Refinery?"
        )
        == "enumerated_list"
    )
    assert controller._detect_answer_category("Where do I currently keep my old sneakers?") == "short_personal_fact"
    assert controller._detect_answer_category("What was my previous occupation?") == "short_personal_fact"
    assert (
        controller._detect_answer_category("How many different types of food delivery services have I used recently?")
        == "short_personal_fact"
    )


def test_table_lookup_transcript_priority_filters_placeholder_rows() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    hits = [
        SimpleNamespace(
            transcript_snippet="Shift table | Sunday | Agent 1 | Agent 2 | Agent 3 | Agent 4 |",
        ),
        SimpleNamespace(
            transcript_snippet="Final table | Sunday | Admon | Magdy | Ehab | Sara |",
        ),
    ]

    ordered = controller._prioritize_transcript_hits(
        "Can you remind me what was the rotation for Admon on a Sunday?",
        hits,
    )

    assert "Admon" in controller._transcript_snippet(ordered[0])


def test_answer_bearing_section_puts_exact_transcript_before_noisy_nodes() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    noisy_node = SimpleNamespace(
        node_id="noisy",
        node_type="entity",
        label="The Long Way",
        content="A long book recommendation unrelated to a commute duration.",
        score=0.9,
        is_superseded=False,
        raw_node=None,
    )
    exact_transcript = SimpleNamespace(
        transcript_snippet="user: I've been listening during my daily commute, which takes 45 minutes each way.",
    )

    lines, _nodes, _keys, _ids = controller._answer_bearing_evidence_section(
        query="How long is my daily commute to work?",
        category="short_personal_fact",
        hits=[noisy_node],
        transcript_hits=[exact_transcript],
        max_tokens=256,
    )

    assert lines
    assert "45 minutes each way" in lines[0]
    assert all("The Long Way" not in line for line in lines)


def test_exact_detail_evidence_centers_long_transcript_on_color_answer() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    long_transcript = SimpleNamespace(
        transcript_snippet=(
            "Chapter 1: The T-Rex has a green scaly body. "
            + "filler " * 120
            + "Chapter 3: The Swimming Plesiosaur. The Plesiosaur has a blue scaly body, "
            "and its eyes are fixed on something in the distance."
        )
    )

    lines, _nodes, _keys, _ids = controller._answer_bearing_evidence_section(
        query="Can you remind me what color was the scaly body of the Plesiosaur in the image?",
        category="exact_detail",
        hits=[],
        transcript_hits=[long_transcript],
        max_tokens=320,
    )

    assert lines
    assert "Plesiosaur has a blue scaly body" in lines[0]


def test_exact_detail_named_place_evidence_scores_shop_with_milkshakes() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    wrong = SimpleNamespace(
        transcript_snippet="assistant: Orlando has many restaurants and theme parks, including ICON Park."
    )
    right = SimpleNamespace(
        transcript_snippet=(
            "assistant: The Sugar Factory - A sweet shop located at Icon Park that offers "
            "specialty drinks and giant milkshakes."
        )
    )

    ordered = controller._prioritize_transcript_hits(
        "Can you remind me of that unique dessert shop with the giant milkshakes?",
        [wrong, right],
    )

    assert "The Sugar Factory" in controller._transcript_snippet(ordered[0])


def test_enumerated_list_answer_bearing_section_preserves_complete_raw_list() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    partial_node = SimpleNamespace(
        node_id="alkylation",
        node_type="entity",
        label="Alkylation",
        content="Alkylation: combines smaller molecules to form larger ones.",
        score=0.92,
        is_superseded=False,
        raw_node=None,
    )
    complete_transcript = SimpleNamespace(
        transcript_snippet=(
            "assistant: Lake Charles Refinery: "
            "* Atmospheric distillation: separates crude oil into fractions. "
            "* Fluid catalytic cracking (FCC): breaks heavier fractions into gasoline and diesel. "
            "* Alkylation: creates high-octane gasoline components. "
            "* Hydrotreating: removes impurities from gasoline and diesel fractions."
        )
    )

    lines, _nodes, _keys, _ids = controller._answer_bearing_evidence_section(
        query="Can you remind me what kind of processes are used at the Lake Charles Refinery?",
        category="enumerated_list",
        hits=[partial_node],
        transcript_hits=[complete_transcript],
        max_tokens=420,
    )

    joined = "\n".join(lines)
    assert "Atmospheric distillation" in joined
    assert "Fluid catalytic cracking" in joined
    assert "Alkylation" in joined
    assert "Hydrotreating" in joined


def test_pinned_lane_past_scope_keeps_superseded_user_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    old = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript_snippet="user: I used to take yoga classes at Riverside Wellness Studio before I switched.",
    )
    current = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        transcript_snippet="user: I now take yoga classes at Serenity Yoga.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where did I used to take yoga classes before I switched?",
        hits=[],
        transcript_hits=[current, old],
        max_tokens=120,
    )

    joined = "\n".join(lines)
    assert "Riverside Wellness Studio" in joined
    assert "Serenity Yoga" not in joined


def test_pinned_lane_current_scope_prefers_current_user_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    old = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript_snippet="user: I used to take yoga classes at Riverside Wellness Studio before I switched.",
    )
    current = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        transcript_snippet="user: I now take yoga classes at Serenity Yoga.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes now?",
        hits=[],
        transcript_hits=[old, current],
        max_tokens=120,
    )

    joined = "\n".join(lines)
    assert "Serenity Yoga" in joined
    assert "Riverside Wellness Studio" not in joined


def test_pinned_lane_unspecified_frequency_prefers_newer_user_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    old = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 4, 3, tzinfo=UTC),
        transcript_snippet="user: I have a therapy session with Dr. Smith coming up soon - it's every two weeks.",
    )
    current = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 11, 3, tzinfo=UTC),
        transcript_snippet="user: I see Dr. Smith every week, and she's been helping me work on boundaries.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="How often do I see my therapist, Dr. Smith?",
        hits=[],
        transcript_hits=[old, current],
        max_tokens=140,
    )

    joined = "\n".join(lines)
    assert "every week" in joined
    assert "every two weeks" not in joined


def test_pinned_lane_uses_longmemeval_document_date_over_ingest_time() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    ingest_time = datetime(2026, 8, 2, tzinfo=UTC)
    old = SimpleNamespace(
        role="user",
        observed_at=ingest_time,
        transcript_snippet=(
            "[documentDate: 2023/04/03]\n"
            "user: I have a therapy session with Dr. Smith coming up soon - it's every two weeks."
        ),
    )
    current = SimpleNamespace(
        role="user",
        observed_at=ingest_time,
        transcript_snippet=(
            "[documentDate: 2023/11/03]\n"
            "user: I see Dr. Smith every week, and she's been helping me work on boundaries."
        ),
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="How often do I see my therapist, Dr. Smith?",
        hits=[],
        transcript_hits=[old, current],
        max_tokens=140,
    )

    joined = "\n".join(lines)
    assert "every week" in joined
    assert "every two weeks" not in joined


def test_pinned_lane_unspecified_current_record_prefers_newer_count() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    old = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 6, 16, tzinfo=UTC),
        transcript_snippet="user: I've been doing pretty well in the volleyball league, we're 3-2 so far!",
    )
    current = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 6, 30, tzinfo=UTC),
        transcript_snippet=(
            "user: Our volleyball team, the Net Ninjas, is doing well with a 5-2 record."
        ),
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="What is my current record in the recreational volleyball league?",
        hits=[],
        transcript_hits=[old, current],
        max_tokens=140,
    )

    joined = "\n".join(lines)
    assert "5-2" in joined
    assert "3-2" not in joined


def test_pinned_lane_record_query_rejects_unrelated_date_ranges() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    noisy = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 7, 15, tzinfo=UTC),
        transcript_snippet=(
            "user: My friend is living in Paris and I'd like to visit her within the next 3-4 months."
        ),
    )
    relevant = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 7, 21, tzinfo=UTC),
        transcript_snippet="user: Our volleyball team, the Net Ninjas, is doing well with a 5-2 record.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="What is my current record in the recreational volleyball league?",
        hits=[],
        transcript_hits=[noisy, relevant],
        max_tokens=140,
    )

    joined = "\n".join(lines)
    assert "5-2" in joined
    assert "3-4 months" not in joined


def test_pinned_lane_count_query_rejects_unrelated_durations() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    noisy_later = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 8, 1, tzinfo=UTC),
        transcript_snippet="user: I have been using the meditation app for 2 months and like the sleep stories.",
    )
    relevant = SimpleNamespace(
        role="user",
        observed_at=datetime(2023, 7, 1, tzinfo=UTC),
        transcript_snippet=(
            "user: With the new road bike from my brother, I'll actually have four bikes now."
        ),
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="How many bikes do I currently own?",
        hits=[],
        transcript_hits=[noisy_later, relevant],
        max_tokens=140,
    )

    joined = "\n".join(lines)
    assert "four bikes" in joined
    assert "2 months" not in joined


def test_pinned_lane_ignores_document_date_numbers_for_numeric_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    stale = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        transcript_snippet=(
            "[documentDate: 2023/05/25 (Thu) 05:26] "
            "user: I've got 1250 followers on Instagram now."
        ),
    )
    current = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        transcript_snippet=(
            "[documentDate: 2023/05/25 (Thu) 09:28] "
            "user: I've been meaning to check my current follower count - I think I'm close to 1300 now."
        ),
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="How many followers do I have on Instagram now?",
        hits=[],
        transcript_hits=[stale, current],
        max_tokens=160,
    )

    joined = "\n".join(lines)
    assert "1300" in joined
    assert "1250" not in joined


def test_pinned_lane_uses_session_document_date_for_unprefixed_turns() -> None:
    class SessionDateGraph:
        def list_transcript_records(self, **kwargs):
            if kwargs.get("session_id") == "old-session":
                return [
                    SimpleNamespace(
                        transcript_text="[documentDate: 2023/05/25 (Thu) 05:26] user: Session date marker.",
                    )
                ]
            if kwargs.get("session_id") == "current-session":
                return [
                    SimpleNamespace(
                        transcript_text="[documentDate: 2023/05/25 (Thu) 09:28] user: Session date marker.",
                    )
                ]
            return []

    controller = RecursiveContextController(graph=SessionDateGraph())
    stale = SimpleNamespace(
        role="user",
        agent_id="agent",
        project="project",
        session_id="old-session",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        transcript_snippet="user: I've got 1250 followers on Instagram now.",
    )
    current = SimpleNamespace(
        role="user",
        agent_id="agent",
        project="project",
        session_id="current-session",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        transcript_snippet="user: I think I'm close to 1300 followers on Instagram now.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="How many followers do I have on Instagram now?",
        hits=[],
        transcript_hits=[stale, current],
        max_tokens=160,
    )

    joined = "\n".join(lines)
    assert "1300" in joined
    assert "1250" not in joined


def test_pinned_lane_team_count_uses_latest_relevant_numeric_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    stale = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        transcript_snippet=(
            "[documentDate: 2023/07/11 (Tue) 13:05] "
            "user: My former manager Rachel is leading a team of 10 people, and half of them are women."
        ),
    )
    current = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        transcript_snippet=(
            "[documentDate: 2023/08/03 (Thu) 05:53] "
            "user: Rachel's team is a great example of a diverse team, with 6 women out of 10 people."
        ),
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="How many women are on the team led by my former manager Rachel?",
        hits=[],
        transcript_hits=[stale, current],
        max_tokens=160,
    )

    joined = "\n".join(lines)
    assert "6 women" in joined
    assert "half" not in joined


def test_pinned_lane_three_state_history_respects_final_revert() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    first = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript_snippet="user: I take yoga classes at Riverside Wellness Studio.",
    )
    middle = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        transcript_snippet="user: I switched to Serenity Yoga for yoga classes.",
    )
    final = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 3, 1, tzinfo=UTC),
        transcript_snippet="user: I switched back to Riverside Wellness Studio for yoga classes now.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes now?",
        hits=[],
        transcript_hits=[first, middle, final],
        max_tokens=120,
    )

    joined = "\n".join(lines)
    assert "Riverside Wellness Studio" in joined
    assert "switched back" in joined


def test_pinned_lane_unspecified_single_clean_fact_pins() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    hit = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript_snippet="user: I take yoga classes at Serenity Yoga.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=[hit],
        max_tokens=120,
    )

    assert lines
    assert "Serenity Yoga" in "\n".join(lines)


def test_pinned_lane_unspecified_conflict_abstains_without_clear_update() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    first = SimpleNamespace(
        role="user",
        transcript_snippet="user: I take yoga classes at Riverside Wellness Studio.",
    )
    second = SimpleNamespace(
        role="user",
        transcript_snippet="user: I take yoga classes at Serenity Yoga.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=[first, second],
        max_tokens=120,
    )

    assert lines == []


def test_pinned_lane_accepts_assistant_echo_linked_to_user_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    user_fact = SimpleNamespace(
        role="user",
        session_id="s1",
        turn_index=1,
        transcript_snippet="user: I take yoga classes at Riverside Wellness Studio.",
    )
    assistant_echo = SimpleNamespace(
        role="assistant",
        session_id="s7",
        turn_index=8,
        transcript_snippet="assistant: Got it, your studio is Riverside Wellness Studio.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=[assistant_echo, user_fact],
        max_tokens=120,
    )

    assert "Riverside Wellness Studio" in "\n".join(lines)


def test_pinned_lane_rejects_repeated_assistant_speculation_without_user_origin() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    speculative = SimpleNamespace(
        role="assistant",
        transcript_snippet="assistant: You might like Riverside Wellness Studio for yoga.",
    )
    repeated = SimpleNamespace(
        role="assistant",
        transcript_snippet="assistant: Riverside Wellness Studio has calming classes.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=[speculative, repeated],
        max_tokens=120,
    )

    assert lines == []


def test_pinned_lane_rejects_confident_assistant_speculation_without_hedge() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    speculative = SimpleNamespace(
        role="assistant",
        transcript_snippet="assistant: Your best option is Riverside Wellness Studio for yoga.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=[speculative],
        max_tokens=120,
    )

    assert lines == []


def test_pinned_lane_rejects_speculative_past_state_candidate() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    speculative = SimpleNamespace(
        role="assistant",
        transcript_snippet="assistant: You may have gone to Riverside Wellness Studio before switching.",
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where did I used to take yoga before I switched?",
        hits=[],
        transcript_hits=[speculative],
        max_tokens=120,
    )

    assert lines == []


def test_pinned_lane_rejects_adversarial_near_miss_queries() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    fact = SimpleNamespace(
        role="user",
        transcript_snippet="user: I take yoga classes at Serenity Yoga.",
    )

    for query in [
        "What's a good name for my new yoga studio?",
        "Where should I take yoga if I move to Miami?",
        "What was Sarah's last name before she got married?",
        "What is my mother's maiden name?",
    ]:
        lines, _nodes, _keys, _ids = controller._pinned_fact_section(
            query=query,
            hits=[],
            transcript_hits=[fact],
            max_tokens=120,
        )
        assert lines == []


def test_pinned_lane_handles_known_identity_and_named_place_shapes() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    identity = SimpleNamespace(
        role="user",
        transcript_snippet="user: My last name was Johnson before I changed it.",
    )
    place = SimpleNamespace(
        role="user",
        transcript_snippet="user: I take yoga classes at Serenity Yoga.",
    )

    identity_lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="What was my last name before I changed it?",
        hits=[],
        transcript_hits=[identity],
        max_tokens=120,
    )
    place_lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=[place],
        max_tokens=120,
    )

    assert "Johnson" in "\n".join(identity_lines)
    assert "Serenity Yoga" in "\n".join(place_lines)
    assert not controller._looks_like_named_place_query("what was my last name before i changed it?")


def test_pinned_transcript_evidence_admits_exact_user_fact_before_noisy_context() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="assistant",
                    transcript_text="assistant: Here are generic tips for yoga classes and apps like Down Dog.",
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I've actually been using Down Dog for my home practice. "
                        "It's helpful on days when I can't make it to Serenity Yoga."
                    ),
                ),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())

    hits = controller._pinned_transcript_evidence(
        query="Where do I take yoga classes?",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        limit=4,
    )
    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I take yoga classes?",
        hits=[],
        transcript_hits=hits,
        max_tokens=120,
    )

    assert hits
    assert "Serenity Yoga" in "\n".join(lines)


def test_pinned_transcript_evidence_admits_clock_time_boundary_fact() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="user",
                    transcript_text=("user: I read a LeBron article and want help rewriting it into a unique article."),
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I've been trying to establish a better evening routine, "
                        "stopping work emails and messages by 7 pm to separate my work and personal life."
                    ),
                ),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())

    hits = controller._pinned_transcript_evidence(
        query="What time do I stop checking work emails and messages?",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        limit=4,
    )
    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="What time do I stop checking work emails and messages?",
        hits=[],
        transcript_hits=hits,
        max_tokens=120,
    )

    assert hits
    assert "7 pm" in "\n".join(lines)


def test_pinned_lane_admits_current_storage_location_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    stale_assistant = SimpleNamespace(
        role="assistant",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript_snippet="assistant: Keeping old sneakers under the bed can make them smell.",
    )
    current_user = SimpleNamespace(
        role="user",
        observed_at=datetime(2026, 2, 1, tzinfo=UTC),
        transcript_snippet=(
            "user: I need to organize my closet this weekend, and I'm looking forward "
            "to storing my old sneakers in a shoe rack."
        ),
    )

    lines, _nodes, _keys, _ids = controller._pinned_fact_section(
        query="Where do I currently keep my old sneakers?",
        hits=[],
        transcript_hits=[stale_assistant, current_user],
        max_tokens=160,
    )

    joined = "\n".join(lines)
    assert "shoe rack" in joined
    assert "under the bed" not in joined


def test_answer_bearing_section_admits_previous_occupation_fact() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    noisy = SimpleNamespace(transcript_snippet="user: In my previous role at the startup, I managed interns.")
    exact = SimpleNamespace(
        transcript_snippet=(
            "user: I've used Trello in my previous role as a marketing specialist "
            "at a small startup and I'm familiar with its features."
        )
    )

    lines, _nodes, _keys, _ids = controller._answer_bearing_evidence_section(
        query="What was my previous occupation?",
        category="short_personal_fact",
        hits=[],
        transcript_hits=[noisy, exact],
        max_tokens=260,
    )

    joined = "\n".join(lines)
    assert "marketing specialist" in joined
    assert "small startup" in joined


def test_lexical_answer_evidence_admits_previous_occupation_from_full_transcripts() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="user",
                    transcript_text="user: In my previous role at the startup, I managed interns.",
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I've used Trello in my previous role as a marketing specialist "
                        "at a small startup and I'm familiar with its features."
                    ),
                ),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())

    hits = controller._lexical_answer_transcript_evidence(
        query="What was my previous occupation?",
        category="short_personal_fact",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        limit=2,
    )

    joined = "\n".join(controller._transcript_snippet(hit) for hit in hits)
    assert "marketing specialist" in joined
    assert "small startup" in joined


def test_count_query_prioritizes_pickup_return_and_delivery_evidence() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    clothing_hits = [
        SimpleNamespace(transcript_snippet="user: I need to organize my closet and fold sweaters."),
        SimpleNamespace(
            transcript_snippet="user: I exchanged a pair of boots at Zara and need to pick up the new pair."
        ),
        SimpleNamespace(
            transcript_snippet="assistant: Don't forget to pick up the dry cleaning for your navy blue blazer."
        ),
    ]

    ordered_clothing = controller._prioritize_transcript_hits(
        "How many items of clothing do I need to pick up or return from a store?",
        clothing_hits,
    )
    clothing_text = "\n".join(controller._transcript_snippet(hit) for hit in ordered_clothing[:2])
    assert "Zara" in clothing_text
    assert "dry cleaning" in clothing_text

    delivery_hits = [
        SimpleNamespace(transcript_snippet="assistant: Cooking at home is healthier than takeout."),
        SimpleNamespace(transcript_snippet="user: I had Domino's Pizza three times last week."),
        SimpleNamespace(transcript_snippet="user: I found a delivery service called Fresh Fusion."),
        SimpleNamespace(transcript_snippet="user: I ordered Uber Eats after work yesterday."),
    ]
    ordered_delivery = controller._prioritize_transcript_hits(
        "How many different types of food delivery services have I used recently?",
        delivery_hits,
    )
    delivery_text = "\n".join(controller._transcript_snippet(hit) for hit in ordered_delivery[:3])
    assert "Domino" in delivery_text
    assert "Fresh Fusion" in delivery_text
    assert "Uber Eats" in delivery_text


def test_count_query_context_includes_pickup_return_counting_guidance() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    context, _nodes = controller._compress_to_budget(
        query="How many items of clothing do I need to pick up or return from a store?",
        hits=[],
        conflicts=[],
        transcript_hits=[
            SimpleNamespace(transcript_snippet="user: I exchanged boots and still need to pick up the new pair."),
        ],
        token_budget=512,
    )

    assert "Answer guidance:" in context
    assert "count each still-open pickup and each still-open return obligation separately" in context


def test_count_query_context_extracts_separate_pickup_return_candidates() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    context, _nodes = controller._compress_to_budget(
        query="How many items of clothing do I need to pick up or return from a store?",
        hits=[],
        conflicts=[],
        transcript_hits=[
            SimpleNamespace(
                transcript_snippet=(
                    "user: I need to return some boots to Zara, actually. I got them on February 5th, "
                    "but they were too small, so I exchanged them for a larger size. "
                    "I just haven't had a chance to pick them up yet."
                )
            ),
            SimpleNamespace(
                transcript_snippet=(
                    "user: I still need to pick up my dry cleaning for the navy blue blazer "
                    "I wore to a meeting a few weeks ago."
                )
            ),
        ],
        token_budget=900,
    )

    assert "Count candidates:" in context
    assert "return boots to Zara" in context
    assert (
        "pick up new boots" in context
        or "pick up new pair of boots" in context
        or "pick up replacement boots" in context
    )
    assert "pick up dry cleaning for navy blue blazer" in context


def test_count_query_context_extracts_base_count_plus_addition() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    context, _nodes = controller._compress_to_budget(
        query="How many pre-1920 American coins do I have in my collection?",
        hits=[],
        conflicts=[],
        transcript_hits=[
            SimpleNamespace(
                transcript_snippet=(
                    "user: I have a total of 37 coins in my pre-1920 American coin collection."
                )
            ),
            SimpleNamespace(
                transcript_snippet=(
                    "user: I just added a new coin to my collection of pre-1920 American coins - "
                    "a 1915-S Barber quarter."
                )
            ),
        ],
        token_budget=900,
    )

    assert "Answer guidance:" in context
    assert "combine the base count with later additions/removals" in context
    assert "Count candidates:" in context
    assert "base count: 37 coins" in context
    assert "addition: 1 coin" in context
    assert "1915-S Barber quarter" in context


def test_count_query_dedupes_partial_duplicate_coin_addition() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    context, _nodes = controller._compress_to_budget(
        query="How many pre-1920 American coins do I have in my collection?",
        hits=[],
        conflicts=[],
        transcript_hits=[
            SimpleNamespace(
                transcript_snippet=(
                    "user: I have a total of 37 coins in that collection, "
                    "and I think it would be cool to see them all displayed together."
                )
            ),
            SimpleNamespace(
                transcript_snippet=(
                    "user: By the way, I just added a new coin to my collection "
                    "of pre-1920 American coins - a 1915-S Barber quarter."
                )
            ),
            SimpleNamespace(
                transcript_snippet=(
                    "user: By the way, I just added a new coin to my collection "
                    "of pre-1920 American coins -"
                )
            ),
        ],
        token_budget=900,
    )

    assert "Count candidates:" in context
    assert "base count: 37 coins" in context
    assert "addition: 1 coin (1915-S Barber quarter)" in context
    assert context.count("- addition: 1 coin") == 1


def test_set_aggregation_context_collects_tomato_and_cucumber_counts() -> None:
    class PlantGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    transcript_text=(
                        "user: I've been enjoying the harvest immensely! "
                        "I planted 5 tomato plants initially, and they've been producing like crazy."
                    )
                ),
                SimpleNamespace(
                    transcript_text=(
                        "user: I've been growing my own cucumbers in my garden, "
                        "and I've got 3 plants that are producing a lot of them!"
                    )
                ),
            ]

    controller = RecursiveContextController(graph=PlantGraph())

    context, _nodes = controller._compress_to_budget(
        query="How many plants did I initially plant for tomatoes and cucumbers?",
        hits=[],
        conflicts=[],
        transcript_hits=[],
        token_budget=900,
        scope={"agent_id": "a", "project": "p", "session_id": ""},
    )

    assert "Set candidates:" in context
    assert "tomato plants: 5" in context
    assert "cucumber plants: 3" in context


def test_set_aggregation_context_collects_model_kits_across_sessions() -> None:
    class ModelGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    transcript_text="user: I recently finished a simple Revell F-15 Eagle kit."
                ),
                SimpleNamespace(
                    transcript_text="user: I recently finished a Tamiya 1/48 scale Spitfire Mk.V."
                ),
                SimpleNamespace(
                    transcript_text="user: I started working on a diorama featuring a 1/16 scale German Tiger I tank."
                ),
                SimpleNamespace(
                    transcript_text=(
                        "user: I just got this 1/72 scale B-29 bomber model kit "
                        "and a 1/24 scale '69 Camaro at a model show."
                    )
                ),
            ]

    controller = RecursiveContextController(graph=ModelGraph())

    context, _nodes = controller._compress_to_budget(
        query="How many model kits have I worked on or bought?",
        hits=[],
        conflicts=[],
        transcript_hits=[],
        token_budget=1000,
        scope={"agent_id": "a", "project": "p", "session_id": ""},
    )

    assert "Set candidates:" in context
    for expected in ["Revell F-15 Eagle", "Tamiya 1/48 scale Spitfire Mk.V", "German Tiger I tank", "B-29 bomber", "'69 Camaro"]:
        assert expected in context


def test_set_aggregation_context_collects_acquired_jewelry() -> None:
    class JewelryGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    transcript_text=(
                        "user: I just got a new pair of earrings last weekend at a flea market - "
                        "a stunning pair of emerald earrings."
                    )
                ),
                SimpleNamespace(
                    transcript_text=(
                        "user: I just got a new silver necklace with a small pendant on the 15th of last month."
                    )
                ),
                SimpleNamespace(
                    transcript_text="user: I got my engagement ring a month ago, and it is still a bit too loose."
                ),
                SimpleNamespace(
                    transcript_text="assistant: A jewelry cleaning kit with a solution and cloth is a good idea."
                ),
            ]

    controller = RecursiveContextController(graph=JewelryGraph())

    context, _nodes = controller._compress_to_budget(
        query="How many pieces of jewelry did I acquire in the last two months?",
        hits=[],
        conflicts=[],
        transcript_hits=[],
        token_budget=1000,
        scope={"agent_id": "a", "project": "p", "session_id": ""},
    )

    assert "Set candidates:" in context
    assert "emerald earrings" in context
    assert "silver necklace" in context
    assert "engagement ring" in context
    assert "jewelry cleaning kit" not in context


def test_obligation_decomposition_handles_exchange_variants() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    direct_swap = controller._extract_obligation_candidates(
        "user: I need to return my red jacket and picked up a blue jacket.",
        event_prefix="t",
    )
    direct_labels = [controller._obligation_label(item) for item in direct_swap]
    assert "return red jacket" in direct_labels
    assert "pick up blue jacket" in direct_labels

    replacement = controller._extract_obligation_candidates(
        "user: I need to return some boots to Zara. They were too small, so I exchanged them for a larger size. "
        "I still need to pick them up.",
        event_prefix="t",
    )
    replacement_labels = [controller._obligation_label(item) for item in replacement]
    assert "return boots to Zara" in replacement_labels
    assert "pick up replacement boots from Zara" in replacement_labels


def test_lexical_answer_evidence_admits_named_delivery_services_from_full_transcripts() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(role="user", transcript_text="user: I had Domino's Pizza three times last week."),
                SimpleNamespace(role="user", transcript_text="user: I found a delivery service called Fresh Fusion."),
                SimpleNamespace(role="user", transcript_text="user: My weekends have been all about Uber Eats lately."),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())

    hits = controller._lexical_answer_transcript_evidence(
        query="How many different types of food delivery services have I used recently?",
        category="short_personal_fact",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        limit=3,
    )

    joined = "\n".join(controller._transcript_snippet(hit) for hit in hits)
    assert "Domino" in joined
    assert "Fresh Fusion" in joined
    assert "Uber Eats" in joined


def test_temporal_event_phrases_handles_between_two_days_query() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    phrases = controller._extract_event_phrases(
        "How many days had passed between the day I bought a gift for my brother's graduation ceremony "
        "and the day I bought a birthday gift for my best friend?"
    )

    assert any("brother's graduation ceremony" in phrase for phrase in phrases)
    assert any("birthday gift for my best friend" in phrase for phrase in phrases)


def test_lexical_answer_evidence_admits_both_temporal_gift_events() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "[documentDate: 2023/03/29] user: I recently got a wireless headphone "
                        "for my brother as a graduation gift on the 3/8, and it was a big hit."
                    ),
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "[documentDate: 2023/03/29] user: I recently got a silver necklace with "
                        "a tiny pendant for my best friend's 30th birthday on the 15th of March."
                    ),
                ),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())

    hits = controller._lexical_answer_transcript_evidence(
        query=(
            "How many days had passed between the day I bought a gift for my brother's graduation ceremony "
            "and the day I bought a birthday gift for my best friend?"
        ),
        category="temporal_ordering",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        limit=2,
    )

    joined = "\n".join(controller._transcript_snippet(hit) for hit in hits)
    assert "graduation gift on the 3/8" in joined
    assert "15th of March" in joined


def test_personalization_lane_promotes_painting_history_for_advice_query() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="assistant",
                    transcript_text="assistant: You can always seek inspiration from galleries and art communities.",
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I've been looking at a lot of flower paintings on Instagram and trying realistic flowers."
                    ),
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I have been getting inspiration from social media and recently started a 30-day painting challenge."
                    ),
                ),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())
    hits = controller._personalization_transcript_evidence(
        query="I've been feeling a bit stuck with my paintings lately. Do you have any ideas on how I can find new inspiration?",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        transcript_hits=[],
        limit=4,
    )
    lines, _keys = controller._personalization_evidence_section(
        query="I've been feeling a bit stuck with my paintings lately. Do you have any ideas on how I can find new inspiration?",
        transcript_hits=hits,
        emitted_transcript_keys=set(),
        max_tokens=180,
    )

    section = "\n".join(lines)
    assert "Instagram" in section
    assert "30-day painting challenge" in section
    assert "galleries and art communities" not in section


def test_personalization_lane_promotes_slow_cooker_experience_for_advice_query() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="assistant",
                    transcript_text="assistant: Here are some general tips for slow cooker recipes.",
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I recently figured out how to use the slow cooker and made a delicious beef stew. "
                        "I've been wanting to try more recipes with it."
                    ),
                ),
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I'm interested in making yogurt in the slow cooker, but I'm unsure about timing."
                    ),
                ),
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())
    hits = controller._personalization_transcript_evidence(
        query="I've been struggling with my slow cooker recipes. Any advice on getting better results?",
        scope={"agent_id": "a", "project": "p", "session_id": ""},
        transcript_hits=[],
        limit=4,
    )
    lines, _keys = controller._personalization_evidence_section(
        query="I've been struggling with my slow cooker recipes. Any advice on getting better results?",
        transcript_hits=hits,
        emitted_transcript_keys=set(),
        max_tokens=180,
    )

    section = "\n".join(lines)
    assert "beef stew" in section
    assert "yogurt" in section
    assert "general tips" not in section


def test_personalization_lane_does_not_trigger_for_plain_fact_lookup() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())

    assert not controller._looks_like_personalization_advice_query("what time do i stop checking work emails?")


def test_personalization_section_is_protected_before_generic_sections() -> None:
    class TranscriptGraph:
        def list_transcript_records(self, **_kwargs):
            return [
                SimpleNamespace(
                    role="user",
                    transcript_text=(
                        "user: I have been getting inspiration from social media and recently started a 30-day painting challenge."
                    ),
                )
            ]

    controller = RecursiveContextController(graph=TranscriptGraph())
    generic_constraint = SimpleNamespace(
        node_id="generic-constraint",
        node_type="preference",
        label="Make it achievable",
        content="Set a goal that's challenging but realistic.",
        score=0.99,
        is_superseded=False,
        updates_ids=[],
        raw_node=None,
    )

    context, _nodes = controller._compress_to_budget(
        query="I've been feeling a bit stuck with my paintings lately. Do you have any ideas on how I can find new inspiration?",
        hits=[generic_constraint],
        conflicts=[],
        transcript_hits=[],
        token_budget=1024,
        scope={"agent_id": "a", "project": "p", "session_id": ""},
    )

    assert "Personalization evidence:" in context
    assert "30-day painting challenge" in context
    assert "Active constraints:" in context
    assert context.index("Personalization evidence:") < context.index("Active constraints:")


def test_personalization_graph_filter_blocks_cross_domain_noise() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    noisy = [
        SimpleNamespace(label="Sun Basket", content="Meal delivery with vegetarian recipes.", node_type="entity"),
        SimpleNamespace(label="Spider-Man comics", content="Displaying comics beside figurines.", node_type="entity"),
        SimpleNamespace(
            label="What are tips to price paintings?",
            content="What are tips to price paintings online?",
            node_type="question",
        ),
        SimpleNamespace(
            label="30-day painting challenge",
            content="I have been getting inspiration from social media and recently started a 30-day painting challenge.",
            node_type="note",
        ),
    ]
    hits = [
        SimpleNamespace(
            node_id=f"n{index}",
            label=item.label,
            content=item.content,
            node_type=item.node_type,
            score=0.8,
            is_superseded=False,
            raw_node=None,
        )
        for index, item in enumerate(noisy)
    ]

    selected = controller._personalization_graph_hits(
        "I've been feeling a bit stuck with my paintings lately. Do you have any ideas on how I can find new inspiration?",
        hits,
    )
    labels = [hit.label for hit in selected]

    assert "30-day painting challenge" in labels
    assert "Sun Basket" not in labels
    assert "Spider-Man comics" not in labels
    assert "What are tips to price paintings?" not in labels


def test_personalization_graph_section_precedes_generic_graph_sections() -> None:
    controller = RecursiveContextController(graph=SimpleNamespace())
    beef = SimpleNamespace(
        node_id="beef",
        node_type="note",
        label="slow cooker beef stew",
        content="I recently figured out how to use the slow cooker and made a delicious beef stew.",
        score=0.9,
        is_superseded=False,
        updates_ids=[],
        raw_node=None,
    )
    generic = SimpleNamespace(
        node_id="generic",
        node_type="entity",
        label="10 Favorite Show-Stopper Recipes",
        content="10 Favorite Show-Stopper Recipes is a generic recipe collection.",
        score=0.95,
        is_superseded=False,
        updates_ids=[],
        raw_node=None,
    )

    context, _nodes = controller._compress_to_budget(
        query="I've been struggling with my slow cooker recipes. Any advice on getting better results?",
        hits=[generic, beef],
        conflicts=[],
        transcript_hits=[],
        token_budget=1024,
        scope={"agent_id": "a", "project": "p", "session_id": ""},
    )

    assert "Personalized graph memories:" in context
    assert "beef stew" in context
    assert "Show-Stopper Recipes" not in context
