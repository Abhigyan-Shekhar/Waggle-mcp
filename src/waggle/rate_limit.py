from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from waggle.errors import RateLimitExceededError


class RateLimiter:
    def __init__(
        self,
        *,
        requests_per_minute: int,
        max_concurrent_requests: int,
        write_requests_per_minute: int | None = None,
    ) -> None:
        self.requests_per_minute = max(requests_per_minute, 1)
        self.max_concurrent_requests = max(max_concurrent_requests, 1)
        self.write_requests_per_minute = write_requests_per_minute or self.requests_per_minute
        self._lock = asyncio.Lock()
        self._request_windows: dict[str, deque[float]] = defaultdict(deque)
        self._write_windows: dict[str, deque[float]] = defaultdict(deque)
        self._concurrent: dict[str, int] = defaultdict(int)

    async def check_rate(self, key: str, *, is_write: bool) -> None:
        limit = self.write_requests_per_minute if is_write else self.requests_per_minute
        bucket = self._write_windows if is_write else self._request_windows
        now = time.monotonic()

        async with self._lock:
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
        async with self._lock:
            if self._concurrent[key] >= self.max_concurrent_requests:
                raise RateLimitExceededError("Too many concurrent requests.")
            self._concurrent[key] += 1
        try:
            yield
        finally:
            async with self._lock:
                self._concurrent[key] = max(self._concurrent[key] - 1, 0)
                if self._concurrent[key] == 0:
                    del self._concurrent[key]
