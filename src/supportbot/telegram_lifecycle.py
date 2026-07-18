from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher


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
