from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from waggle.orchestrator import (
    AsyncMemoryOrchestrator,
    ConversationTurn,
    MemoryPolicy,
    MemoryScope,
    RetrieveRequest,
)


@dataclass
class FakeGraph:
    observed: list[dict[str, str]] = field(default_factory=list)
    queries: list[dict[str, object]] = field(default_factory=list)

    def observe_conversation(
        self,
        *,
        user_message: str,
        assistant_response: str,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> dict[str, object]:
        self.observed.append(
            {
                "user_message": user_message,
                "assistant_response": assistant_response,
                "agent_id": agent_id,
                "project": project,
                "session_id": session_id,
            }
        )
        return {"ok": True}

    def query(
        self,
        *,
        query: str,
        max_nodes: int = 20,
        max_depth: int = 2,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        retrieval_mode: str = "graph",
    ) -> dict[str, object]:
        payload = {
            "query": query,
            "max_nodes": max_nodes,
            "max_depth": max_depth,
            "agent_id": agent_id,
            "project": project,
            "session_id": session_id,
            "retrieval_mode": retrieval_mode,
        }
        self.queries.append(payload)
        return payload

    def prime_context(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 25,
    ) -> dict[str, object]:
        return {
            "project": project,
            "agent_id": agent_id,
            "session_id": session_id,
            "max_nodes": max_nodes,
        }


@pytest.mark.asyncio
async def test_on_assistant_turn_enqueues_and_ingests() -> None:
    graph = FakeGraph()
    orchestrator = AsyncMemoryOrchestrator(graph)
    scope = MemoryScope(project="MCP", session_id="thread-1", agent_id="codex")
    turn = ConversationTurn(
        user_message="Please remember that we decided to publish to MCP registry.",
        assistant_response="Decision captured. We'll publish to registry and keep pyproject in sync.",
    )

    await orchestrator.start()
    try:
        plan = await orchestrator.on_assistant_turn(scope=scope, turn=turn)
        assert plan.should_ingest is True
        await asyncio.wait_for(orchestrator.flush(), timeout=2)
    finally:
        await orchestrator.stop()

    assert len(graph.observed) == 1
    observed = graph.observed[0]
    assert observed["project"] == "MCP"
    assert observed["session_id"] == "thread-1"


@pytest.mark.asyncio
async def test_on_assistant_turn_deduplicates_same_turn() -> None:
    graph = FakeGraph()
    orchestrator = AsyncMemoryOrchestrator(graph)
    scope = MemoryScope(project="MCP", session_id="thread-1", agent_id="codex")
    turn = ConversationTurn(
        user_message="Remember that MCP registry publishing needs server.json.",
        assistant_response="I'll keep that registry requirement in memory.",
    )

    await orchestrator.start()
    try:
        first = await orchestrator.on_assistant_turn(scope=scope, turn=turn)
        second = await orchestrator.on_assistant_turn(scope=scope, turn=turn)
        await asyncio.wait_for(orchestrator.flush(), timeout=2)
    finally:
        await orchestrator.stop()

    assert first.should_ingest is True
    assert second.should_ingest is False
    assert second.reason == "duplicate turn"
    assert len(graph.observed) == 1


@pytest.mark.asyncio
async def test_build_context_applies_token_budget() -> None:
    graph = FakeGraph()
    orchestrator = AsyncMemoryOrchestrator(graph)
    scope = MemoryScope(project="MCP", session_id="thread-2", agent_id="codex")

    result = await orchestrator.build_context(
        scope=scope,
        request=RetrieveRequest(
            query="what did we decide about pypi and registry publishing",
            max_context_tokens=340,
            max_nodes=20,
            max_depth=2,
        ),
    )

    assert result is not None
    assert len(graph.queries) == 1
    query_call = graph.queries[0]
    assert query_call["max_nodes"] == 4
    assert query_call["max_depth"] == 1
    assert query_call["project"] == "MCP"


def test_memory_policy_is_durable_only_by_default() -> None:
    policy = MemoryPolicy()
    scope = MemoryScope(project="MCP", session_id="thread-3", agent_id="codex")

    durable = policy.plan_ingest(
        ConversationTurn(
            user_message="We decided to publish the package after CI passes.",
            assistant_response="Understood. I'll remember that release decision.",
        ),
        scope,
    )
    filler = policy.plan_ingest(
        ConversationTurn(
            user_message="That walkthrough was really interesting and detailed.",
            assistant_response="Glad it helped. Let me know if you want more examples.",
        ),
        scope,
    )

    assert durable.should_ingest is True
    assert durable.reason == "durable signal detected"
    assert filler.should_ingest is False
    assert filler.reason == "durable-only policy: no durable signal"


@pytest.mark.asyncio
async def test_on_assistant_turn_drops_when_queue_full() -> None:
    """Queue-full: when the ingest queue is at capacity, the turn is dropped with reason 'queue full'."""
    graph = FakeGraph()
    # maxsize=1 so the queue fills after one item; do NOT start the worker so nothing drains
    orchestrator = AsyncMemoryOrchestrator(graph, queue_maxsize=1)

    scope = MemoryScope(project="MCP", session_id="thread-full", agent_id="codex")

    def _durable_turn(n: int) -> ConversationTurn:
        return ConversationTurn(
            user_message=f"We decided to always remember policy rule {n}.",
            assistant_response=f"Understood, policy rule {n} is stored.",
            turn_id=f"turn-{n}",
        )

    # Fill the queue (worker not started, so nothing drains)
    first = await orchestrator.on_assistant_turn(scope=scope, turn=_durable_turn(1))
    # Second enqueue should hit queue full
    second = await orchestrator.on_assistant_turn(scope=scope, turn=_durable_turn(2))

    assert first.should_ingest is True
    assert second.should_ingest is False
    assert second.reason == "queue full"

    # Clean up without flushing since worker was never started
    await orchestrator.stop()


@pytest.mark.asyncio
async def test_on_assistant_turn_deduplicates_by_explicit_turn_id() -> None:
    """Duplicate-turn: explicit turn_id takes priority over content hash for dedup."""
    graph = FakeGraph()
    orchestrator = AsyncMemoryOrchestrator(graph)
    scope = MemoryScope(project="MCP", session_id="thread-dedup", agent_id="codex")

    # Same turn_id, different content — should still dedup on turn_id
    turn_a = ConversationTurn(
        user_message="We must always require code review before merging.",
        assistant_response="Noted. Code review is required before any merge.",
        turn_id="explicit-turn-42",
    )
    turn_b = ConversationTurn(
        user_message="Different message content but same turn_id.",
        assistant_response="This should be treated as a duplicate.",
        turn_id="explicit-turn-42",
    )

    await orchestrator.start()
    try:
        first = await orchestrator.on_assistant_turn(scope=scope, turn=turn_a)
        second = await orchestrator.on_assistant_turn(scope=scope, turn=turn_b)
        await asyncio.wait_for(orchestrator.flush(), timeout=2)
    finally:
        await orchestrator.stop()

    assert first.should_ingest is True
    assert second.should_ingest is False
    assert second.reason == "duplicate turn"
    assert len(graph.observed) == 1


@pytest.mark.asyncio
async def test_flush_drains_all_pending_turns() -> None:
    """Flush: all enqueued turns are fully processed before flush() returns."""
    graph = FakeGraph()
    orchestrator = AsyncMemoryOrchestrator(graph, queue_maxsize=64)
    scope = MemoryScope(project="MCP", session_id="thread-flush", agent_id="codex")

    turns = [
        ConversationTurn(
            user_message=f"We must always implement requirement {i}.",
            assistant_response=f"Requirement {i} noted and stored.",
            turn_id=f"flush-turn-{i}",
        )
        for i in range(5)
    ]

    await orchestrator.start()
    try:
        for turn in turns:
            await orchestrator.on_assistant_turn(scope=scope, turn=turn)

        # flush() must block until all 5 are processed
        await asyncio.wait_for(orchestrator.flush(), timeout=5)
    finally:
        await orchestrator.stop()

    assert len(graph.observed) == 5
