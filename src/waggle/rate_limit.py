from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from waggle.errors import RateLimitExceededError

# Number of lock shards. A fixed-size array of locks keeps the number of lock
# objects bounded (unlike per-key locks, which would need their own cleanup map
# and interact badly with the unbounded-map cleanup in the sibling issue) while
# still splitting contention so that requests for unrelated keys do not queue on
# a single global lock. 64 shards is plenty for the concurrency a single MCP
# server instance sees; it can be tuned per instance via the constructor.
DEFAULT_LOCK_SHARDS = 64


class RateLimiter:
    def __init__(
        self,
        *,
        requests_per_minute: int,
        max_concurrent_requests: int,
        write_requests_per_minute: int | None = None,
        lock_shards: int = DEFAULT_LOCK_SHARDS,
    ) -> None:
        self.requests_per_minute = max(requests_per_minute, 1)
        self.max_concurrent_requests = max(max_concurrent_requests, 1)
        self.write_requests_per_minute = write_requests_per_minute or self.requests_per_minute
        # A fixed pool of shard locks. Every operation on a given key always
        # acquires the *same* shard lock (see `_lock_for`), so all critical
        # sections that touch one key's state remain mutually exclusive — the
        # exact serialization guarantee the old single global lock gave, but
        # scoped to a shard instead of the whole limiter.
        self._lock_shards = max(lock_shards, 1)
        self._locks: list[asyncio.Lock] = [asyncio.Lock() for _ in range(self._lock_shards)]
        self._request_windows: dict[str, deque[float]] = defaultdict(deque)
        self._write_windows: dict[str, deque[float]] = defaultdict(deque)
        self._concurrent: dict[str, int] = defaultdict(int)

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Return the shard lock that guards `key`.

        A key is mapped to a shard by its hash, so the same key always resolves
        to the same lock for the lifetime of this limiter. That stability is
        what makes the scheme race-free: two operations on the same key can
        never run their critical sections concurrently, while two operations on
        keys in different shards no longer block each other.
        """
        return self._locks[hash(key) % self._lock_shards]

    async def check_rate(self, key: str, *, is_write: bool) -> None:
        limit = self.write_requests_per_minute if is_write else self.requests_per_minute
        bucket = self._write_windows if is_write else self._request_windows
        now = time.monotonic()

        async with self._lock_for(key):
            window = bucket[key]

            while window and now - window[0] > 60.0:
                window.popleft()

            if not window and key in bucket:
                del bucket[key]

            if len(window) >= limit:
                raise RateLimitExceededError()

            bucket[key].append(now)

    @asynccontextmanager
    async def concurrency_slot(self, key: str):
        lock = self._lock_for(key)
        async with lock:
            if self._concurrent[key] >= self.max_concurrent_requests:
                raise RateLimitExceededError("Too many concurrent requests.")
            self._concurrent[key] += 1
        try:
            yield
        finally:
            async with lock:
                self._concurrent[key] = max(self._concurrent[key] - 1, 0)
                if self._concurrent[key] == 0:
                    del self._concurrent[key]
