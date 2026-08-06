from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class UserMessageRateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    notify_client: bool = False


@dataclass
class _UserMessageRateWindow:
    minute_hits: deque[float] = field(default_factory=deque)
    hour_hits: deque[float] = field(default_factory=deque)
    last_seen: float = 0.0
    last_notice_at: float | None = None


class UserMessageRateLimiter:
    """Process-local per-user admission limiter shared by Telegram and Web."""

    def __init__(
        self,
        *,
        per_minute: int = 30,
        per_hour: int = 200,
        notice_interval_seconds: float = 60.0,
        max_users: int = 20_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute <= 0 or per_hour < per_minute:
            raise ValueError("user message limits must be positive and hourly must cover burst")
        if notice_interval_seconds <= 0 or max_users <= 0:
            raise ValueError("notice interval and max_users must be positive")
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.notice_interval_seconds = notice_interval_seconds
        self.max_users = max_users
        self._monotonic = monotonic
        self._windows: dict[str, _UserMessageRateWindow] = {}
        self._lock = asyncio.Lock()

    async def consume(self, identity_key: str) -> UserMessageRateLimitDecision:
        if not identity_key:
            raise ValueError("identity_key must not be empty")
        now = self._monotonic()
        minute_cutoff = now - 60.0
        hour_cutoff = now - 3600.0
        async with self._lock:
            window = self._windows.get(identity_key)
            if window is None:
                self._evict_stale(hour_cutoff)
                if len(self._windows) >= self.max_users:
                    oldest_key = min(
                        self._windows,
                        key=lambda candidate: self._windows[candidate].last_seen,
                    )
                    del self._windows[oldest_key]
                window = _UserMessageRateWindow()
                self._windows[identity_key] = window

            self._discard_expired(window.minute_hits, minute_cutoff)
            self._discard_expired(window.hour_hits, hour_cutoff)
            window.last_seen = now
            minute_blocked = len(window.minute_hits) >= self.per_minute
            hour_blocked = len(window.hour_hits) >= self.per_hour
            if not minute_blocked and not hour_blocked:
                window.minute_hits.append(now)
                window.hour_hits.append(now)
                return UserMessageRateLimitDecision(allowed=True)

            retry_at = []
            if minute_blocked:
                retry_at.append(window.minute_hits[0] + 60.0)
            if hour_blocked:
                retry_at.append(window.hour_hits[0] + 3600.0)
            retry_after = max(1, int(max(retry_at) - now + 0.999))
            notify_client = (
                window.last_notice_at is None
                or now - window.last_notice_at >= self.notice_interval_seconds
            )
            if notify_client:
                window.last_notice_at = now
            return UserMessageRateLimitDecision(
                allowed=False,
                retry_after_seconds=retry_after,
                notify_client=notify_client,
            )

    @staticmethod
    def _discard_expired(hits: deque[float], cutoff: float) -> None:
        while hits and hits[0] <= cutoff:
            hits.popleft()

    def _evict_stale(self, cutoff: float) -> None:
        stale_keys = [key for key, window in self._windows.items() if window.last_seen <= cutoff]
        for key in stale_keys:
            del self._windows[key]
