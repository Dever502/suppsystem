from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType, MessageEntityType
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, MessageEntity, Update, User
from pydantic import SecretStr

from suppsystem.config import Settings
from suppsystem.telegram_adapter import WELCOME_TEXT, TelegramSupportAdapter
from suppsystem.telegram_limits import TelegramRateLimiter


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 -- aiogram abstract interface
    ) -> Any:
        del bot, timeout
        self.requests.append(method)
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 -- aiogram abstract interface
        chunk_size: int = 65_536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        del url, headers, timeout, chunk_size, raise_for_status
        if False:
            yield b""


class TicketServiceMustNotBeCalled:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Unauthorized update reached ticket service method {name}")


def _message_update(
    *,
    update_id: int,
    chat_id: int,
    chat_type: ChatType,
    user_id: int,
    text: str,
    message_thread_id: int | None = None,
    command: bool = False,
) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=user_id, is_bot=False, first_name="Router Test"),
            text=text,
            message_thread_id=message_thread_id,
            entities=(
                [
                    MessageEntity(
                        type=MessageEntityType.BOT_COMMAND,
                        offset=0,
                        length=len(text.split(maxsplit=1)[0]),
                    )
                ]
                if command
                else None
            ),
        ),
    )


def _edited_message_update(
    *,
    update_id: int,
    chat_id: int,
    user_id: int,
    text: str,
    message_thread_id: int,
) -> Update:
    return Update(
        update_id=update_id,
        edited_message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=ChatType.SUPERGROUP),
            from_user=User(id=user_id, is_bot=False, first_name="Router Test"),
            text=text,
            message_thread_id=message_thread_id,
        ),
    )


async def test_router_registers_and_routes_private_and_authorized_group_boundaries() -> None:
    session = RecordingSession()
    bot = Bot(token=f"123456:{'A' * 35}", session=session)
    settings = Settings(
        support_bot_token=SecretStr("test-token"),
        support_group_id=-100123,
        admin_telegram_ids={2},
    )
    adapter = TelegramSupportAdapter(
        bot=bot,
        ticket_service=TicketServiceMustNotBeCalled(),  # type: ignore[arg-type]
        settings=settings,
        limiter=TelegramRateLimiter(0),
        statistics_service=SimpleNamespace(),  # type: ignore[arg-type]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(adapter.router)

    assert len(adapter.router.message.handlers) == 3
    assert len(adapter.router.edited_message.handlers) == 1
    assert len(adapter.router.callback_query.handlers) == 3
    assert len(adapter.router.inline_query.handlers) == 0

    await dispatcher.feed_update(
        bot,
        _message_update(
            update_id=1,
            chat_id=1001,
            chat_type=ChatType.PRIVATE,
            user_id=1001,
            text="/start",
            command=True,
        ),
    )

    assert len(session.requests) == 1
    assert isinstance(session.requests[0], SendMessage)
    assert session.requests[0].text == WELCOME_TEXT
    session.requests.clear()

    await dispatcher.feed_update(
        bot,
        _message_update(
            update_id=2,
            chat_id=settings.support_group_id,
            chat_type=ChatType.SUPERGROUP,
            user_id=3,
            text="Ответ, который нельзя отправить",
            message_thread_id=777,
        ),
    )

    assert session.requests == []

    await dispatcher.feed_update(
        bot,
        _message_update(
            update_id=3,
            chat_id=-100999,
            chat_type=ChatType.SUPERGROUP,
            user_id=3,
            text="Сообщение в чужой группе",
            message_thread_id=777,
        ),
    )

    assert session.requests == []

    await dispatcher.feed_update(
        bot,
        _edited_message_update(
            update_id=4,
            chat_id=settings.support_group_id,
            user_id=2,
            text="Изменённый ответ в клиентской теме",
            message_thread_id=777,
        ),
    )

    assert session.requests == []
    await bot.session.close()
    assert session.closed is True
