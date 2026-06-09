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
