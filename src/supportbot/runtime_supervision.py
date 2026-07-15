from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

SHUTDOWN_SOFT_TIMEOUT_SECONDS = 20.0


async def stop_polling_task(
    polling_task: asyncio.Task[None],
    stop_polling: Callable[[], Awaitable[None]],
) -> None:
    if not polling_task.done():
        try:
            await stop_polling()
        except RuntimeError:
            # Aiogram rejects stop_polling before its running flag is set.
            # Cancellation is the only safe fallback during early shutdown.
            polling_task.cancel()
    await asyncio.gather(polling_task, return_exceptions=True)


async def supervise_ingress(
    polling_task: asyncio.Task[None],
    api_task: asyncio.Task[None] | None,
    stop_polling: Callable[[], Awaitable[None]],
) -> None:
    """Keep Telegram polling and the optional API in one failure domain."""

    if api_task is None:
        await polling_task
        return

    done, _ = await asyncio.wait({polling_task, api_task}, return_when=asyncio.FIRST_COMPLETED)
    if polling_task in done:
        await polling_task
        if api_task.done():
            await api_task
        return

    api_error: BaseException | None = None
    try:
        await api_task
    except BaseException as error:
        api_error = error

    await stop_polling_task(polling_task, stop_polling)

    if api_error is not None:
        raise api_error
    raise RuntimeError("API server stopped while Telegram polling was still running")


async def _wait_without_cancelling(
    tasks: set[asyncio.Task[None]],
    *,
    phase: str,
    soft_timeout_seconds: float,
) -> None:
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=soft_timeout_seconds)
    if pending:
        logger.warning(
            "Graceful shutdown phase exceeded its soft deadline; resources remain open",
            extra={
                "event": "shutdown_soft_deadline_exceeded",
                "shutdown_phase": phase,
                "pending_task_count": len(pending),
                "soft_timeout_seconds": soft_timeout_seconds,
            },
        )
    # Confirmed Telegram updates must never be cancelled here: their Bot API
    # offset has already advanced. Keep every dependency open until they finish.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.error(
                "Graceful shutdown task failed",
                exc_info=(type(result), result, result.__traceback__),
                extra={
                    "event": "shutdown_task_failed",
                    "shutdown_phase": phase,
                    "shutdown_task": task.get_name(),
                },
            )


async def shutdown_runtime(
    *,
    polling_task: asyncio.Task[None],
    stop_polling: Callable[[], Awaitable[None]],
    drain_telegram_handlers: Callable[[], Awaitable[None]],
    api_task: asyncio.Task[None] | None,
    request_api_stop: Callable[[], None] | None,
    worker_tasks: tuple[asyncio.Task[None], ...],
    stop_workers: tuple[Callable[[], None], ...],
    close_resources: tuple[Callable[[], Awaitable[None]], ...],
    soft_timeout_seconds: float = SHUTDOWN_SOFT_TIMEOUT_SECONDS,
) -> None:
    """Stop ingress, drain work, and only then close shared dependencies."""

    if request_api_stop is not None:
        request_api_stop()

    async def stop_and_drain_telegram() -> None:
        await stop_polling_task(polling_task, stop_polling)
        await drain_telegram_handlers()

    telegram_task = asyncio.create_task(stop_and_drain_telegram(), name="telegram-ingress-shutdown")
    ingress_tasks = {telegram_task}
    if api_task is not None:
        ingress_tasks.add(api_task)
    await _wait_without_cancelling(
        ingress_tasks,
        phase="ingress",
        soft_timeout_seconds=soft_timeout_seconds,
    )

    for stop_worker in stop_workers:
        stop_worker()
    await _wait_without_cancelling(
        set(worker_tasks),
        phase="workers",
        soft_timeout_seconds=soft_timeout_seconds,
    )

    for close_resource in close_resources:
        try:
            await close_resource()
        except Exception:
            logger.exception(
                "Unable to close a runtime resource during shutdown",
                extra={"event": "shutdown_resource_close_failed"},
            )
