from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from supportbot.durable_work import DurableWorkRepository, ReconciliationJob
from supportbot.panel import PanelService
from supportbot.runtime_health import RuntimeHealth
from supportbot.runtime_supervision import wait_for_event

logger = logging.getLogger(__name__)


class ReconciliationWorker:
    def __init__(
        self,
        *,
        repository: DurableWorkRepository,
        reconcile_topic: Callable[[str], Awaitable[bool]],
        panel_service: PanelService | None,
        runtime_health: RuntimeHealth | None = None,
        poll_interval_seconds: float = 0.5,
        stale_recovery_interval_seconds: float = 60.0,
    ) -> None:
        self.repository = repository
        self.reconcile_topic = reconcile_topic
        self.panel_service = panel_service
        self.runtime_health = runtime_health
        self.poll_interval_seconds = poll_interval_seconds
        self.stale_recovery_interval_seconds = stale_recovery_interval_seconds
        self._stopped = asyncio.Event()
        self._next_stale_recovery_at = 0.0

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        if self.runtime_health is not None:
            self.runtime_health.starting("reconciliation")
        while not self._stopped.is_set():
            try:
                await self._release_stale_if_due()
                job = await self.repository.claim_reconciliation()
                self._progress()
                if job is None:
                    await self._wait()
                    continue
                await self._process(job)
                self._progress()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.runtime_health is not None:
                    self.runtime_health.degraded("reconciliation")
                logger.exception(
                    "Reconciliation worker failed; retrying",
                    extra={"event": "reconciliation_worker_failed"},
                )
                await self._wait(delay_seconds=5.0)

    async def _process(self, job: ReconciliationJob) -> None:
        try:
            if job.kind == "telegram_topic":
                if job.ticket_id is None:
                    raise RuntimeError("topic reconciliation is missing ticket_id")
                completed = await self.reconcile_topic(job.ticket_id)
            elif job.kind == "remnawave":
                if self.panel_service is None or job.operator_action_id is None:
                    raise RuntimeError("Remnawave reconciliation is not configured")
                completed = await self.panel_service.reconcile_durable_action(
                    job.operator_action_id,
                    job.payload,
                    attempt_count=job.attempt_count,
                )
            else:
                raise RuntimeError(f"unknown reconciliation kind: {job.kind}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.repository.retry_reconciliation(job, str(error))
            logger.exception(
                "Reconciliation job failed; retry scheduled",
                extra={"event": "reconciliation_retry", "reconciliation_id": job.id},
            )
            return
        if completed:
            await self.repository.finish_reconciliation(job)
        else:
            await self.repository.retry_reconciliation(job, "desired external state not observed")

    async def _release_stale_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_stale_recovery_at:
            return
        await self.repository.release_stale_reconciliations()
        self._next_stale_recovery_at = now + self.stale_recovery_interval_seconds

    def _progress(self) -> None:
        if self.runtime_health is not None:
            self.runtime_health.progress("reconciliation")

    async def _wait(self, *, delay_seconds: float | None = None) -> None:
        await wait_for_event(
            self._stopped,
            self.poll_interval_seconds if delay_seconds is None else delay_seconds,
        )
