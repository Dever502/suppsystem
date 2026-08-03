from __future__ import annotations

import asyncio
import time


class TelegramRateLimiter:
    """A process-wide limiter for outgoing Telegram API calls in the single-instance MVP."""

    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = minimum_interval_seconds
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed_at - now)
            if delay:
                await asyncio.sleep(delay)
            self._next_allowed_at = time.monotonic() + self.minimum_interval_seconds

    async def defer(self, seconds: float) -> None:
        async with self._lock:
            self._next_allowed_at = max(self._next_allowed_at, time.monotonic() + seconds)
