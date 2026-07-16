from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from waggle.models import ObservationResult, PrimeContextResult, SubgraphResult

LOGGER = logging.getLogger(__name__)


class GraphBackend(Protocol):
    def observe_conversation(
        self,
        *,
        user_message: str,
        assistant_response: str,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> ObservationResult: ...

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
    ) -> SubgraphResult: ...

    def prime_context(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_nodes: int = 25,
    ) -> PrimeContextResult: ...


@dataclass(slots=True, frozen=True)
class MemoryScope:
    tenant_id: str = "local-default"
    project: str = ""
    agent_id: str = ""
    session_id: str = ""
    model_id: str = ""


@dataclass(slots=True, frozen=True)
class ConversationTurn:
    user_message: str
    assistant_response: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    turn_id: str = ""


@dataclass(slots=True, frozen=True)
class RetrieveRequest:
    query: str
    max_context_tokens: int = 1200
    retrieval_mode: str = "graph"
    max_nodes: int = 12
    max_depth: int = 2


@dataclass(slots=True, frozen=True)
class RetrievePlan:
    should_retrieve: bool
    reason: str
    request: RetrieveRequest | None = None


@dataclass(slots=True, frozen=True)
class IngestPlan:
    should_ingest: bool
    reason: str


@dataclass(slots=True)
class MemoryPolicy:
    min_text_len: int = 16
    min_turn_interval_seconds: int = 6
    durable_markers: tuple[str, ...] = (
        "decide",
        "decision",
        "prefer",
        "must",
        "should",
        "constraint",
        "require",
        "requirement",
        "remember",
        "always",
        "never",
        "policy",
        "ship",
        "publish",
        "fix",
        "fixed",
        "implement",
        "implemented",
        "outcome",
    )
    skip_markers: tuple[str, ...] = (
        "thanks",
        "thank you",
        "lol",
        "ok",
        "okay",
        "cool",
        "bro",
    )
    last_ingest_at: dict[MemoryScope, datetime] = field(default_factory=dict)

    def plan_ingest(self, turn: ConversationTurn, scope: MemoryScope) -> IngestPlan:
        user = turn.user_message.strip().lower()
        assistant = turn.assistant_response.strip().lower()
        if not user or not assistant:
            return IngestPlan(False, "empty turn")

        if len(user) < self.min_text_len and len(assistant) < self.min_text_len:
            return IngestPlan(False, "turn too short")

        if any(user == marker for marker in self.skip_markers):
            return IngestPlan(False, "chatty acknowledgement")

        last = self.last_ingest_at.get(scope)
        if last is not None and (turn.occurred_at - last) < timedelta(seconds=self.min_turn_interval_seconds):
            return IngestPlan(False, "ingest interval guard")

        combined = f"{user}\n{assistant}"
        if any(marker in combined for marker in self.durable_markers):
            return IngestPlan(True, "durable signal detected")

        return IngestPlan(False, "durable-only policy: no durable signal")

    def mark_ingested(self, scope: MemoryScope, at: datetime) -> None:
        self.last_ingest_at[scope] = at

    def plan_retrieve(self, request: RetrieveRequest) -> RetrievePlan:
        query = request.query.strip()
        if not query:
            return RetrievePlan(False, "empty query")
        if len(query) < 3:
            return RetrievePlan(False, "query too short")
        return RetrievePlan(True, "retrieve", request=request)


@dataclass(slots=True)
class IngestTask:
    scope: MemoryScope
    turn: ConversationTurn


# Default cap for the dedup cache.  Each entry is a 64-char hex SHA-256
# digest (~120 bytes with Python object overhead), so 50 000 entries ≈ 3.2 MB.
DEFAULT_KNOWN_TURNS_MAXSIZE = 50_000


