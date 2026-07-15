from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import TelegramObject


class TelegramUpdateTaskRegistry(BaseMiddleware):
    """Track accepted Telegram updates until their handlers have finished.

    Aiogram owns the task creation, so a middleware-only registry has a small but
    important race: polling can stop after creating a task but before that task
    enters the middleware. The compatibility adapter below also observes
    aiogram's task set after polling has stopped. Keeping that private detail in
    one guarded class makes an incompatible aiogram upgrade fail at startup
    instead of silently reintroducing update loss during shutdown.
    """

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher
        self._middleware_tasks: set[asyncio.Task[Any]] = set()
        self._dispatcher_tasks()

    def _dispatcher_tasks(self) -> set[asyncio.Future[Any]]:
        raw_tasks = getattr(self._dispatcher, "_handle_update_tasks", None)
        if not isinstance(raw_tasks, set) or any(
            not isinstance(task, asyncio.Future) for task in raw_tasks
        ):
            raise RuntimeError(
                "Installed aiogram is incompatible with safe Telegram update shutdown"
            )
        return cast(set[asyncio.Future[Any]], raw_tasks)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always provides one here
            raise RuntimeError("Telegram handler is not running in an asyncio task")
        self._middleware_tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self._middleware_tasks.discard(task)

    def _pending_tasks(self) -> set[asyncio.Future[Any]]:
        tasks: set[asyncio.Future[Any]] = set(self._middleware_tasks)
        tasks.update(self._dispatcher_tasks())
        return {task for task in tasks if not task.done()}

    async def drain(self) -> None:
        """Wait for every task accepted before polling stopped, without cancellation."""

        idle_turns = 0
        while idle_turns < 2:
            pending = self._pending_tasks()
            if pending:
                idle_turns = 0
                await asyncio.gather(*pending, return_exceptions=True)
                continue
            # A task created by aiogram may not have entered middleware yet. Two
            # event-loop turns close that scheduling window after polling exits.
            idle_turns += 1
            await asyncio.sleep(0)


def create_polling_task(
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    allowed_updates: list[str],
) -> asyncio.Task[None]:
    """Start polling while retaining ownership of the shared Bot session."""

    return asyncio.create_task(
        dispatcher.start_polling(
            bot,
            allowed_updates=allowed_updates,
            handle_as_tasks=False,
            close_bot_session=False,
        ),
        name="telegram-polling",
    )
