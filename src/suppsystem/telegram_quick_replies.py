from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.quick_replies import (
    QUICK_RESPONSE_MAX_TAGS,
    QUICK_RESPONSE_PENDING_DELETION,
    QUICK_RESPONSE_PUBLICATION_FORMAT_VERSION,
    QUICK_RESPONSE_VALID,
    QuickReplyService,
    QuickResponseView,
    render_quick_response,
)
from suppsystem.telegram_errors import is_missing_topic_error
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_locks import TicketLockPool

logger = logging.getLogger(__name__)

QUICK_RESPONSE_DELETE_DELAY_SECONDS = 300
QUICK_RESPONSE_TOPIC_REFRESH_INTERVAL_SECONDS = 60.0
QUICK_RESPONSE_WARNING_TEXT = (
    "⚠️ Неправильное количество тегов. Укажите не более 4 тегов, "
    "иначе сообщение будет удалено через 5 минут."
)
QUICK_RESPONSE_INSTRUCTION_TEXT = (
    "⚡ Быстрые ответы\n\n"
    "Отправьте готовый ответ обычным текстовым сообщением. "
    "Можно добавить до 4 произвольных хештегов. После сохранения бот заменит исходник "
    "оформленной копией.\n\n"
    "Для поиска используйте лупу Telegram и текст или хештег."
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _message_not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in str(error).casefold()


def _message_missing(error: TelegramBadRequest) -> bool:
    text = str(error).casefold()
    return "message to edit not found" in text or "message_id_invalid" in text


class TelegramQuickReplyHandlers:
    bot: Bot
    authorization: AuthorizationService
    limiter: TelegramRateLimiter
    settings: Settings
    quick_reply_service: QuickReplyService | None
    quick_replies_topic_id: int | None
    recover_quick_replies_topic: Callable[[int], Awaitable[int]] | None
    _quick_response_tasks: dict[int, asyncio.Task[None]]
    _quick_response_locks: TicketLockPool
    _quick_response_topic_lock: asyncio.Lock

    def initialize_quick_reply_runtime(self) -> None:
        self._quick_response_tasks = {}
        self._quick_response_locks = TicketLockPool()
        self._quick_response_topic_lock = asyncio.Lock()

    @staticmethod
    def _hashtags(message: Message) -> list[str]:
        text = message.text or ""
        tags: list[str] = []
        for entity in message.entities or []:
            entity_type = getattr(entity.type, "value", entity.type)
            if entity_type == "hashtag":
                tags.append(entity.extract_from(text))
        return tags

    async def _delete_message(self, message_id: int) -> bool:
        try:
            await self.limiter.wait()
            await self.bot.delete_message(
                chat_id=self.settings.support_group_id,
                message_id=message_id,
            )
        except TelegramBadRequest as error:
            error_text = str(error).casefold()
            if "message to delete not found" in error_text or "message_id_invalid" in error_text:
                return True
            logger.warning(
                "Telegram rejected quick response deletion",
                exc_info=True,
                extra={
                    "event": "quick_response_message_delete_rejected",
                    "message_id": message_id,
                },
            )
            return False
        except TelegramAPIError:
            logger.warning(
                "Unable to delete quick response message",
                exc_info=True,
                extra={
                    "event": "quick_response_message_delete_failed",
                    "message_id": message_id,
                },
            )
            return False
        return True

    async def _edit_bot_message(self, message_id: int, text: str) -> bool:
        try:
            await self.limiter.wait()
            await self.bot.edit_message_text(
                chat_id=self.settings.support_group_id,
                message_id=message_id,
                text=text,
                parse_mode=None,
            )
        except TelegramBadRequest as error:
            if _message_not_modified(error):
                return True
            if _message_missing(error):
                return False
            raise
        return True

    async def _cleanup_warning(self, response: QuickResponseView) -> bool:
        assert self.quick_reply_service is not None
        warning_message_id = response.warning_message_id
        if warning_message_id is None:
            return True
        if not await self._delete_message(warning_message_id):
            return False
        if await self.quick_reply_service.clear_warning(response.id, warning_message_id):
            return True
        current = await self.quick_reply_service.get_by_source(
            source_chat_id=response.source_chat_id,
            source_message_id=response.source_message_id,
        )
        return current is not None and current.warning_message_id is None

    async def _publish_valid_response(
        self,
        response: QuickResponseView,
        *,
        force_new: bool = False,
        refresh_existing: bool = False,
    ) -> QuickResponseView:
        assert self.quick_reply_service is not None
        assert self.quick_replies_topic_id is not None
        rendered_text = render_quick_response(response.text)
        published_message_id = None if force_new else response.published_message_id

        if (
            published_message_id is not None
            and published_message_id != response.source_message_id
            and (
                refresh_existing
                or response.publication_format_version < QUICK_RESPONSE_PUBLICATION_FORMAT_VERSION
            )
        ):
            if not await self._edit_bot_message(published_message_id, rendered_text):
                published_message_id = None
        elif published_message_id == response.source_message_id:
            published_message_id = None

        if published_message_id is None:
            await self.limiter.wait()
            published = await self.bot.send_message(
                chat_id=self.settings.support_group_id,
                message_thread_id=self.quick_replies_topic_id,
                text=rendered_text,
                parse_mode=None,
            )
            published_message_id = published.message_id
            await self.quick_reply_service.record_publication(
                response.id,
                published_message_id,
            )

        source_deleted = await self._delete_message(response.source_message_id)
        warning_deleted = await self._cleanup_warning(response)
        if not source_deleted or not warning_deleted:
            raise RuntimeError("quick response source cleanup is incomplete")
        completed = await self.quick_reply_service.complete_publication(
            response.id,
            published_message_id,
        )
        if not completed:
            raise RuntimeError("quick response publication changed before completion")

        return (
            await self.quick_reply_service.get_by_source(
                source_chat_id=response.source_chat_id,
                source_message_id=response.source_message_id,
            )
            or response
        )

    async def _sync_warning_reply(
        self,
        message: Message,
        response: QuickResponseView,
    ) -> QuickResponseView:
        assert self.quick_reply_service is not None
        warning_message_id = response.warning_message_id
        if warning_message_id is not None:
            if await self._edit_bot_message(
                warning_message_id,
                QUICK_RESPONSE_WARNING_TEXT,
            ):
                return response
            await self.quick_reply_service.clear_warning(
                response.id,
                warning_message_id,
            )

        warning = await message.reply(QUICK_RESPONSE_WARNING_TEXT, parse_mode=None)
        attached = await self.quick_reply_service.attach_warning(
            response.id,
            warning.message_id,
        )
        if not attached:
            await self._delete_message(warning.message_id)
        return (
            await self.quick_reply_service.get_by_source(
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
            )
            or response
        )

    def _cancel_expiration(self, response_id: int) -> None:
        task = self._quick_response_tasks.pop(response_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_expiration(self, response: QuickResponseView) -> None:
        if response.invalid_until is None:
            return
        self._cancel_expiration(response.id)
        task = asyncio.create_task(
            self._expire_response(response.id, response.source_message_id, response.invalid_until),
            name=f"quick-response-expiration-{response.id}",
        )
        self._quick_response_tasks[response.id] = task

    async def _expire_response(
        self,
        response_id: int,
        source_message_id: int,
        invalid_until: datetime,
    ) -> None:
        try:
            delay = max(0.0, (invalid_until - _utcnow()).total_seconds())
            while True:
                await asyncio.sleep(delay)
                async with self._quick_response_locks.hold(source_message_id):
                    service = self.quick_reply_service
                    if service is None:
                        return
                    current = await service.get_by_source(
                        source_chat_id=self.settings.support_group_id,
                        source_message_id=source_message_id,
                    )
                    if (
                        current is None
                        or current.id != response_id
                        or current.state != QUICK_RESPONSE_PENDING_DELETION
                        or current.invalid_until != invalid_until
                    ):
                        return
                    original_deleted = await self._delete_message(source_message_id)
                    warning_deleted = (
                        current.warning_message_id is None
                        or await self._delete_message(current.warning_message_id)
                    )
                    if original_deleted and warning_deleted:
                        await service.delete_if_still_pending(
                            response_id,
                            invalid_until=invalid_until,
                        )
                        return
                delay = 30.0
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unable to expire invalid quick response",
                extra={
                    "event": "quick_response_expiration_failed",
                    "response_id": response_id,
                    "message_id": source_message_id,
                },
            )
        finally:
            task = self._quick_response_tasks.get(response_id)
            if task is asyncio.current_task():
                self._quick_response_tasks.pop(response_id, None)

    async def _accept_response(
        self,
        message: Message,
        tags: list[str],
    ) -> None:
        assert self.quick_reply_service is not None
        assert message.from_user is not None
        response = await self.quick_reply_service.save_valid(
            text=message.text or "",
            tags=tags,
            operator_telegram_id=message.from_user.id,
            operator_display_name=message.from_user.full_name,
            operator_username=message.from_user.username,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )
        self._cancel_expiration(response.id)
        await self._publish_valid_response(response, refresh_existing=True)

    async def _reject_response(
        self,
        message: Message,
        tags: list[str],
        previous: QuickResponseView | None,
    ) -> None:
        assert self.quick_reply_service is not None
        assert message.from_user is not None
        invalid_until = (
            previous.invalid_until
            if previous is not None
            and previous.state == QUICK_RESPONSE_PENDING_DELETION
            and previous.invalid_until is not None
            else _utcnow() + timedelta(seconds=QUICK_RESPONSE_DELETE_DELAY_SECONDS)
        )
        if (
            previous is not None
            and previous.state == QUICK_RESPONSE_VALID
            and previous.published_message_id is not None
            and previous.published_message_id != previous.source_message_id
        ):
            if not await self._delete_message(previous.published_message_id):
                raise RuntimeError("unable to remove previous quick response publication")
        response = await self.quick_reply_service.save_pending_deletion(
            text=message.text or "",
            tags=tags,
            operator_telegram_id=message.from_user.id,
            operator_display_name=message.from_user.full_name,
            operator_username=message.from_user.username,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            invalid_until=invalid_until,
        )
        response = await self._sync_warning_reply(message, response)
        self._schedule_expiration(response)

    async def handle_quick_reply_topic_message(
        self,
        message: Message,
        command: str = "",
    ) -> bool:
        del command
        if (
            getattr(self, "quick_replies_topic_id", None) is None
            or message.message_thread_id != self.quick_replies_topic_id
        ):
            return False
        if (
            self.quick_reply_service is None
            or message.from_user is None
            or message.text is None
            or not message.text.strip()
        ):
            return True

        async with self._quick_response_locks.hold(message.message_id):
            previous = await self.quick_reply_service.get_by_source(
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
            )
            tags = self._hashtags(message)
            if len(tags) <= QUICK_RESPONSE_MAX_TAGS:
                await self._accept_response(message, tags)
            else:
                await self._reject_response(message, tags, previous)
        return True

    async def _pin_instruction(self, message_id: int) -> None:
        try:
            await self.limiter.wait()
            await self.bot.pin_chat_message(
                chat_id=self.settings.support_group_id,
                message_id=message_id,
                disable_notification=True,
            )
        except TelegramBadRequest as error:
            if "already pinned" not in str(error).casefold():
                logger.warning(
                    "Unable to pin quick response instruction",
                    exc_info=True,
                    extra={"event": "quick_response_instruction_pin_failed"},
                )
        except TelegramAPIError:
            logger.warning(
                "Unable to pin quick response instruction",
                exc_info=True,
                extra={"event": "quick_response_instruction_pin_failed"},
            )

    async def _send_instruction(self) -> int:
        assert self.quick_reply_service is not None
        assert self.quick_replies_topic_id is not None
        await self.limiter.wait()
        message = await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            message_thread_id=self.quick_replies_topic_id,
            text=QUICK_RESPONSE_INSTRUCTION_TEXT,
            parse_mode=None,
        )
        await self.quick_reply_service.save_instruction_message_id(
            self.settings.support_group_id,
            message.message_id,
            self.quick_replies_topic_id,
        )
        await self._pin_instruction(message.message_id)
        return message.message_id

    async def _restore_valid_responses(self, *, all_responses: bool) -> None:
        assert self.quick_reply_service is not None
        assert self.quick_replies_topic_id is not None
        for response in await self.quick_reply_service.list_valid():
            if (
                not all_responses
                and response.published_message_id is not None
                and response.publication_format_version >= QUICK_RESPONSE_PUBLICATION_FORMAT_VERSION
                and response.warning_message_id is None
            ):
                continue
            await self._publish_valid_response(response, force_new=all_responses)

    async def _cleanup_legacy_messages(self, instruction_message_id: int) -> None:
        assert self.quick_reply_service is not None
        message_ids = await self.quick_reply_service.legacy_message_ids(
            self.settings.support_group_id
        )
        cleaned = True
        for message_id in message_ids:
            if message_id == instruction_message_id:
                continue
            cleaned = await self._delete_message(message_id) and cleaned
        if cleaned:
            await self.quick_reply_service.finish_legacy_cleanup(self.settings.support_group_id)

    async def _cancel_all_expirations(self) -> None:
        tasks = list(self._quick_response_tasks.values())
        self._quick_response_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _recover_quick_response_topic(self, missing_topic_id: int) -> None:
        recover = getattr(self, "recover_quick_replies_topic", None)
        if recover is None:
            raise RuntimeError("quick response topic recovery is not configured")
        self.quick_replies_topic_id = await recover(missing_topic_id)
        await self._cancel_all_expirations()
        assert self.quick_reply_service is not None
        await self.quick_reply_service.discard_all_pending()
        instruction_message_id = await self._send_instruction()
        await self._restore_valid_responses(all_responses=True)
        await self._cleanup_legacy_messages(instruction_message_id)
        logger.warning(
            "Recreated quick response topic and restored saved responses",
            extra={
                "event": "quick_response_topic_recovered",
                "old_topic_id": missing_topic_id,
                "new_topic_id": self.quick_replies_topic_id,
            },
        )

    async def ensure_quick_response_topic(self) -> None:
        service = self.quick_reply_service
        topic_id = self.quick_replies_topic_id
        if service is None or topic_id is None:
            return
        async with self._quick_response_topic_lock:
            instruction_topic_id = await service.instruction_topic_id(
                self.settings.support_group_id
            )
            topic_was_replaced = (
                instruction_topic_id is not None and instruction_topic_id != topic_id
            )
            if topic_was_replaced:
                await self._cancel_all_expirations()
                await service.discard_all_pending()
            instruction_id = await service.instruction_message_id(self.settings.support_group_id)
            if instruction_id is None or topic_was_replaced:
                try:
                    instruction_id = await self._send_instruction()
                except TelegramBadRequest as error:
                    if not is_missing_topic_error(error):
                        raise
                    await self._recover_quick_response_topic(topic_id)
                    return
            else:
                try:
                    await self.limiter.wait()
                    await self.bot.edit_message_text(
                        chat_id=self.settings.support_group_id,
                        message_id=instruction_id,
                        text=QUICK_RESPONSE_INSTRUCTION_TEXT,
                        parse_mode=None,
                    )
                except TelegramBadRequest as error:
                    if is_missing_topic_error(error):
                        await self._recover_quick_response_topic(topic_id)
                        return
                    if _message_missing(error):
                        try:
                            instruction_id = await self._send_instruction()
                        except TelegramBadRequest as send_error:
                            if not is_missing_topic_error(send_error):
                                raise
                            await self._recover_quick_response_topic(topic_id)
                            return
                    elif not _message_not_modified(error):
                        raise
                await self._pin_instruction(instruction_id)

            await self._cleanup_legacy_messages(instruction_id)
            await self._restore_valid_responses(all_responses=topic_was_replaced)

    async def restore_pending_quick_response_expirations(self) -> None:
        if self.quick_reply_service is None:
            return
        for response in await self.quick_reply_service.list_pending_deletion():
            self._schedule_expiration(response)

    async def shutdown_quick_reply_runtime(self) -> None:
        await self._cancel_all_expirations()


class QuickResponseTopicRefreshWorker:
    def __init__(
        self,
        topic: TelegramQuickReplyHandlers,
        *,
        interval_seconds: float = QUICK_RESPONSE_TOPIC_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.topic = topic
        self.interval_seconds = interval_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                try:
                    await self.topic.ensure_quick_response_topic()
                except Exception:
                    logger.exception(
                        "Unable to refresh quick response topic",
                        extra={"event": "quick_response_topic_refresh_failed"},
                    )
