"""Measure event-loop stall caused by synchronous ``embed()`` calls.

Part of the *async embedding* work (sub-issue 2 of 2).

We claim that calling ``EmbeddingModel.embed`` synchronously inside an async request handler blocks the event loop, starving lightweight requests (health checks, ``get_stats``).
This module quantifies that stall so the problem is visible and so the thread-pool-executor fix can be shown to help.

How it works
------------
A "heartbeat" coroutine ticks every ``HEARTBEAT_INTERVAL`` seconds and records the largest gap between successive ticks.
Each tick stands in for a lightweight request: the gap is how long such a request would have waited to be serviced.
While the heartbeat runs we drive one embed-heavy ``add_node`` through two code paths:

- ``blocking``: mirrors the current ``graph_create_node`` ASGI handler, which calls ``graph.add_node(...)`` (and therefore ``embed``) directly on the loop.
- ``threaded``: mirrors the proposed fix, wrapping that same call in ``asyncio.to_thread`` so the blocking work runs off the loop.

The real model is never loaded. We use the deterministic embedder and patch ``EmbeddingModel.embed`` to ``time.sleep`` for a fixed interval, so the test
measures event-loop responsiveness (not model speed) and runs anywhere with no 400 MB download.

Run with ``-s`` to see the reported numbers:

    pytest tests/test_embed_event_loop_stall.py -v -s
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from itertools import count

import pytest

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph
from waggle.models import NodeType

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Simulated cost of one synchronous embed forward pass.
EMBED_SLEEP_SECONDS = 0.5
# Heartbeat cadence; also the expected baseline latency of a lightweight request.
HEARTBEAT_INTERVAL = 0.01
# A blocking embed must stall the loop at least this long to count as "blocked".
STALL_FLOOR_SECONDS = 0.2
# Offloaded embed must keep the loop responsive under this ceiling.
# The issue names <50ms as the no-load baseline; 100ms leaves head-room for noisy CI runners while still being far below the nearly 500ms blocking stall.
RESPONSIVE_CEILING_SECONDS = 0.1

# Captured before any patching so the slow wrapper can delegate to it.
_ORIGINAL_EMBED = EmbeddingModel.embed


def _slow_embed(self: EmbeddingModel, text: str, *, wait_timeout: float = 30.0):
    """Stand-in for a slow model: sleep, then return a real (deterministic) vector."""
    time.sleep(EMBED_SLEEP_SECONDS)
    return _ORIGINAL_EMBED(self, text, wait_timeout=wait_timeout)


# ---------------------------------------------------------------------------
# Measurement harness
# ---------------------------------------------------------------------------


def _make_workload(graph: MemoryGraph) -> Callable[[], None]:
    """Return a callable that adds one node with unique content each time.

    Unique content avoids the embed LRU cache short-circuiting the simulated
    sleep, so every call pays the full embed cost.
    """
    counter = count()

    def add_one_node() -> None:
        n = next(counter)
        graph.add_node(
            label=f"stall-probe-{n}",
            content=f"event loop stall probe content number {n}",
            node_type=NodeType.NOTE,
        )

    return add_one_node


async def _measure_loop_stall(graph: MemoryGraph, mode: str) -> float:
    """Run a heartbeat while one workload of *mode* executes; return worst gap (s).

    mode is one of: ``"idle"`` (no embed, baseline jitter), ``"blocking"``
    (embed called directly on the loop), ``"threaded"`` (embed offloaded via
    ``asyncio.to_thread``).
    """
    add_one_node = _make_workload(graph)
    worst_gap = 0.0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal worst_gap
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            now = time.perf_counter()
            worst_gap = max(worst_gap, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(HEARTBEAT_INTERVAL * 3)  # let the baseline settle

    if mode == "idle":
        await asyncio.sleep(EMBED_SLEEP_SECONDS)  # comparable window, no embed
    elif mode == "blocking":
        add_one_node()  # synchronous embed on the loop thread -> stalls the loop
    elif mode == "threaded":
        await asyncio.to_thread(add_one_node)  # embed offloaded to a worker thread
    else:  # pragma: no cover - guard against typos
        raise ValueError(f"unknown mode: {mode!r}")

    await asyncio.sleep(HEARTBEAT_INTERVAL * 3)  # capture trailing ticks
    stop.set()
    await beat
    return worst_gap


# ---------------------------------------------------------------------------
# Fixture: measure all three modes once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stall_ms(tmp_path_factory: pytest.TempPathFactory) -> dict[str, float]:
    """Patch embed to be slow, then measure idle/blocking/threaded stalls (in ms)."""
    EmbeddingModel.embed = _slow_embed  # type: ignore[method-assign]
    try:
        db_path = tmp_path_factory.mktemp("embed-stall") / "memory.db"
        graph = MemoryGraph(db_path, EmbeddingModel("deterministic"))
        results = {
            "idle": asyncio.run(_measure_loop_stall(graph, "idle")) * 1000.0,
            "blocking": asyncio.run(_measure_loop_stall(graph, "blocking")) * 1000.0,
            "threaded": asyncio.run(_measure_loop_stall(graph, "threaded")) * 1000.0,
        }
    finally:
        EmbeddingModel.embed = _ORIGINAL_EMBED  # type: ignore[method-assign]
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_synchronous_embed_stalls_the_event_loop(stall_ms: dict[str, float]) -> None:
    """A direct embed call blocks the loop far beyond the idle baseline."""
    idle = stall_ms["idle"]
    blocking = stall_ms["blocking"]
    # The blocking stall should approach the simulated embed cost and dwarf idle.
    assert blocking >= STALL_FLOOR_SECONDS * 1000.0
    assert blocking > idle * 5.0


def test_threadpool_offload_keeps_loop_responsive(stall_ms: dict[str, float]) -> None:
    """Offloading embed to a worker thread keeps lightweight requests fast."""
    threaded = stall_ms["threaded"]
    assert threaded < RESPONSIVE_CEILING_SECONDS * 1000.0


def test_offload_reduces_stall_versus_blocking(stall_ms: dict[str, float], capsys: pytest.CaptureFixture[str]) -> None:
    """The thread-pool path measurably reduces the stall, and we report numbers."""
    idle = stall_ms["idle"]
    blocking = stall_ms["blocking"]
    threaded = stall_ms["threaded"]

    # Print a report so the numbers land in the captured test output.
    with capsys.disabled():
        print()
        print("event-loop stall while one embed (~%.0f ms) runs:" % (EMBED_SLEEP_SECONDS * 1000.0))
        print(f"  idle baseline (no embed)      : {idle:8.1f} ms")
        print(f"  synchronous embed (blocking)  : {blocking:8.1f} ms")
        print(f"  thread-pool offload (fixed)   : {threaded:8.1f} ms")
        print(f"  stall reduction               : {blocking / max(threaded, 1e-9):7.1f}x")

    # The fix must cut the stall by at least half and by a large absolute margin.
    assert blocking > threaded * 2.0
    assert (blocking - threaded) > STALL_FLOOR_SECONDS * 1000.0