class AsyncMemoryOrchestrator:
    """
    Event-driven memory orchestration for chat runtimes.

    Intended integration points:
    1. Call `on_assistant_turn` after each response to enqueue async ingestion.
    2. Call `build_context` before the next model invocation to retrieve scoped memory.
    """

    def __init__(
        self,
        graph: GraphBackend,
        *,
        policy: MemoryPolicy | None = None,
        queue_maxsize: int = 256,
        known_turns_maxsize: int = DEFAULT_KNOWN_TURNS_MAXSIZE,
    ) -> None:
        if known_turns_maxsize < 1:
            raise ValueError("known_turns_maxsize must be >= 1")
        self.graph = graph
        self.policy = policy or MemoryPolicy()
        self._queue: asyncio.Queue[IngestTask] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker_task: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()
        self._known_turns_maxsize = known_turns_maxsize
        # Bounded dedup cache: OrderedDict gives O(1) membership + LRU eviction.
        # When the cache is full the oldest entry is evicted.  A very old turn
        # re-appearing after eviction would be re-ingested, which is safe
        # because observe_conversation is idempotent at the graph layer.
        self._known_turn_ids: collections.OrderedDict[str, None] = collections.OrderedDict()

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._closing.clear()
        self._worker_task = asyncio.create_task(self._worker(), name="waggle-memory-orchestrator")

    async def stop(self) -> None:
        self._closing.set()
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    async def flush(self) -> None:
        await self._queue.join()

    async def on_assistant_turn(self, *, scope: MemoryScope, turn: ConversationTurn) -> IngestPlan:
        plan = self.policy.plan_ingest(turn, scope)
        if not plan.should_ingest:
            return plan
        turn_key = self._turn_key(scope, turn)
        if turn_key in self._known_turn_ids:
            # Move to end so recently-seen keys survive eviction longer.
            self._known_turn_ids.move_to_end(turn_key)
            return IngestPlan(False, "duplicate turn")
        if self._queue.full():
            LOGGER.warning("memory ingest queue full; dropping turn for session '%s'", scope.session_id)
            return IngestPlan(False, "queue full")
        self._known_turn_ids[turn_key] = None
        # Evict oldest entries when the cache exceeds the configured cap.
        while len(self._known_turn_ids) > self._known_turns_maxsize:
            self._known_turn_ids.popitem(last=False)
        await self._queue.put(IngestTask(scope=scope, turn=turn))
        return plan

    async def build_context(self, *, scope: MemoryScope, request: RetrieveRequest) -> SubgraphResult | None:
        plan = self.policy.plan_retrieve(request)
        if not plan.should_retrieve or plan.request is None:
            return None
        bounded = self._fit_budget(plan.request)
        return await asyncio.to_thread(
            self.graph.query,
            query=bounded.query,
            max_nodes=bounded.max_nodes,
            max_depth=bounded.max_depth,
            retrieval_mode=bounded.retrieval_mode,
            project=scope.project,
            agent_id=scope.agent_id,
            session_id=scope.session_id,
        )

    async def build_prime(self, *, scope: MemoryScope, max_nodes: int = 12) -> PrimeContextResult:
        return await asyncio.to_thread(
            self.graph.prime_context,
            project=scope.project,
            agent_id=scope.agent_id,
            session_id=scope.session_id,
            max_nodes=max_nodes,
        )

    async def _worker(self) -> None:
        while not self._closing.is_set():
            task = await self._queue.get()
            try:
                await asyncio.to_thread(
                    self.graph.observe_conversation,
                    user_message=task.turn.user_message,
                    assistant_response=task.turn.assistant_response,
                    project=task.scope.project,
                    agent_id=task.scope.agent_id,
                    session_id=task.scope.session_id,
                )
                self.policy.mark_ingested(task.scope, task.turn.occurred_at)
            except Exception:
                LOGGER.exception("memory ingest failed for session '%s'", task.scope.session_id)
            finally:
                self._queue.task_done()

    def _fit_budget(self, request: RetrieveRequest) -> RetrieveRequest:
        token_budget = max(200, request.max_context_tokens)
        # A conservative heuristic: reserve ~85 tokens per node in the returned context.
        budget_nodes = max(3, min(request.max_nodes, token_budget // 85))
        budget_depth = request.max_depth if budget_nodes >= 6 else min(request.max_depth, 1)
        return RetrieveRequest(
            query=request.query,
            max_context_tokens=request.max_context_tokens,
            retrieval_mode=request.retrieval_mode,
            max_nodes=budget_nodes,
            max_depth=budget_depth,
        )

    def _turn_key(self, scope: MemoryScope, turn: ConversationTurn) -> str:
        if turn.turn_id.strip():
            return turn.turn_id.strip()
        raw = "\n".join(
            [
                scope.tenant_id,
                scope.project,
                scope.agent_id,
                scope.session_id,
                scope.model_id,
                turn.user_message.strip(),
                turn.assistant_response.strip(),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
