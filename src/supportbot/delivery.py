from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from supportbot.audit import record_event
from supportbot.config import Settings
from supportbot.models import Direction
from supportbot.outbox_repository import OutboxRepository
from supportbot.runtime_health import RuntimeHealth
from supportbot.runtime_supervision import wait_for_event
from supportbot.service_types import DeliveryJob
from supportbot.services import TicketService
from supportbot.telegram_errors import is_missing_topic_error
from supportbot.telegram_limits import TelegramRateLimiter

logger = logging.getLogger(__name__)


def _payload_int(payload: dict[str, object], key: str) -> int:
    """Return an integer delivery field, rejecting malformed persisted jobs."""

    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"Delivery payload field {key!r} must be an integer")
    return int(value)


class DeliveryWorker:
    """Persists retries through the outbox instead of relying on process memory."""

    def __init__(
        self,
        *,
        bot: Bot,
        ticket_service: TicketService,
        outbox: OutboxRepository,
        settings: Settings,
        limiter: TelegramRateLimiter,
        heartbeat_path: Path,
        recover_missing_topic: Callable[[str, int], Awaitable[int | None]],
        runtime_health: RuntimeHealth | None = None,
        stale_recovery_interval_seconds: float = 60.0,
        stale_delivery_after_seconds: int = 300,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if stale_recovery_interval_seconds <= 0:
            raise ValueError("stale_recovery_interval_seconds must be positive")
        if stale_delivery_after_seconds <= 0:
            raise ValueError("stale_delivery_after_seconds must be positive")
        self.bot = bot
        self.ticket_service = ticket_service
        self.outbox = outbox
        self.settings = settings
        self.limiter = limiter
        self.heartbeat_path = heartbeat_path
        self.recover_missing_topic = recover_missing_topic
        self.runtime_health = runtime_health
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self.stale_delivery_after_seconds = stale_delivery_after_seconds
        self._monotonic = monotonic
        self._next_stale_recovery_at = 0.0
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        if self.runtime_health is not None:
            self.runtime_health.starting("delivery_worker")
        while not self._stopped.is_set():
            try:
                await self._release_stale_deliveries_if_due()
                self._record_progress()
                jobs = await self.outbox.claim_due_deliveries()
                self._record_progress()
                if jobs:
                    logger.info(
                        "Claimed due delivery jobs",
                        extra={
                            "event": "delivery_jobs_claimed",
                            "claimed_delivery_count": len(jobs),
                        },
                    )
                if not jobs:
                    await self._wait_for_poll_interval()
                    continue
                await self._process_claimed_jobs(jobs)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.runtime_health is not None:
                    self.runtime_health.degraded("delivery_worker")
                logger.exception(
                    "Delivery worker failed; retrying", extra={"event": "delivery_worker_failed"}
                )
                await self._wait_for_poll_interval(delay_seconds=5.0)

    async def _release_stale_deliveries_if_due(self) -> int | None:
        now = self._monotonic()
        if now < self._next_stale_recovery_at:
            return None
        released = await self.outbox.release_stale_deliveries(
            stale_after_seconds=self.stale_delivery_after_seconds
        )
        self._next_stale_recovery_at = now + self.stale_recovery_interval_seconds
        if released:
            logger.warning(
                "Released stale delivery claims",
                extra={
                    "event": "stale_delivery_claims_released",
                    "released_delivery_count": released,
                },
            )
        return released

    def stop(self) -> None:
        self._stopped.set()

    async def _process_claimed_jobs(self, jobs: list[DeliveryJob]) -> None:
        for index, job in enumerate(jobs):
            if self._stopped.is_set():
                await self._release_unstarted_jobs(jobs[index:])
                return
            self._record_progress()
            try:
                await self._deliver(job)
            except asyncio.CancelledError:
                # The current Telegram call can have an unknown outcome and must
                # retain its claim for stale recovery. Later jobs have not started.
                await self._release_unstarted_jobs(jobs[index + 1 :])
                raise
            self._record_progress()

    async def _release_unstarted_jobs(self, jobs: list[DeliveryJob]) -> None:
        released = await self.outbox.release_delivery_claims(
            [(job.id, job.claim_token) for job in jobs]
        )
        if released:
            logger.info(
                "Released unstarted delivery claims during shutdown",
                extra={
                    "event": "delivery_shutdown_claims_released",
                    "released_delivery_count": released,
                },
            )

    def _touch_heartbeat(self) -> None:
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.touch()

    def _record_progress(self) -> None:
        self._touch_heartbeat()
        if self.runtime_health is not None:
            self.runtime_health.progress("delivery_worker")

    @staticmethod
    def _delivery_context(job: DeliveryJob, payload: dict[str, object]) -> dict[str, object]:
        return {
            "delivery_id": job.id,
            "ticket_id": job.ticket_id,
            "delivery_kind": payload.get("kind", "copy"),
            "target_chat_id": payload.get("target_chat_id"),
            "source_chat_id": payload.get("source_chat_id"),
            "source_message_id": payload.get("source_message_id"),
            "topic_id": payload.get("target_thread_id"),
            "attempt_count": job.attempt_count,
        }

    def _claim_transition_applied(
        self,
        job: DeliveryJob,
        payload: dict[str, object],
        *,
        transition: str,
        applied: bool,
    ) -> bool:
        if applied:
            return True
        logger.warning(
            "Ignored delivery transition from a stale claim",
            extra={
                "event": "stale_delivery_claim_transition_ignored",
                **self._delivery_context(job, payload),
                "delivery_transition": transition,
            },
        )
        return False

    async def _retry_delivery(
        self,
        job: DeliveryJob,
        payload: dict[str, object],
        error: str,
        retry_after_seconds: float,
        *,
        max_attempts: int | None = None,
        transition: str = "retry",
    ) -> bool:
        retried = await self.outbox.mark_delivery_retry(
            job.id,
            claim_token=job.claim_token,
            error=error,
            retry_after_seconds=retry_after_seconds,
            max_attempts=(
                self.settings.delivery_max_attempts if max_attempts is None else max_attempts
            ),
        )
        return self._claim_transition_applied(job, payload, transition=transition, applied=retried)

    async def _recipient_is_blocked(self, job: DeliveryJob, payload: dict[str, object]) -> bool:
        if Direction(job.direction) is not Direction.OPERATOR_TO_USER:
            return False
        try:
            target_chat_id = _payload_int(payload, "target_chat_id")
        except (KeyError, TypeError, ValueError):
            return False
        if not await self.ticket_service.is_blocked(target_chat_id):
            return False
        cancelled = await self.outbox.mark_delivery_cancelled(
            job.id,
            claim_token=job.claim_token,
            reason="recipient is blocked",
        )
        if not self._claim_transition_applied(
            job, payload, transition="cancelled", applied=cancelled
        ):
            return True
        logger.info(
            "Cancelled delivery to blocked recipient",
            extra={
                "event": "delivery_cancelled_blocked_recipient",
                **self._delivery_context(job, payload),
            },
        )
        return True

    async def _deliver(self, job: DeliveryJob) -> None:
        payload = job.payload
        delivered_message_id: int | None = None
        try:
            target_thread_id = payload.get("target_thread_id")
            if await self._recipient_is_blocked(job, payload):
                return
            await self.limiter.wait()
            if payload.get("kind", "copy") == "send_text":
                reply_markup_payload = payload.get("reply_markup")
                reply_markup = (
                    InlineKeyboardMarkup.model_validate(reply_markup_payload)
                    if isinstance(reply_markup_payload, dict)
                    else None
                )
                sent_message = await self.bot.send_message(
                    chat_id=_payload_int(payload, "target_chat_id"),
                    text=str(payload["text"]),
                    parse_mode=None,
                    message_thread_id=(
                        _payload_int(payload, "target_thread_id")
                        if target_thread_id is not None
                        else None
                    ),
                    reply_markup=reply_markup,
                )
                delivered_message_id = sent_message.message_id
            else:
                copied_message = await self.bot.copy_message(
                    chat_id=_payload_int(payload, "target_chat_id"),
                    from_chat_id=_payload_int(payload, "source_chat_id"),
                    message_id=_payload_int(payload, "source_message_id"),
                    message_thread_id=(
                        _payload_int(payload, "target_thread_id")
                        if target_thread_id is not None
                        else None
                    ),
                )
                delivered_message_id = copied_message.message_id
        except TelegramRetryAfter as error:
            retry_after = float(error.retry_after)
            await self.limiter.defer(retry_after)
            if not await self._retry_delivery(job, payload, str(error), retry_after):
                return
            logger.warning(
                "Telegram flood control delayed delivery",
                extra={
                    "event": "delivery_retry_scheduled",
                    **self._delivery_context(job, payload),
                    "retry_after_seconds": retry_after,
                    "max_attempts": self.settings.delivery_max_attempts,
                    "error_kind": "telegram_retry_after",
                    "error_message": str(error),
                },
            )
            if job.attempt_count >= self.settings.delivery_max_attempts:
                logger.error(
                    "Delivery exhausted after Telegram flood control",
                    extra={
                        "event": "delivery_exhausted",
                        **self._delivery_context(job, payload),
                        "max_attempts": self.settings.delivery_max_attempts,
                        "error_kind": "telegram_retry_after",
                    },
                )
                await self._alert(job, "delivery exhausted after Telegram flood control")
        except TelegramBadRequest as error:
            error_text = str(error)
            if is_missing_topic_error(error) and target_thread_id is not None:
                old_topic_id = _payload_int(payload, "target_thread_id")
                try:
                    new_topic_id = await self.recover_missing_topic(job.ticket_id, old_topic_id)
                    if new_topic_id is None:
                        await self._retry_delivery(job, payload, error_text, 5)
                        return
                    retargeted = await self.ticket_service.retarget_topic_deliveries(
                        ticket_id=job.ticket_id,
                        old_topic_id=old_topic_id,
                        new_topic_id=new_topic_id,
                    )
                    if retargeted == 0:
                        if not await self._retry_delivery(
                            job,
                            payload,
                            "topic recovered but delivery was not retargeted",
                            5,
                        ):
                            return
                        logger.error(
                            "Recovered support topic without retargeting current delivery",
                            extra={
                                "event": "missing_topic_retarget_empty",
                                **self._delivery_context(job, payload),
                                "old_topic_id": old_topic_id,
                                "new_topic_id": new_topic_id,
                            },
                        )
                        return
                    logger.warning(
                        "Recreated missing support topic and requeued deliveries",
                        extra={
                            "event": "missing_topic_recovered",
                            **self._delivery_context(job, payload),
                            "old_topic_id": old_topic_id,
                            "new_topic_id": new_topic_id,
                            "retargeted_delivery_count": retargeted,
                        },
                    )
                except Exception as recovery_error:
                    if not await self._retry_delivery(
                        job, payload, f"topic recovery failed: {recovery_error}"[:1000], 5
                    ):
                        return
                    logger.exception(
                        "Unable to recover missing support topic; delivery requeued",
                        extra={
                            "event": "missing_topic_recovery_failed",
                            **self._delivery_context(job, payload),
                            "old_topic_id": old_topic_id,
                            "error_kind": type(recovery_error).__name__,
                        },
                    )
                return
            if not await self._retry_delivery(
                job,
                payload,
                error_text,
                0,
                max_attempts=job.attempt_count,
                transition="failed",
            ):
                return
            logger.error(
                "Permanent Telegram delivery error",
                extra={
                    "event": "delivery_failed_permanently",
                    **self._delivery_context(job, payload),
                    "error_kind": "telegram_bad_request",
                    "error_message": error_text[:300],
                },
            )
            await self._alert(job, f"permanent Telegram delivery error: {error_text[:200]}")
        except TelegramAPIError as error:
            retry_after = min(60.0, 2.0 ** min(job.attempt_count, 6))
            if not await self._retry_delivery(job, payload, str(error), retry_after):
                return
            logger.warning(
                "Telegram API delivery error; retry scheduled",
                extra={
                    "event": "delivery_retry_scheduled",
                    **self._delivery_context(job, payload),
                    "retry_after_seconds": retry_after,
                    "max_attempts": self.settings.delivery_max_attempts,
                    "error_kind": "telegram_api_error",
                    "error_message": str(error)[:300],
                },
            )
            if job.attempt_count >= self.settings.delivery_max_attempts:
                logger.error(
                    "Delivery exhausted after Telegram API errors",
                    extra={
                        "event": "delivery_exhausted",
                        **self._delivery_context(job, payload),
                        "max_attempts": self.settings.delivery_max_attempts,
                        "error_kind": "telegram_api_error",
                    },
                )
                await self._alert(job, "delivery exhausted after Telegram API errors")
        except Exception as error:
            logger.exception(
                "Unexpected delivery failure",
                extra={
                    "event": "delivery_unexpected_error",
                    **self._delivery_context(job, payload),
                    "error_kind": type(error).__name__,
                },
            )
            if not await self._retry_delivery(job, payload, "unexpected delivery failure", 30):
                return
            if job.attempt_count >= self.settings.delivery_max_attempts:
                logger.error(
                    "Delivery exhausted after unexpected error",
                    extra={
                        "event": "delivery_exhausted",
                        **self._delivery_context(job, payload),
                        "max_attempts": self.settings.delivery_max_attempts,
                        "error_kind": "unexpected_error",
                    },
                )
                await self._alert(job, "delivery exhausted after unexpected error")
        else:
            delivered = await self.outbox.mark_delivery_delivered(
                job.id,
                claim_token=job.claim_token,
                delivered_message_id=delivered_message_id,
            )
            if not self._claim_transition_applied(
                job, payload, transition="delivered", applied=delivered
            ):
                return
            logger.info(
                "Delivered outbox message",
                extra={
                    "event": "delivery_delivered",
                    **self._delivery_context(job, payload),
                    "delivery_status": "delivered",
                    "delivered_message_id": delivered_message_id,
                },
            )

    async def _alert(self, job: DeliveryJob, reason: str) -> None:
        record_event("delivery_failed", ticket_id=job.ticket_id, delivery_id=job.id)
        logger.error(
            "Sending operator-visible delivery alert",
            extra={
                "event": "delivery_alert_sending",
                "ticket_id": job.ticket_id,
                "delivery_id": job.id,
                "error_message": reason[:300],
            },
        )
        try:
            await self.limiter.wait()
            await self.bot.send_message(
                chat_id=self.settings.support_group_id,
                text=(
                    "⚠️ <b>Ошибка доставки</b>\n\n"
                    f"Тикет: <code>{job.ticket_id}</code>\n"
                    f"Причина: {escape(reason)}"
                ),
            )
        except TelegramAPIError:
            logger.exception(
                "Unable to send delivery alert",
                extra={
                    "event": "delivery_alert_failed",
                    "ticket_id": job.ticket_id,
                    "delivery_id": job.id,
                },
            )

    async def _wait_for_poll_interval(self, *, delay_seconds: float | None = None) -> None:
        await wait_for_event(
            self._stopped,
            self.settings.delivery_poll_interval_seconds
            if delay_seconds is None
            else delay_seconds,
        )
