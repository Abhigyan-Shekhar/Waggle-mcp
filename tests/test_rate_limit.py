import asyncio
import time
from collections import deque

import pytest

from waggle.errors import RateLimitExceededError
from waggle.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_read_limit():
    limiter = RateLimiter(requests_per_minute=2, max_concurrent_requests=5)
    key = "test_user_read"

    await limiter.check_rate(key, is_write=False)
    await limiter.check_rate(key, is_write=False)

    with pytest.raises(RateLimitExceededError):
        await limiter.check_rate(key, is_write=False)


@pytest.mark.asyncio
async def test_rate_limiter_read_write_independence():
    limiter = RateLimiter(
        requests_per_minute=2,
        max_concurrent_requests=5,
        write_requests_per_minute=2,
    )
    key = "test_user_independent"

    await limiter.check_rate(key, is_write=True)
    await limiter.check_rate(key, is_write=True)

    with pytest.raises(RateLimitExceededError):
        await limiter.check_rate(key, is_write=True)

    await limiter.check_rate(key, is_write=False)
    await limiter.check_rate(key, is_write=False)

    with pytest.raises(RateLimitExceededError):
        await limiter.check_rate(key, is_write=False)


@pytest.mark.asyncio
async def test_rate_limiter_concurrency_slot():
    limiter = RateLimiter(requests_per_minute=10, max_concurrent_requests=2)
    key = "test_user_concurrency"

    async with limiter.concurrency_slot(key):
        async with limiter.concurrency_slot(key):
            with pytest.raises(RateLimitExceededError):
                async with limiter.concurrency_slot(key):
                    pass

        async with limiter.concurrency_slot(key):
            pass


@pytest.mark.asyncio
async def test_concurrency_key_removed_when_count_reaches_zero():
    limiter = RateLimiter(
        requests_per_minute=10,
        max_concurrent_requests=2,
    )

    async with limiter.concurrency_slot("user1"):
        assert "user1" in limiter._concurrent

    assert "user1" not in limiter._concurrent


@pytest.mark.asyncio
async def test_expired_request_window_is_cleaned_up():
    limiter = RateLimiter(
        requests_per_minute=10,
        max_concurrent_requests=2,
    )

    old_time = time.monotonic() - 61
    limiter._request_windows["user1"] = deque([old_time])

    await limiter.check_rate("user1", is_write=False)

    assert "user1" in limiter._request_windows
    assert len(limiter._request_windows["user1"]) == 1


# ── Issue #120: sharded locks must cut cross-key contention while keeping caps ──


def test_same_key_maps_to_same_shard_lock():
    limiter = RateLimiter(requests_per_minute=10, max_concurrent_requests=10, lock_shards=64)
    assert limiter._lock_for("alpha") is limiter._lock_for("alpha")


def test_distinct_keys_spread_across_shards():
    # The whole point of the change: unrelated keys must not all funnel through
    # one lock. With 64 shards, 256 keys have to land on more than one shard.
    limiter = RateLimiter(requests_per_minute=10, max_concurrent_requests=10, lock_shards=64)
    distinct_locks = {id(limiter._lock_for(f"key-{i}")) for i in range(256)}
    assert len(distinct_locks) >= 32


def test_single_shard_degenerates_to_global_lock():
    # lock_shards=1 is the old behaviour; correctness must not depend on sharding.
    limiter = RateLimiter(requests_per_minute=10, max_concurrent_requests=10, lock_shards=1)
    assert limiter._lock_for("a") is limiter._lock_for("b")


@pytest.mark.asyncio
async def test_check_rate_cap_holds_under_concurrent_hammer():

    limit = 50
    limiter = RateLimiter(requests_per_minute=limit, max_concurrent_requests=10_000)
    key = "hammer"
    attempts = 500

    async def attempt() -> bool:
        try:
            await limiter.check_rate(key, is_write=False)
            return True
        except RateLimitExceededError:
            return False

    results = await asyncio.gather(*[attempt() for _ in range(attempts)])

    allowed = sum(results)
    # Exactly `limit` requests get through in a single window — no race lets an
    # extra one slip past the cap.
    assert allowed == limit
    assert len(limiter._request_windows[key]) == limit


@pytest.mark.asyncio
async def test_independent_keys_each_get_full_budget():

    limiter = RateLimiter(requests_per_minute=3, max_concurrent_requests=10)
    keys = [f"user-{i}" for i in range(20)]

    async def fill(key: str) -> int:
        allowed = 0
        for _ in range(5):
            try:
                await limiter.check_rate(key, is_write=False)
                allowed += 1
            except RateLimitExceededError:
                pass
        return allowed

    per_key = await asyncio.gather(*[fill(k) for k in keys])
    # Every key independently receives exactly its own per-minute budget; one
    # key exhausting its window never steals from another.
    assert per_key == [3] * len(keys)


@pytest.mark.asyncio
async def test_concurrency_cap_holds_under_concurrent_hammer():

    cap = 5
    limiter = RateLimiter(requests_per_minute=10_000, max_concurrent_requests=cap)
    key = "slots"
    release = asyncio.Event()
    tally_lock = asyncio.Lock()
    peak = current = admitted = rejected = 0

    async def worker() -> None:
        nonlocal peak, current, admitted, rejected
        try:
            async with limiter.concurrency_slot(key):
                async with tally_lock:
                    admitted += 1
                    current += 1
                    peak = max(peak, current)
                await release.wait()
                async with tally_lock:
                    current -= 1
        except RateLimitExceededError:
            async with tally_lock:
                rejected += 1

    tasks = [asyncio.create_task(worker()) for _ in range(20)]
    await asyncio.sleep(0.05)  # let every worker reach the slot check
    release.set()
    await asyncio.gather(*tasks)

    assert peak <= cap
    assert admitted == cap
    assert rejected == 20 - cap
