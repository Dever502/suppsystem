from __future__ import annotations

import logging

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
from supportbot.telegram_constants import (
    TICKET_REOPENED_BY_CUSTOMER_TEXT,
    TICKET_REOPENED_BY_OPERATOR_TEXT,
)
from supportbot.telegram_errors import is_missing_topic_error
from supportbot.telegram_formatting import (
    customer_identity,
    format_subscription_lookup,
    topic_name,
)
from supportbot.telegram_limits import TelegramRateLimiter
from supportbot.telegram_locks import TicketLockPool

logger = logging.getLogger(__name__)


class TelegramTopicManager:
    bot: Bot
    ticket_service: TicketService
    settings: Settings
    panel_service: PanelService | None
    limiter: TelegramRateLimiter
    _ticket_locks: TicketLockPool

    async def _refresh_reopened_ticket_context(self, ticket: TicketView) -> None:
        await self._sync_ticket_topic(ticket)
        await self._send_ticket_reopened_notice(ticket, by_operator=False)
        await self._send_reopened_ticket_customer_card(ticket)

    async def prepare_reopened_customer_topic(self, ticket_id: str) -> None:
        ticket = await self.ticket_service.get_ticket(ticket_id)
        async with self._ticket_locks.hold(ticket.telegram_user_id):
            ticket = await self.ticket_service.get_ticket(ticket_id)
            await self._refresh_reopened_ticket_context(ticket)

    async def _send_reopened_ticket_customer_card(self, ticket: TicketView) -> None:
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

    async def _send_ticket_reopened_notice(self, ticket: TicketView, *, by_operator: bool) -> None:
        if ticket.topic_id is None:
            logger.info(
                "Skipped ticket reopened notice because ticket has no topic",
                extra={"event": "ticket_reopened_notice_skipped", "ticket_id": ticket.id},
            )
            return
        text = TICKET_REOPENED_BY_OPERATOR_TEXT if by_operator else TICKET_REOPENED_BY_CUSTOMER_TEXT
        event = (
            "ticket_reopened_by_operator_notice_sent"
            if by_operator
            else "ticket_reopened_by_customer_notice_sent"
        )
        try:
            await self.limiter.wait()
            await self.bot.send_message(
                chat_id=self.settings.support_group_id,
                message_thread_id=ticket.topic_id,
                text=text,
            )
        except TelegramAPIError:
            logger.warning(
                "Unable to send ticket reopened notice",
                exc_info=True,
                extra={
                    "event": f"{event}_failed",
                    "ticket_id": ticket.id,
                    "topic_id": ticket.topic_id,
                },
            )
        except Exception:
            logger.exception(
                "Unexpected ticket reopened notice failure",
                extra={
                    "event": f"{event}_unexpected_error",
                    "ticket_id": ticket.id,
                    "topic_id": ticket.topic_id,
                },
            )
        else:
            logger.info(
                "Sent ticket reopened notice",
                extra={
                    "event": event,
                    "ticket_id": ticket.id,
                    "topic_id": ticket.topic_id,
                },
            )

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
                name=topic_name(ticket, closed=ticket.status.value == "closed"),
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
                name=topic_name(ticket, closed=ticket.status.value == "closed"),
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
        return (
            "👤 <b>Клиент</b>\n\n"
            f"<b>{customer_identity(ticket)}</b>\n"
            f"Telegram ID: <code>{ticket.telegram_user_id}</code>\n\n"
            f"{await self._subscription_block(ticket)}"
        )

    async def _subscription_block(self, ticket: TicketView) -> str:
        if self.panel_service is None:
            return "💳 <b>Подписка Remnawave</b>\n\nИнтеграция не подключена."
        lookup = await self.panel_service.get_subscription_for_ticket(ticket)
        return format_subscription_lookup(lookup)
