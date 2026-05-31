import pytest

from waggle.errors import RateLimitExceededError
from waggle.rate_limit import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_read_limit():
    """Assert that calling check_rate for reads up to requests_per_minute succeeds,
    and the next call raises RateLimitExceededError.
    """
    limiter = RateLimiter(requests_per_minute=2, max_concurrent_requests=5)
    key = "test_user_read"

    # First two read requests must succeed
    await limiter.check_rate(key, is_write=False)
    await limiter.check_rate(key, is_write=False)

    # The 3rd request must exceed the limit
    with pytest.raises(RateLimitExceededError):
        await limiter.check_rate(key, is_write=False)


@pytest.mark.asyncio
async def test_rate_limiter_read_write_independence():
    """Assert that read and write windows are independent:
    hitting the write limit does not block reads, and vice-versa.
    """
    limiter = RateLimiter(requests_per_minute=2, max_concurrent_requests=5, write_requests_per_minute=2)
    key = "test_user_independent"

    # 1. Hit the write limit completely (2 requests)
    await limiter.check_rate(key, is_write=True)
    await limiter.check_rate(key, is_write=True)

    with pytest.raises(RateLimitExceededError):
        await limiter.check_rate(key, is_write=True)

    # 2. Assert that reads STILL work completely fine because windows are independent
    await limiter.check_rate(key, is_write=False)
    await limiter.check_rate(key, is_write=False)

    with pytest.raises(RateLimitExceededError):
        await limiter.check_rate(key, is_write=False)


@pytest.mark.asyncio
async def test_rate_limiter_concurrency_slot():
    """Assert concurrency_slot raises RateLimitExceededError once max_concurrent_requests slots
    are held for a key, and releasing a slot frees up capacity immediately.
    """
    limiter = RateLimiter(requests_per_minute=10, max_concurrent_requests=2)
    key = "test_user_concurrency"

    # Acquire slot 1
    async with limiter.concurrency_slot(key):
        # Acquire slot 2 (Max capacity reached)
        async with limiter.concurrency_slot(key):
            # The 3rd simultaneous request must fail
            with pytest.raises(RateLimitExceededError):
                async with limiter.concurrency_slot(key):
                    pass

        # Exiting slot 2's context manager drops active count to 1.
        # We should now be able to successfully acquire a slot again.
        async with limiter.concurrency_slot(key):
            pass
