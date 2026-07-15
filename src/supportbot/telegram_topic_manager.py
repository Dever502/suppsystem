from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from supportbot.config import Settings
from supportbot.panel import (
    PanelService,
)
from supportbot.service_types import (
    TicketView,
    TopicProvisioningConflictError,
)
from supportbot.services import TicketService
from supportbot.telegram_errors import is_missing_topic_error
from supportbot.telegram_formatting import (
    code_or_dash,
    date_text,
    expiration_text,
    format_subscription_lookup,
    operator_ticket_info,
    panel_status_text,
    subscription_status,
    ticket_status_text,
    topic_identity,
    topic_name,
    traffic_text,
)
from supportbot.telegram_limits import TelegramRateLimiter
from supportbot.telegram_locks import TicketLockPool
from supportbot.telegram_message_utils import (
    attachment_metadata,
    command_argument,
    command_key,
    media_metadata,
    message_command,
    message_text,
    metadata_fields,
    rating_keyboard,
    rating_report,
)
from supportbot.telegram_panel_handler import (
    GIFT_DAYS_ERROR_TEXT as PANEL_GIFT_DAYS_ERROR_TEXT,
)

logger = logging.getLogger(__name__)
GIFT_DAYS_ERROR_TEXT = PANEL_GIFT_DAYS_ERROR_TEXT


