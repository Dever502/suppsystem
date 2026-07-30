from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, TelegramObject, Update

from suppsystem.durable_work import DurableWorkRepository
from suppsystem.runtime_health import RuntimeHealth
from suppsystem.runtime_supervision import wait_for_event
from suppsystem.telegram_limits import TelegramInboundRateLimiter, TelegramRateLimiter

logger = logging.getLogger(__name__)


class DurableTelegramIngressMiddleware(BaseMiddleware):
    """Apply per-user admission control, then durably persist polling updates."""

    def __init__(
        self,
        repository: DurableWorkRepository,
        wake_worker: Callable[[], None],
        *,
        bot: Bot,
        inbound_limiter: TelegramInboundRateLimiter,
        outbound_limiter: TelegramRateLimiter,
    ) -> None:
        self.repository = repository
        self.wake_worker = wake_worker
        self.bot = bot
        self.inbound_limiter = inbound_limiter
        self.outbound_limiter = outbound_limiter

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
        message = event.message
        if (
            isinstance(message, Message)
            and message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
        ):
            decision = await self.inbound_limiter.consume(message.from_user.id)
            if not decision.allowed:
                await self._reject_rate_limited_message(
                    message, decision.retry_after_seconds, decision.notify_user
                )
                return None
        payload = event.model_dump(mode="json", exclude_none=True)
        await self.repository.enqueue_inbound_update(event.update_id, payload)
        self.wake_worker()
        return None

    async def _reject_rate_limited_message(
        self, message: Message, retry_after_seconds: int, notify_user: bool
    ) -> None:
        logger.warning(
            "Rejected rate-limited private Telegram message",
            extra={
                "event": "telegram_inbound_rate_limited",
                "telegram_user_id": message.from_user.id if message.from_user is not None else None,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "retry_after_seconds": retry_after_seconds,
            },
        )
        if not notify_user:
            return
        delay = (
            f"{retry_after_seconds} сек."
            if retry_after_seconds < 60
            else f"{(retry_after_seconds + 59) // 60} мин."
        )
        try:
            await self.outbound_limiter.wait()
            await self.bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "⚠️ <b>Слишком много сообщений</b>\n\n"
                    "Последнее сообщение не принято. Уже принятые сообщения сохранены. "
                    f"Подождите примерно {delay} и повторите его. До окончания паузы "
                    "новые сообщения также не будут приняты."
                ),
            )
        except TelegramAPIError:
            logger.warning(
                "Unable to notify user about inbound rate limit",
                exc_info=True,
                extra={
                    "event": "telegram_inbound_rate_limit_notice_failed",
                    "telegram_user_id": (
                        message.from_user.id if message.from_user is not None else None
                    ),
                    "chat_id": message.chat.id,
                },
            )


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
        cleanup_interval_seconds: float = 3600.0,
    ) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.repository = repository
        self.runtime_health = runtime_health
        self.poll_interval_seconds = poll_interval_seconds
        if cleanup_interval_seconds <= 0:
            raise ValueError("cleanup_interval_seconds must be positive")
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self._stopped = asyncio.Event()
        self._wake = asyncio.Event()
        self._next_stale_recovery_at = 0.0
        self._next_cleanup_at = 0.0

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
                await self._purge_expired_if_due()
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

    async def _purge_expired_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_cleanup_at:
            return
        try:
            result = await self.repository.purge_expired_terminal_work()
        except Exception:
            self._next_cleanup_at = now + min(300.0, self.cleanup_interval_seconds)
            logger.exception(
                "Durable work retention cleanup failed; message processing continues",
                extra={"event": "durable_work_retention_cleanup_failed"},
            )
            return
        self._next_cleanup_at = now + self.cleanup_interval_seconds
        if result.total:
            logger.info(
                "Pruned %d expired durable work rows",
                result.total,
                extra={"event": "durable_work_retention_cleanup"},
            )

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
        await wait_for_event(
            self._wake,
            self.poll_interval_seconds if delay_seconds is None else delay_seconds,
        )
