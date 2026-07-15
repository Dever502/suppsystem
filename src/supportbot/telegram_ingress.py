from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import TelegramObject, Update

from supportbot.durable_work import DurableWorkRepository
from supportbot.runtime_health import RuntimeHealth

logger = logging.getLogger(__name__)


class DurableTelegramIngressMiddleware(BaseMiddleware):
    """Commit polling updates before allowing Telegram's offset to advance."""

    def __init__(self, repository: DurableWorkRepository, wake_worker: Callable[[], None]) -> None:
        self.repository = repository
        self.wake_worker = wake_worker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if data.get("durable_replay") is True:
            return await handler(event, data)
        if not isinstance(event, Update):
            raise TypeError("durable ingress expects an aiogram Update")
        payload = event.model_dump(mode="json", exclude_none=True)
        await self.repository.enqueue_inbound_update(event.update_id, payload)
        self.wake_worker()
        return None


class TelegramIngressWorker:
    def __init__(
        self,
        *,
        bot: Bot,
        dispatcher: Dispatcher,
        repository: DurableWorkRepository,
        runtime_health: RuntimeHealth | None = None,
        poll_interval_seconds: float = 0.25,
        stale_recovery_interval_seconds: float = 60.0,
    ) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.repository = repository
        self.runtime_health = runtime_health
        self.poll_interval_seconds = poll_interval_seconds
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self._stopped = asyncio.Event()
        self._wake = asyncio.Event()
        self._next_stale_recovery_at = 0.0

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    async def run(self) -> None:
        if self.runtime_health is not None:
            self.runtime_health.starting("telegram_ingress")
        while not self._stopped.is_set():
            try:
                await self._release_stale_if_due()
                job = await self.repository.claim_inbound_update()
                self._progress()
                if job is None:
                    await self._wait()
                    continue
                try:
                    update = Update.model_validate(job.payload, context={"bot": self.bot})
                    await self.dispatcher.feed_update(
                        self.bot,
                        update,
                        durable_replay=True,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await self.repository.retry_inbound_update(job, str(error))
                    logger.exception(
                        "Durable Telegram update failed; retry scheduled",
                        extra={
                            "event": "telegram_inbound_retry",
                            "telegram_update_id": job.telegram_update_id,
                            "attempt_count": job.attempt_count,
                        },
                    )
                else:
                    await self.repository.finish_inbound_update(job)
                self._progress()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.runtime_health is not None:
                    self.runtime_health.degraded("telegram_ingress")
                logger.exception(
                    "Telegram ingress worker failed; retrying",
                    extra={"event": "telegram_ingress_worker_failed"},
                )
                await self._wait(delay_seconds=5.0)

    async def _release_stale_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_stale_recovery_at:
            return
        await self.repository.release_stale_inbound_updates()
        self._next_stale_recovery_at = now + self.stale_recovery_interval_seconds

    def _progress(self) -> None:
        if self.runtime_health is not None:
            self.runtime_health.progress("telegram_ingress")

    async def _wait(self, *, delay_seconds: float | None = None) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(
                self._wake.wait(),
                timeout=self.poll_interval_seconds if delay_seconds is None else delay_seconds,
            )
        except TimeoutError:
            pass