class TelegramTopicManager:
    _operator_ticket_info = staticmethod(operator_ticket_info)
    _ticket_status_text = staticmethod(ticket_status_text)
    _message_command = staticmethod(message_command)
    _command_argument = staticmethod(command_argument)
    _message_text = staticmethod(message_text)
    _metadata_fields = staticmethod(metadata_fields)
    _attachment_metadata = staticmethod(attachment_metadata)
    _media_metadata = staticmethod(media_metadata)
    _rating_keyboard = staticmethod(rating_keyboard)
    _rating_report = staticmethod(rating_report)
    _command_key = staticmethod(command_key)
    _topic_identity = staticmethod(topic_identity)
    _topic_name = staticmethod(topic_name)
    _format_subscription_lookup = staticmethod(format_subscription_lookup)
    _subscription_status = staticmethod(subscription_status)
    _date_text = staticmethod(date_text)
    _expiration_text = staticmethod(expiration_text)
    _panel_status_text = staticmethod(panel_status_text)
    _code_or_dash = staticmethod(code_or_dash)
    _traffic_text = staticmethod(traffic_text)

    bot: Bot
    ticket_service: TicketService
    settings: Settings
    panel_service: PanelService | None
    limiter: TelegramRateLimiter
    _ticket_locks: TicketLockPool

    async def _refresh_reopened_ticket_context(self, ticket: TicketView) -> None:
        await self._sync_ticket_topic(ticket)
        try:
            await self._send_customer_card(ticket, event="ticket_reopened_customer_card_sent")
        except TelegramAPIError:
            logger.exception(
                "Unable to send reopened ticket customer card",
                extra={
                    "event": "ticket_reopened_customer_card_failed",
                    "ticket_id": ticket.id,
                    "telegram_user_id": ticket.telegram_user_id,
                    "topic_id": ticket.topic_id,
                },
            )
        except Exception:
            logger.exception(
                "Unexpected reopened ticket customer card failure",
                extra={
                    "event": "ticket_reopened_customer_card_unexpected_error",
                    "ticket_id": ticket.id,
                    "telegram_user_id": ticket.telegram_user_id,
                    "topic_id": ticket.topic_id,
                },
            )

    async def sync_ticket_topic(self, ticket: TicketView) -> bool:
        return await self._sync_ticket_topic(ticket)

    async def reconcile_ticket_topic(self, ticket_id: str) -> bool:
        ticket = await self.ticket_service.get_ticket(ticket_id)
        async with self._ticket_locks.hold(ticket.telegram_user_id):
            ticket = await self.ticket_service.get_ticket(ticket_id)
            if ticket.topic_id is None:
                ticket = await self._ensure_topic(ticket, reconcile_missing=True)
            if ticket.topic_id is None:
                return False
            return await self._sync_ticket_topic(ticket)

    async def recover_missing_topic(self, ticket_id: str, old_topic_id: int) -> int | None:
        """Replace a topic Telegram has confirmed no longer exists."""

        ticket = await self.ticket_service.get_ticket(ticket_id)
        async with self._ticket_locks.hold(ticket.telegram_user_id):
            ticket = await self.ticket_service.get_ticket(ticket_id)
            if ticket.topic_id == old_topic_id:
                await self.ticket_service.invalidate_topic(
                    ticket_id=ticket_id, topic_id=old_topic_id
                )
                ticket = await self.ticket_service.get_ticket(ticket_id)
            elif ticket.topic_id is not None:
                return ticket.topic_id

            try:
                ticket = await self._ensure_topic(
                    ticket,
                    recover_closed=ticket.status.value == "closed",
                )
            except Exception:
                # Topic attachment can succeed even if sending its customer card fails.
                ticket = await self.ticket_service.get_ticket(ticket_id)
                if ticket.topic_id is None:
                    raise
            return ticket.topic_id

    async def recover_waiting_topics_after_restart(self) -> int:
        """Resume only topic work that stopped before any Telegram call was claimed."""

        ticket_ids = await self.ticket_service.list_waiting_topic_recovery_ticket_ids()
        recovered = 0
        for ticket_id in ticket_ids:
            try:
                ticket = await self.ticket_service.get_ticket(ticket_id)
                async with self._ticket_locks.hold(ticket.telegram_user_id):
                    ticket = await self.ticket_service.get_ticket(ticket_id)
                    recovering_closed = ticket.status.value == "closed"
                    ticket = await self._ensure_topic(
                        ticket,
                        recover_closed=recovering_closed,
                    )
                    if ticket.topic_id is None and not recovering_closed:
                        # A concurrent close cancels the ordinary provisioning claim.
                        # Re-read the state and use the stricter closed-ticket claim so
                        # the waiting delivery is not stranded by that transition.
                        ticket = await self.ticket_service.get_ticket(ticket_id)
                        if ticket.status.value == "closed":
                            ticket = await self._ensure_topic(ticket, recover_closed=True)
                if ticket.topic_id is None:
                    logger.info(
                        "Deferred startup topic recovery because the claim is no longer available",
                        extra={
                            "event": "startup_topic_recovery_deferred",
                            "ticket_id": ticket_id,
                        },
                    )
                    continue
                recovered += 1
                logger.info(
                    "Recovered unclaimed topic work after restart",
                    extra={
                        "event": "startup_topic_recovery_succeeded",
                        "ticket_id": ticket_id,
                        "topic_id": ticket.topic_id,
                    },
                )
            except Exception:
                # _ensure_topic deliberately preserves a claim when the Telegram
                # result is unknown. A later automatic retry could create a duplicate.
                try:
                    current = await self.ticket_service.get_ticket(ticket_id)
                except Exception:
                    logger.exception(
                        "Unable to inspect topic state after startup recovery failed",
                        extra={
                            "event": "startup_topic_recovery_state_check_failed",
                            "ticket_id": ticket_id,
                        },
                    )
                    continue
                if current.topic_id is not None:
                    # Attachment and WAITING_TOPIC release share one commit. A
                    # post-attach customer-card failure must not misreport the
                    # durable recovery itself as failed.
                    recovered += 1
                    logger.warning(
                        "Recovered topic after restart but post-attach setup failed",
                        exc_info=True,
                        extra={
                            "event": "startup_topic_recovery_partially_succeeded",
                            "ticket_id": ticket_id,
                            "topic_id": current.topic_id,
                        },
                    )
                    continue
                logger.exception(
                    "Unable to recover unclaimed topic work after restart",
                    extra={
                        "event": "startup_topic_recovery_failed",
                        "ticket_id": ticket_id,
                    },
                )
        return recovered

    async def _sync_ticket_topic(self, ticket: TicketView) -> bool:
        if ticket.topic_id is None:
            logger.info(
                "Skipped topic synchronization because ticket has no topic",
                extra={"event": "topic_sync_skipped", "ticket_id": ticket.id},
            )
            return False
        logger.info(
            "Synchronizing support topic",
            extra={
                "event": "topic_sync_started",
                "ticket_id": ticket.id,
                "topic_id": ticket.topic_id,
            },
        )
        try:
            await self.limiter.wait()
            await self.bot.edit_forum_topic(
                chat_id=self.settings.support_group_id,
                message_thread_id=ticket.topic_id,
                name=self._topic_name(ticket, closed=ticket.status.value == "closed"),
            )
        except TelegramAPIError as error:
            logger.exception(
                "Unable to synchronize forum topic",
                exc_info=True,
                extra={
                    "ticket_id": ticket.id,
                    "topic_id": ticket.topic_id,
                    "event": "topic_sync_failed",
                },
            )
            if is_missing_topic_error(error):
                await self.ticket_service.invalidate_topic(
                    ticket_id=ticket.id, topic_id=ticket.topic_id
                )
            return False
        logger.info(
            "Synchronized support topic",
            extra={
                "event": "topic_sync_succeeded",
                "ticket_id": ticket.id,
                "topic_id": ticket.topic_id,
            },
        )
        return True

    async def _ensure_topic(
        self, ticket: TicketView, *, recover_closed: bool = False, reconcile_missing: bool = False
    ) -> TicketView:
        if ticket.topic_id is not None:
            logger.info(
                "Ticket already has a support topic",
                extra={
                    "event": "topic_already_attached",
                    "ticket_id": ticket.id,
                    "topic_id": ticket.topic_id,
                },
            )
            return ticket
        token = (
            await self.ticket_service.claim_topic_reconciliation(ticket.id)
            if reconcile_missing
            else (
                await self.ticket_service.claim_closed_topic_recovery(ticket.id)
                if recover_closed
                else await self.ticket_service.claim_topic_provisioning(ticket.id)
            )
        )
        if token is None:
            logger.info(
                "Topic provisioning is already claimed",
                extra={"event": "topic_claim_already_exists", "ticket_id": ticket.id},
            )
            return await self.ticket_service.get_ticket(ticket.id)
        logger.info(
            "Claimed support topic provisioning",
            extra={"event": "topic_claimed", "ticket_id": ticket.id},
        )
        # The ticket may have been closed while this caller was waiting for the
        # conditional claim. Close cancels the claim, so do not create an orphan
        # topic when that transition is already visible.
        ticket = await self.ticket_service.get_ticket(ticket.id)
        if ticket.status.value == "closed" and not (recover_closed or reconcile_missing):
            return ticket
        topic_attached = False
        try:
            await self.limiter.wait()
            topic = await self.bot.create_forum_topic(
                chat_id=self.settings.support_group_id,
                name=self._topic_name(ticket, closed=ticket.status.value == "closed"),
            )
            logger.info(
                "Created support topic in Telegram",
                extra={
                    "event": "topic_created",
                    "ticket_id": ticket.id,
                    "topic_id": topic.message_thread_id,
                },
            )
            ticket = await self.ticket_service.attach_topic(
                ticket.id,
                topic.message_thread_id,
                token=token,
            )
            topic_attached = True
            logger.info(
                "Attached support topic to ticket",
                extra={
                    "event": "topic_attached",
                    "ticket_id": ticket.id,
                    "topic_id": ticket.topic_id,
                },
            )
            await self._send_customer_card(ticket, event="topic_customer_card_sent")
            return ticket
        except TopicProvisioningConflictError:
            logger.warning(
                "Rejected a stale support topic attachment",
                extra={
                    "event": "topic_attachment_stale",
                    "ticket_id": ticket.id,
                    "topic_id": topic.message_thread_id,
                },
            )
            try:
                await self.limiter.wait()
                await self.bot.delete_forum_topic(
                    chat_id=self.settings.support_group_id,
                    message_thread_id=topic.message_thread_id,
                )
                logger.info(
                    "Deleted the unattached stale support topic",
                    extra={
                        "event": "stale_topic_deleted",
                        "ticket_id": ticket.id,
                        "topic_id": topic.message_thread_id,
                    },
                )
            except Exception:
                # The database has already rejected the stale result. Cleanup is
                # best-effort and must never overwrite the ticket's current state.
                logger.exception(
                    "Unable to delete stale support topic; manual cleanup is required",
                    extra={
                        "event": "stale_topic_manual_cleanup_required",
                        "ticket_id": ticket.id,
                        "topic_id": topic.message_thread_id,
                    },
                )
            return await self.ticket_service.get_ticket(ticket.id)
        except TelegramAPIError:
            if topic_attached:
                logger.exception(
                    "Support topic was attached but post-attach setup failed",
                    exc_info=True,
                    extra={
                        "event": "topic_post_attach_setup_failed",
                        "ticket_id": ticket.id,
                        "topic_id": ticket.topic_id,
                    },
                )
            else:
                logger.exception(
                    "Topic creation outcome is unknown; preserving claim for manual recovery",
                    exc_info=True,
                    extra={"event": "topic_provisioning_uncertain", "ticket_id": ticket.id},
                )
            raise
        except Exception:
            if topic_attached:
                logger.exception(
                    "Support topic was attached but post-attach setup failed",
                    extra={
                        "event": "topic_post_attach_setup_failed",
                        "ticket_id": ticket.id,
                        "topic_id": ticket.topic_id,
                    },
                )
            else:
                logger.exception(
                    "Topic creation outcome is unknown; preserving claim for manual recovery",
                    extra={"ticket_id": ticket.id, "event": "topic_provisioning_uncertain"},
                )
            raise

    async def _send_customer_card(self, ticket: TicketView, *, event: str) -> None:
        if ticket.topic_id is None:
            logger.info(
                "Skipped customer card because ticket has no topic",
                extra={"event": "customer_card_skipped", "ticket_id": ticket.id},
            )
            return
        await self.limiter.wait()
        await self.bot.send_message(
            chat_id=self.settings.support_group_id,
            message_thread_id=ticket.topic_id,
            text=await self._customer_card(ticket),
        )
        logger.info(
            "Sent customer card to support topic",
            extra={
                "event": event,
                "ticket_id": ticket.id,
                "topic_id": ticket.topic_id,
            },
        )

    async def _customer_card(self, ticket: TicketView) -> str:
        identity_parts = []
        if ticket.display_name:
            identity_parts.append(escape(ticket.display_name))
        if ticket.username:
            identity_parts.append(f"@{escape(ticket.username)}")
        identity = " · ".join(identity_parts) or "Без имени"
        return (
            "👤 <b>Клиент</b>\n\n"
            f"<b>{identity}</b>\n"
            f"Telegram ID: <code>{ticket.telegram_user_id}</code>\n"
            f"Тикет: <code>{escape(ticket.id)}</code>\n\n"
            f"{await self._subscription_block(ticket)}"
        )

    async def _subscription_block(self, ticket: TicketView) -> str:
        if self.panel_service is None:
            return "💳 <b>Подписка Remnawave</b>\n\nИнтеграция не подключена."
        lookup = await self.panel_service.get_subscription_for_ticket(ticket)
        return self._format_subscription_lookup(lookup)
