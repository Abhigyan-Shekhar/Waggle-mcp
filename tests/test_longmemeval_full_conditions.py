from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.longmemeval_full.conditions import (
    AGENTIC_MCP,
    EXTERNAL_JSONL_PREFIX,
    FLAT_TRANSCRIPT_VECTOR,
    GRAPH_GUIDED_CONTEXT,
    GRAPH_NODES_ONLY,
    ORACLE_ANSWER_TURN_CONTEXT,
    PRODUCTION_CONTEXT,
    ConditionConfig,
    flat_transcript_vector,
    graph_guided_transcript_context,
    graph_nodes_only,
    hybrid_context,
    oracle_answer_turn_context,
    production_context,
    run_condition,
)
from scripts.longmemeval_full.fixtures import capability_fixtures
from scripts.longmemeval_full.ingestion import build_case_graph
from scripts.longmemeval_full.run import DeterministicEmbeddingModel, build_result_row


class FakeTranscriptHit:
    record_id = "tr-1"
    session_id = "s1"
    role = "user"
    transcript_text = "The confirmed answer is two free nights."
    turn_pair_id = "turn-1"


class FakeNode:
    def __init__(self, node_id: str = "node-1", content: str = "two free nights") -> None:
        self.id = node_id
        self.label = "Hilton nights"
        self.content = content
        self.type = SimpleNamespace(value="fact")
        self.source_turn_pair_id = "turn-1"
        self.evidence_records = [SimpleNamespace(evidence_id="ev-1", session_id="s1")]


class FakeEdge:
    id = "edge-1"
    source_id = "node-1"
    target_id = "node-2"
    relationship = SimpleNamespace(value="updates")


class FakeGraph:
    def __init__(self) -> None:
        self.query_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.prime_calls = 0
        self.related_calls = 0

    def search_transcript_records(self, **kwargs):
        self.search_calls.append(kwargs)
        return [FakeTranscriptHit()]

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return SimpleNamespace(nodes=[FakeNode()], edges=[FakeEdge()], hybrid_hits=[], fusion_hits=[])

    def debug_retrieval(self, **kwargs):
        return {
            "layers": {
                "vector_transcript": [{"id": "tr-1"}],
                "vector_node": [{"id": "node-1"}],
                "lexical": [],
                "graph_expansion": [],
            },
            "hybrid_top_hits": [],
            "fused_top20": [],
        }

    def prime_context(self, **kwargs):
        self.prime_calls += 1
        return SimpleNamespace(nodes=[FakeNode("prime-node", "primed project fact")])

    def get_related(self, *args, **kwargs):
        self.related_calls += 1
        return SimpleNamespace(nodes=[FakeNode("related-node", "related evidence")], edges=[FakeEdge()])


class FakeController:
    calls: list[dict] = []

    def __init__(self, graph) -> None:
        self.graph = graph

    def build_context(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            context_pack="Production context says two free nights.",
            nodes_used=[FakeNode()],
            edges_used=[FakeEdge()],
            transcript_evidence=[FakeTranscriptHit()],
            token_estimate=10,
            debug={"mode": kwargs.get("mode")},
        )


def case():
    return {
        "question_id": "case-1",
        "question_type": "knowledge-update",
        "question": "How many free nights?",
        "answer": "two free nights",
        "gold_support_ids": ["s1"],
    }


def oracle_case_with_late_answer_turn():
    return {
        "question_id": "oracle-late-answer",
        "question_type": "knowledge-update",
        "question": "What type of vehicle model am I currently working on?",
        "answer": "Ford F-150 pickup truck",
        "gold_support_ids": ["s1"],
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [
            [
                {
                    "role": "user",
                    "content": "I'm looking for weathering tips for my current project, a Ford Mustang Shelby GT350R model.",
                    "has_answer": False,
                },
                {
                    "role": "assistant",
                    "content": "Here is a long answer about paint and weathering. " * 80,
                    "has_answer": False,
                },
                {
                    "role": "user",
                    "content": "I have just wrapped up that model and switched to a Ford F-150 pickup truck.",
                    "has_answer": True,
                },
            ]
        ],
    }


