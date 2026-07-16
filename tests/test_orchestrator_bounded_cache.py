"""Tests for the bounded _known_turn_ids dedup cache in AsyncMemoryOrchestrator."""

from __future__ import annotations

import pytest

from waggle.orchestrator import (
    AsyncMemoryOrchestrator,
    ConversationTurn,
    IngestPlan,
    MemoryPolicy,
    MemoryScope,
)


class _StubGraph:
    """Minimal GraphBackend stub for orchestrator tests."""

    def __init__(self) -> None:
        self.ingested: list[str] = []

    def observe_conversation(self, **kwargs: object) -> None:  # type: ignore[override]
        self.ingested.append(kwargs.get("user_message", ""))  # type: ignore[arg-type]

    def query(self, **kwargs: object) -> None:  # type: ignore[override]
        pass

    def prime_context(self, **kwargs: object) -> None:  # type: ignore[override]
        pass


def _scope() -> MemoryScope:
    return MemoryScope(tenant_id="t", project="p", agent_id="a", session_id="s", model_id="m")


def _turn(i: int) -> ConversationTurn:
    return ConversationTurn(
        user_message=f"user message {i} with enough text to pass the durable filter",
        assistant_response=f"assistant decision {i} — must remember this requirement",
        turn_id=f"turn_{i}",
    )


class _AlwaysIngestPolicy(MemoryPolicy):
    """Policy that always ingests so we can test the dedup layer in isolation."""

    def plan_ingest(self, turn: ConversationTurn, scope: MemoryScope) -> IngestPlan:
        return IngestPlan(should_ingest=True, reason="test")


@pytest.mark.asyncio
async def test_known_turn_ids_bounded() -> None:
    """After exceeding known_turns_maxsize, the oldest entries are evicted."""
    maxsize = 10
    graph = _StubGraph()
    orch = AsyncMemoryOrchestrator(
        graph,
        policy=_AlwaysIngestPolicy(),
        queue_maxsize=200,
        known_turns_maxsize=maxsize,
    )
    await orch.start()

    scope = _scope()

    # Ingest more turns than maxsize
    for i in range(maxsize + 5):
        await orch.on_assistant_turn(scope=scope, turn=_turn(i))

    # The dedup cache should be capped at maxsize
    assert len(orch._known_turn_ids) <= maxsize

    await orch.stop()


@pytest.mark.asyncio
async def test_duplicate_turn_rejected() -> None:
    """A turn with the same turn_id is rejected as a duplicate."""
    graph = _StubGraph()
    orch = AsyncMemoryOrchestrator(
        graph,
        policy=_AlwaysIngestPolicy(),
        queue_maxsize=200,
    )
    await orch.start()

    scope = _scope()
    turn = _turn(0)

    plan1 = await orch.on_assistant_turn(scope=scope, turn=turn)
    assert plan1.should_ingest is True

    plan2 = await orch.on_assistant_turn(scope=scope, turn=turn)
    assert plan2.should_ingest is False
    assert plan2.reason == "duplicate turn"

    await orch.stop()


@pytest.mark.asyncio
async def test_evicted_turn_can_be_reingested() -> None:
    """After eviction from the bounded cache, a turn can be ingested again."""
    maxsize = 5
    graph = _StubGraph()
    orch = AsyncMemoryOrchestrator(
        graph,
        policy=_AlwaysIngestPolicy(),
        queue_maxsize=200,
        known_turns_maxsize=maxsize,
    )
    await orch.start()

    scope = _scope()

    # Ingest turn_0
    plan_first = await orch.on_assistant_turn(scope=scope, turn=_turn(0))
    assert plan_first.should_ingest is True

    # Fill the cache to evict turn_0
    for i in range(1, maxsize + 2):
        await orch.on_assistant_turn(scope=scope, turn=_turn(i))

    # turn_0 should have been evicted — re-ingesting should succeed
    assert "turn_0" not in orch._known_turn_ids
    plan_reingest = await orch.on_assistant_turn(scope=scope, turn=_turn(0))
    assert plan_reingest.should_ingest is True

    await orch.stop()


def test_invalid_known_turns_maxsize() -> None:
    """A ValueError is raised if known_turns_maxsize is < 1."""
    graph = _StubGraph()
    with pytest.raises(ValueError, match="known_turns_maxsize must be >= 1"):
        AsyncMemoryOrchestrator(
            graph,
            policy=_AlwaysIngestPolicy(),
            known_turns_maxsize=0,
        )

    with pytest.raises(ValueError, match="known_turns_maxsize must be >= 1"):
        AsyncMemoryOrchestrator(
            graph,
            policy=_AlwaysIngestPolicy(),
            known_turns_maxsize=-5,
        )
