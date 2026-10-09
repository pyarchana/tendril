"""A small in-memory sliding-window limiter. One server, one gardener: no shared store needed."""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, window_seconds: float = 3600.0):
        self.window = window_seconds
        self._hits: dict[object, deque[float]] = defaultdict(deque)

    def check(self, key: object, limit: int, now: float | None = None) -> float | None:
        """Record a hit and return None, or return the seconds to wait if `key` is over `limit`."""
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= limit:
            return self.window - (now - hits[0])
        hits.append(now)
        return None