def case_graph(graph=None):
    return {
        "graph": graph or FakeGraph(),
        "project": "proj",
        "agent_id": "agent",
        "all_session_ids": ["s1"],
        "session_dates": {"s1": "2026-01-01"},
        "session_messages": {"s1": []},
    }


def test_flat_transcript_vector_never_queries_graph() -> None:
    graph = FakeGraph()
    result = flat_transcript_vector(case(), case_graph(graph), config=ConditionConfig(reader_context_budget=128))

    assert result.condition == FLAT_TRANSCRIPT_VECTOR
    assert graph.search_calls
    assert graph.query_calls == []
    assert result.retrieved_transcript_ids == ["tr-1"]


def test_external_jsonl_context_uses_exported_context(tmp_path) -> None:
    export_path = tmp_path / "external_contexts.jsonl"
    export_path.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "system": "mem0",
                "context": "Mem0 says the confirmed answer is two free nights.",
                "retrieval_mode": "mem0_search",
                "retrieved_transcript_ids": ["mem0-hit-1"],
                "adapter_notes": ["exported by local adapter"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_condition(
        f"{EXTERNAL_JSONL_PREFIX}mem0",
        case(),
        case_graph(),
        config=ConditionConfig(reader_context_budget=128, external_context_path=export_path),
    )

    assert result.condition == "external_jsonl:mem0"
    assert "two free nights" in result.context
    assert result.retrieval_mode == "mem0_search"
    assert result.retrieved_transcript_ids == ["mem0-hit-1"]
    assert "exported by local adapter" in result.adapter_notes


def test_external_jsonl_context_preserves_itemized_context(tmp_path) -> None:
    export_path = tmp_path / "external_contexts.jsonl"
    export_path.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "system": "graphiti",
                "context_items": [
                    {"item_id": "edge-1", "item_type": "graph_fact", "text": "Earlier value: one night."},
                    {"item_id": "edge-2", "item_type": "graph_fact", "text": "Current value: two free nights."},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_condition(
        f"{EXTERNAL_JSONL_PREFIX}graphiti",
        case(),
        case_graph(),
        config=ConditionConfig(reader_context_budget=128, external_context_path=export_path),
    )

    assert "Earlier value" in result.context
    assert "Current value" in result.context
    assert [item.item_id for item in result.context_items] == ["edge-1", "edge-2"]


def test_graph_guided_condition_reproduces_old_context_path(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    def fake_context_from_waggle(input_case, *, condition, case_graph, retrieval_limit):
        called["condition"] = condition
        called["retrieval_limit"] = retrieval_limit
        return "legacy context", ["s1"], "memory_plus_source_chunk"

    monkeypatch.setattr(
        "scripts.longmemeval_full.conditions.legacy._context_from_waggle",
        fake_context_from_waggle,
    )
    result = graph_guided_transcript_context(case(), case_graph(), config=ConditionConfig(retrieval_limit=7))

    assert result.condition == GRAPH_GUIDED_CONTEXT
    assert called == {"condition": "waggle_full", "retrieval_limit": 7}
    assert "historical waggle_full" in " ".join(result.adapter_notes)


def test_production_context_calls_hybrid_query_and_build_context() -> None:
    FakeController.calls = []
    graph = FakeGraph()
    result = production_context(
        case(),
        case_graph(graph),
        config=ConditionConfig(reader_context_budget=128, controller_factory=FakeController),
        use_prime=False,
        temporal_resolution=True,
    )

    assert result.condition == PRODUCTION_CONTEXT
    assert graph.query_calls[0]["retrieval_mode"] == "hybrid"
    assert FakeController.calls
    assert FakeController.calls[0]["token_budget"] == 128
    assert result.retrieved_node_ids == ["node-1"]
    assert result.retrieved_edge_ids == ["edge-1"]


def test_context_budget_is_enforced_for_production_context() -> None:
    class LongController(FakeController):
        def build_context(self, **kwargs):
            return SimpleNamespace(
                context_pack=" ".join(["token"] * 500),
                nodes_used=[],
                edges_used=[],
                transcript_evidence=[],
                token_estimate=500,
                debug={},
            )

    result = production_context(
        case(),
        case_graph(FakeGraph()),
        config=ConditionConfig(reader_context_budget=20, controller_factory=LongController),
        use_prime=False,
        temporal_resolution=True,
    )

    assert result.context
    assert all(item.token_count <= 20 for item in result.context_items)


def test_exact_source_turn_recovered_from_node_provenance() -> None:
    result = graph_nodes_only(case(), case_graph(FakeGraph()), config=ConditionConfig(reader_context_budget=128))

    assert result.condition == GRAPH_NODES_ONLY
    assert result.source_evidence_ids == ["ev-1"]
    assert result.context_items[0].source_turn_id == "turn-1"


def test_agentic_mode_respects_tool_budget() -> None:
    graph = FakeGraph()
    result = run_condition(
        AGENTIC_MCP,
        case(),
        case_graph(graph),
        config=ConditionConfig(reader_context_budget=128, max_tool_calls=2),
    )

    assert len(result.tool_trace) <= 2
    assert [entry["tool"] for entry in result.tool_trace] == ["prime_context", "query_graph"]
    assert graph.related_calls == 0


def test_result_row_contains_required_provenance() -> None:
    FakeController.calls = []
    condition_result = production_context(
        case(),
        case_graph(FakeGraph()),
        config=ConditionConfig(reader_context_budget=128, controller_factory=FakeController),
        use_prime=False,
        temporal_resolution=True,
    )
    row = build_result_row(
        case(),
        case_index=0,
        condition_result=condition_result,
        config={
            "dataset": "targeted_stress_v2",
            "dataset_sha": "dataset-sha",
            "git_commit": "commit",
            "config_sha": "config-sha",
            "prompt_version": "prompt-v",
            "reader_model": "reader",
            "primary_judge_model": "judge",
            "secondary_judge_model": "judge2",
            "reader_context_budget": 128,
            "max_tool_calls": 6,
        },
        dataset_meta={"dataset_sha": "dataset-sha"},
        dry_run=True,
        elapsed_ms=1.0,
    )

    for key in [
        "case_id",
        "dataset_sha",
        "condition",
        "git_commit",
        "config_sha",
        "prompt_version",
        "retrieved_node_ids",
        "retrieved_transcript_ids",
        "retrieved_edge_ids",
        "source_evidence_ids",
        "tool_trace",
        "context_items",
        "final_context_tokens",
        "primary_judgment",
        "latency_ms",
        "cost",
    ]:
        assert key in row


def test_dry_run_result_row_does_not_include_gold_support_ids() -> None:
    condition_result = flat_transcript_vector(
        case(), case_graph(FakeGraph()), config=ConditionConfig(reader_context_budget=128)
    )
    row = build_result_row(
        case(),
        case_index=0,
        condition_result=condition_result,
        config={
            "dataset": "targeted_stress_v2",
            "dataset_sha": "dataset-sha",
            "git_commit": "commit",
            "config_sha": "config-sha",
            "prompt_version": "prompt-v",
            "reader_model": "reader",
            "primary_judge_model": "judge",
            "secondary_judge_model": "judge2",
            "reader_context_budget": 128,
            "max_tool_calls": 6,
        },
        dataset_meta={"dataset_sha": "dataset-sha"},
        dry_run=True,
        elapsed_ms=1.0,
    )

    assert "gold_support_ids" not in row
    assert row["answer"] == "DRY_RUN_NO_READER_CALL"
    assert row["primary_judgment"]["label"] == "dry_run_unscored"


def test_hybrid_no_edges_and_with_edges_keep_same_core_parameters() -> None:
    graph = FakeGraph()

    hybrid_context(
        case(),
        case_graph(graph),
        config=ConditionConfig(reader_context_budget=128, retrieval_limit=4, related_depth=2),
        include_edges=False,
    )
    hybrid_context(
        case(),
        case_graph(graph),
        config=ConditionConfig(reader_context_budget=128, retrieval_limit=4, related_depth=2),
        include_edges=True,
    )

    no_edges_call, with_edges_call = graph.query_calls
    assert no_edges_call["retrieval_mode"] == with_edges_call["retrieval_mode"] == "hybrid"
    assert no_edges_call["max_nodes"] == with_edges_call["max_nodes"] == 4
    assert no_edges_call["query"] == with_edges_call["query"]
    assert no_edges_call["expand_depth"] == 0
    assert with_edges_call["expand_depth"] == 2


def test_real_hybrid_debug_trace_exposes_transcript_and_node_layers() -> None:
    fixture = capability_fixtures()[0]
    graph_payload = build_case_graph(fixture, embedding_model=DeterministicEmbeddingModel())
    try:
        result = production_context(
            fixture,
            graph_payload,
            config=ConditionConfig(reader_context_budget=512, retrieval_limit=5),
            use_prime=False,
            temporal_resolution=True,
        )
    finally:
        graph_payload["tmpdir"].cleanup()

    debug_trace = next(entry for entry in result.tool_trace if entry["tool"] == "debug_retrieval")
    layers = debug_trace["retrieval_layers"]
    assert layers["vector_transcript"]
    assert layers["vector_node"]
    assert debug_trace["hybrid_top_hits"]
    assert any("Phantom Harbor DLC" in hit["content"] for hit in debug_trace["hybrid_top_hits"])


def test_real_build_context_return_enters_final_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from waggle.recursive_context import RecursiveContextController, RecursiveContextResult

    calls = []

    def spy(self, **kwargs):
        calls.append(kwargs)
        return RecursiveContextResult(
            original_query=kwargs["query"],
            context_pack="SPY_BUILD_CONTEXT_OUTPUT",
            token_estimate=5,
            debug={"spy": True},
        )

    monkeypatch.setattr(RecursiveContextController, "build_context", spy)
    fixture = capability_fixtures()[0]
    graph_payload = build_case_graph(fixture, embedding_model=DeterministicEmbeddingModel())
    try:
        result = production_context(
            fixture,
            graph_payload,
            config=ConditionConfig(reader_context_budget=512, retrieval_limit=5),
            use_prime=False,
            temporal_resolution=True,
        )
    finally:
        graph_payload["tmpdir"].cleanup()

    assert calls
    assert calls[0]["query"] == fixture["question"]
    assert "SPY_BUILD_CONTEXT_OUTPUT" in result.context
    assert result.context_items[0].item_type == "context_pack"


def test_oracle_answer_turn_context_centers_late_has_answer_turn() -> None:
    result = oracle_answer_turn_context(
        oracle_case_with_late_answer_turn(),
        case_graph(FakeGraph()),
        config=ConditionConfig(reader_context_budget=160),
    )

    assert result.condition == ORACLE_ANSWER_TURN_CONTEXT
    assert "Ford F-150 pickup truck" in result.context
    assert "has_answer=true" in result.context
    assert result.retrieval_mode == "oracle_answer_turn_gold_support"


def test_oracle_answer_turn_context_query_fallback_for_negative_answers() -> None:
    input_case = {
        "question_id": "negative-support",
        "question_type": "single-session-user",
        "question": "What is the name of my hamster?",
        "answer": "You did not mention a hamster. You mentioned cat Luna.",
        "gold_support_ids": ["s1"],
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "My cat's name is Luna.", "has_answer": False},
                {"role": "assistant", "content": "Luna sounds lovely.", "has_answer": False},
            ]
        ],
    }

    result = oracle_answer_turn_context(
        input_case,
        case_graph(FakeGraph()),
        config=ConditionConfig(reader_context_budget=160),
    )

    assert "Luna" in result.context
    assert result.context_items[0].inclusion_reason == "oracle_query_centered_support"


