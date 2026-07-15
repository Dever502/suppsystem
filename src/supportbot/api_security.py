from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _RateWindow:
    hits: deque[float] = field(default_factory=deque)
    last_seen: float = 0.0


class InMemoryRateLimiter:
    """Bounded single-process sliding-window limiter for the MVP API."""

    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 10_000) -> None:
        if limit <= 0 or window_seconds <= 0 or max_keys <= 0:
            raise ValueError("rate limiter values must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._windows: dict[str, _RateWindow] = {}
        self._lock = asyncio.Lock()

    async def consume(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            window = self._windows.get(key)
            if window is None:
                self._evict_stale(cutoff)
                if len(self._windows) >= self.max_keys:
                    oldest_key = min(
                        self._windows,
                        key=lambda candidate: self._windows[candidate].last_seen,
                    )
                    del self._windows[oldest_key]
                window = _RateWindow()
                self._windows[key] = window
            while window.hits and window.hits[0] <= cutoff:
                window.hits.popleft()
            window.last_seen = now
            if len(window.hits) >= self.limit:
                retry_after = max(1, int(window.hits[0] + self.window_seconds - now + 0.999))
                return False, retry_after
            window.hits.append(now)
            return True, 0

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._windows.pop(key, None)

    def _evict_stale(self, cutoff: float) -> None:
        stale_keys = [key for key, window in self._windows.items() if window.last_seen <= cutoff]
        for key in stale_keys:
            del self._windows[key]
