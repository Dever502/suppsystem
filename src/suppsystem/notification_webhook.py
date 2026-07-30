from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from suppsystem.config import Settings
from suppsystem.metrics import MetricsRegistry
from suppsystem.outbox_repository import OutboxRepository
from suppsystem.runtime_health import RuntimeHealth
from suppsystem.runtime_supervision import wait_for_event
from suppsystem.service_types import NotificationJob

logger = logging.getLogger(__name__)


class NotificationWebhookWorker:
    def __init__(
        self,
        *,
        outbox: OutboxRepository,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        heartbeat_path: Path | None = None,
        runtime_health: RuntimeHealth | None = None,
        metrics: MetricsRegistry | None = None,
        stale_recovery_interval_seconds: float = 60.0,
        stale_notification_after_seconds: int = 300,
    ) -> None:
        if settings.notification_webhook_url is None:
            raise ValueError("notification_webhook_url is required")
        if settings.notification_webhook_secret is None:
            raise ValueError("notification_webhook_secret is required")
        self.outbox = outbox
        self.settings = settings
        self.url = settings.notification_webhook_url
        self.secret = settings.notification_webhook_secret.get_secret_value()
        self._client = client
        self.heartbeat_path = heartbeat_path
        self.runtime_health = runtime_health
        self.metrics = metrics
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self.stale_notification_after_seconds = stale_notification_after_seconds
        self._next_stale_recovery_at = 0.0
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        if self.runtime_health is not None:
            self.runtime_health.starting("notification_webhook")
        while not self._stopped.is_set():
            try:
                await self._release_stale_if_due()
                self._record_progress()
                jobs = await self.outbox.claim_due_notifications(limit=1)
                self._record_progress()
                if not jobs:
                    await self._wait_for_poll_interval()
                    continue
                await self._deliver(jobs[0])
                self._record_progress()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.runtime_health is not None:
                    self.runtime_health.degraded("notification_webhook")
                logger.exception(
                    "Notification webhook worker failed; retrying",
                    extra={"event": "notification_webhook_worker_failed"},
                )
                await self._wait_for_poll_interval(delay_seconds=5.0)

    def stop(self) -> None:
        self._stopped.set()

    async def _release_stale_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_stale_recovery_at:
            return
        await self.outbox.release_stale_notifications(
            stale_after_seconds=self.stale_notification_after_seconds
        )
        self._next_stale_recovery_at = now + self.stale_recovery_interval_seconds

    def _touch_heartbeat(self) -> None:
        if self.heartbeat_path is None:
            return
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.touch()

    def _record_progress(self) -> None:
        self._touch_heartbeat()
        if self.runtime_health is not None:
            self.runtime_health.progress("notification_webhook")

    async def _deliver(self, job: NotificationJob) -> None:
        body = self._body(job)
        headers = self._headers(job.id, body)
        client = self._client or httpx.AsyncClient()
        should_close = self._client is None
        started_at = time.monotonic()
        outcome = "request_error"
        try:
            response = await client.post(
                self.url,
                content=body,
                headers=headers,
                timeout=self.settings.notification_webhook_timeout_seconds,
            )
            outcome = f"http_{response.status_code // 100}xx"
        except httpx.HTTPError as error:
            await self._retry(job, f"notification webhook request failed: {error}")
            return
        finally:
            if self.metrics is not None:
                self.metrics.observe_request(
                    "notification_webhook", outcome, time.monotonic() - started_at
                )
            if should_close:
                await client.aclose()

        if 200 <= response.status_code < 300:
            changed = await self.outbox.mark_notification_delivered(
                job.id, claim_token=job.claim_token
            )
            if not changed:
                logger.warning(
                    "Ignored webhook completion from a stale claim",
                    extra={
                        "event": "notification_webhook_stale_claim_ignored",
                        "notification_id": job.id,
                    },
                )
                return
            logger.info(
                "Delivered notification webhook",
                extra={
                    "event": "notification_webhook_delivered",
                    "notification_id": job.id,
                    "ticket_id": job.ticket_id,
                    "event_type": job.event_type,
                    "destination": job.destination,
                    "status_code": response.status_code,
                },
            )
            return

        message = f"notification webhook returned HTTP {response.status_code}"
        if response.status_code >= 500 or response.status_code in {408, 409, 425, 429}:
            await self._retry(
                job,
                message,
                retry_after_seconds=parse_retry_after(response.headers.get("Retry-After")),
            )
        else:
            changed = await self.outbox.mark_notification_failed(
                job.id, claim_token=job.claim_token, error=message
            )
            if not changed:
                return
            if self.metrics is not None:
                self.metrics.event("notification_webhook", "failed")
            logger.error(
                "Notification webhook failed permanently",
                extra={
                    "event": "notification_webhook_failed_permanently",
                    "notification_id": job.id,
                    "ticket_id": job.ticket_id,
                    "event_type": job.event_type,
                    "destination": job.destination,
                    "status_code": response.status_code,
                },
            )

    async def _retry(
        self, job: NotificationJob, error: str, *, retry_after_seconds: float | None = None
    ) -> None:
        retry_after = (
            min(3600.0, retry_after_seconds)
            if retry_after_seconds is not None
            else min(60.0, 2.0 ** min(job.attempt_count, 6))
        )
        changed = await self.outbox.mark_notification_retry(
            job.id,
            claim_token=job.claim_token,
            error=error,
            retry_after_seconds=retry_after,
            max_attempts=self.settings.notification_webhook_max_attempts,
        )
        if not changed:
            return
        if self.metrics is not None:
            self.metrics.event("notification_webhook", "retry")
        logger.warning(
            "Notification webhook retry scheduled",
            extra={
                "event": "notification_webhook_retry_scheduled",
                "notification_id": job.id,
                "ticket_id": job.ticket_id,
                "event_type": job.event_type,
                "destination": job.destination,
                "attempt_count": job.attempt_count,
                "max_attempts": self.settings.notification_webhook_max_attempts,
                "retry_after_seconds": retry_after,
                "error_message": error[:300],
            },
        )

    def _body(self, job: NotificationJob) -> bytes:
        payload = {
            "event_id": job.id,
            "event_type": job.event_type,
            "ticket_id": job.ticket_id,
            "destination": job.destination,
            "recipient": {
                "identity_provider": job.recipient_identity_provider,
                "identity_value": job.recipient_identity_value,
            },
            "payload": job.payload,
            "created_at": job.created_at.isoformat(),
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()

    def _headers(self, event_id: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature_payload = timestamp.encode() + b"." + body
        signature = hmac.new(self.secret.encode(), signature_payload, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Support-Event-Id": event_id,
            "X-Support-Timestamp": timestamp,
            "X-Support-Signature": f"sha256={signature}",
        }

    async def _wait_for_poll_interval(self, *, delay_seconds: float | None = None) -> None:
        await wait_for_event(
            self._stopped,
            self.settings.notification_webhook_poll_interval_seconds
            if delay_seconds is None
            else delay_seconds,
        )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    try:
        return max(0.0, float(stripped))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current_time = datetime.now(UTC) if now is None else now
    return max(0.0, (retry_at - current_time).total_seconds())