def test_oracle_answer_turn_context_focuses_inside_long_answer_turn() -> None:
    input_case = {
        "question_id": "long-answer-turn",
        "question_type": "single-session-assistant",
        "question": "What did Borges say about the center and circumference of the Library?",
        "answer": "The Library is a sphere whose exact center is any one of its hexagons and whose circumference is inaccessible.",
        "gold_support_ids": ["s1"],
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [
            [
                {
                    "role": "assistant",
                    "content": (
                        "Introductory discussion about language and reality. " * 80
                        + 'Borges notes, "The Library is a sphere whose exact center is any one of its hexagons '
                        + 'and whose circumference is inaccessible."'
                    ),
                    "has_answer": True,
                }
            ]
        ],
    }

    result = oracle_answer_turn_context(
        input_case,
        case_graph(FakeGraph()),
        config=ConditionConfig(reader_context_budget=160),
    )

    assert "exact center" in result.context
    assert "circumference is inaccessible" in result.context


def test_build_case_graph_preserves_leading_assistant_transcript_turn() -> None:
    input_case = {
        "question_id": "leading-assistant-evidence",
        "question_type": "single-session-assistant",
        "question": "What did Borges say about the center and circumference of the Library?",
        "answer": "The Library is a sphere whose exact center is any one of its hexagons and whose circumference is inaccessible.",
        "gold_support_ids": ["s1"],
        "haystack_session_ids": ["s1"],
        "haystack_dates": ["2023/05/28 (Sun) 05:25"],
        "haystack_sessions": [
            [
                {
                    "role": "assistant",
                    "content": (
                        'Borges notes, "The Library is a sphere whose exact center is any one of its hexagons '
                        'and whose circumference is inaccessible."'
                    ),
                    "has_answer": True,
                },
                {"role": "user", "content": "Can you complete the essay?"},
                {"role": "assistant", "content": "Sure, I can help revise it."},
            ]
        ],
    }

    built = build_case_graph(input_case, embedding_model=DeterministicEmbeddingModel())
    try:
        records = built["graph"].list_transcript_records(
            project=built["project"],
            agent_id=built["agent_id"],
            session_id="s1",
            limit=10,
        )
    finally:
        built["tmpdir"].cleanup()

    assert [record.role for record in records] == ["assistant", "user", "assistant"]
    assert "documentDate: 2023/05/28" in records[0].transcript_text
    assert "exact center is any one of its hexagons" in records[0].transcript_text
    assert records[0].metadata["longmemeval_transcript_only"] is True
    assert built["observe_results"][0]["transcript_only"] is True


def test_build_case_graph_can_reuse_persistent_cache(tmp_path) -> None:
    fixture = capability_fixtures()[0]
    cache_dir = tmp_path / "graph-cache"

    first = build_case_graph(
        fixture,
        embedding_model=DeterministicEmbeddingModel(),
        cache_dir=cache_dir,
    )
    try:
        assert first["graph_cache_hit"] is False
        assert (cache_dir / f"{fixture['question_id']}.db").exists()
        assert (cache_dir / f"{fixture['question_id']}.complete.json").exists()
    finally:
        first["tmpdir"].cleanup()

    second = build_case_graph(
        fixture,
        embedding_model=DeterministicEmbeddingModel(),
        cache_dir=cache_dir,
    )
    try:
        assert second["graph_cache_hit"] is True
        assert second["observe_results"] == []
    finally:
        second["tmpdir"].cleanup()
