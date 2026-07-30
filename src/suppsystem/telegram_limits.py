from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TelegramInboundRateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    notify_user: bool = False


@dataclass
class _TelegramInboundRateWindow:
    minute_hits: deque[float] = field(default_factory=deque)
    hour_hits: deque[float] = field(default_factory=deque)
    last_seen: float = 0.0
    last_notice_at: float | None = None


class TelegramInboundRateLimiter:
    """Per-user admission limiter for private messages before durable persistence."""

    def __init__(
        self,
        *,
        per_minute: int = 30,
        per_hour: int = 150,
        notice_interval_seconds: float = 60.0,
        max_users: int = 10_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute <= 0 or per_hour < per_minute:
            raise ValueError("inbound rate limits must be positive and hourly must cover burst")
        if notice_interval_seconds <= 0 or max_users <= 0:
            raise ValueError("notice interval and max_users must be positive")
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.notice_interval_seconds = notice_interval_seconds
        self.max_users = max_users
        self._monotonic = monotonic
        self._windows: dict[int, _TelegramInboundRateWindow] = {}
        self._lock = asyncio.Lock()

    async def consume(self, telegram_user_id: int) -> TelegramInboundRateLimitDecision:
        now = self._monotonic()
        minute_cutoff = now - 60.0
        hour_cutoff = now - 3600.0
        async with self._lock:
            window = self._windows.get(telegram_user_id)
            if window is None:
                self._evict_stale(hour_cutoff)
                if len(self._windows) >= self.max_users:
                    oldest_user_id = min(
                        self._windows,
                        key=lambda candidate: self._windows[candidate].last_seen,
                    )
                    del self._windows[oldest_user_id]
                window = _TelegramInboundRateWindow()
                self._windows[telegram_user_id] = window

            self._discard_expired(window.minute_hits, minute_cutoff)
            self._discard_expired(window.hour_hits, hour_cutoff)
            window.last_seen = now
            minute_blocked = len(window.minute_hits) >= self.per_minute
            hour_blocked = len(window.hour_hits) >= self.per_hour
            if not minute_blocked and not hour_blocked:
                window.minute_hits.append(now)
                window.hour_hits.append(now)
                return TelegramInboundRateLimitDecision(allowed=True)

            retry_at = []
            if minute_blocked:
                retry_at.append(window.minute_hits[0] + 60.0)
            if hour_blocked:
                retry_at.append(window.hour_hits[0] + 3600.0)
            retry_after = max(1, int(max(retry_at) - now + 0.999))
            notify_user = (
                window.last_notice_at is None
                or now - window.last_notice_at >= self.notice_interval_seconds
            )
            if notify_user:
                window.last_notice_at = now
            return TelegramInboundRateLimitDecision(
                allowed=False,
                retry_after_seconds=retry_after,
                notify_user=notify_user,
            )

    @staticmethod
    def _discard_expired(hits: deque[float], cutoff: float) -> None:
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _evict_stale(self, cutoff: float) -> None:
        stale_user_ids = [
            user_id for user_id, window in self._windows.items() if window.last_seen <= cutoff
        ]
        for user_id in stale_user_ids:
            del self._windows[user_id]


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
