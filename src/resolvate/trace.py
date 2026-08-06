from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
logger = logging.getLogger(__name__)


def get_trace_id() -> str | None:
    return trace_id_var.get()


class TraceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        trace_id = uuid.uuid4().hex
        token = trace_id_var.set(trace_id)
        data["trace_id"] = trace_id
        try:
            logger.info("telegram update received", extra={"event": "telegram_update"})
            return await handler(event, data)
        finally:
            trace_id_var.reset(token)
