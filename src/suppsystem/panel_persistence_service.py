from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from suppsystem.audit import record_event
from suppsystem.database import Database
from suppsystem.models import (
    DeliveryOutbox,
    Direction,
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
    TicketMessage,
    utcnow,
)
from suppsystem.panel_notifications import (
    gift_notification_text,
    revoke_link_notification_text,
)
from suppsystem.panel_types import (
    PanelActionStatus,
    PanelSubscriptionInfo,
    PanelSubscriptionLookup,
    RemnawaveOperator,
    RemnawaveReader,
    lookup_status_from_error,
    subscription_info,
)
from suppsystem.remnawave import RemnawaveError, RemnawaveUser
from suppsystem.service_types import TicketView
from suppsystem.trace import get_trace_id


class PanelPersistenceService:
    def __init__(
        self,
        remnawave: RemnawaveReader,
        database: Database | None = None,
        *,
        reconcile_delay_seconds: float = 10.0,
        support_group_id: int | None = None,
        revoke_link_telegram_notification: bool = True,
    ) -> None:
        self.remnawave = remnawave
        self.database = database
        self.reconcile_delay_seconds = reconcile_delay_seconds
        self.support_group_id = support_group_id
        self.revoke_link_telegram_notification = revoke_link_telegram_notification

    def _operator(self) -> RemnawaveOperator:
        return cast(RemnawaveOperator, self.remnawave)

    def _require_database(self) -> Database:
        if self.database is None:
            raise RuntimeError("Panel mutating actions require a Database")
        return self.database

    async def get_subscription_for_ticket(self, ticket: TicketView) -> PanelSubscriptionLookup:
        identity_provider = "telegram"
        identity_value = str(ticket.telegram_user_id)
        try:
            user = await self.remnawave.get_user_by_telegram_id(ticket.telegram_user_id)
        except RemnawaveError as error:
            return PanelSubscriptionLookup(
                status=lookup_status_from_error(error),
                identity_provider=identity_provider,
                identity_value=identity_value,
            )
        return PanelSubscriptionLookup(
            status="found",
            identity_provider=identity_provider,
            identity_value=identity_value,
            subscription=subscription_info(user),
        )

    async def _reserve_action(
        self,
        *,
        ticket: TicketView,
        operator_telegram_id: int,
        action: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> Literal["reserved", "duplicate", "needs_reconcile"]:
        database = self._require_database()
        action_name = f"remnawave_{action}"
        duplicate_query = select(OperatorAction.id).where(
            OperatorAction.idempotency_key == idempotency_key
        )
        unresolved_query = select(OperatorAction.id).where(
            OperatorAction.ticket_id == ticket.id,
            OperatorAction.action.startswith("remnawave_"),
            OperatorAction.result.in_(("started", "unknown")),
        )
        async with database.session() as session:
            if await session.scalar(duplicate_query):
                return "duplicate"
            if await session.scalar(unresolved_query) is not None:
                return "needs_reconcile"
            operator_action = OperatorAction(
                id=str(uuid.uuid4()),
                ticket_id=ticket.id,
                operator_telegram_id=operator_telegram_id,
                action=action_name,
                idempotency_key=idempotency_key,
                payload=payload,
                result="started",
                trace_id=get_trace_id(),
            )
            session.add(operator_action)
            try:
                await session.flush()
                if action == "revoke_subscription_link":
                    session.add(
                        NotificationOutbox(
                            ticket_id=ticket.id,
                            operator_action_id=operator_action.id,
                            idempotency_key=f"{idempotency_key}:user-notification",
                            event_type="subscription_link_reissued",
                            destination="subscription_owner",
                            recipient_identity_provider="telegram",
                            recipient_identity_value=str(ticket.telegram_user_id),
                            payload={},
                            status=NotificationStatus.AWAITING_PAYLOAD,
                        )
                    )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(duplicate_query):
                    return "duplicate"
                if await session.scalar(unresolved_query):
                    return "needs_reconcile"
                raise
            return "reserved"

    async def _prepare_revoke_intent(
        self,
        idempotency_key: str,
        subscription: PanelSubscriptionInfo,
    ) -> bool:
        database = self._require_database()
        async with database.session() as session:
            action = await session.scalar(
                select(OperatorAction).where(OperatorAction.idempotency_key == idempotency_key)
            )
            if action is None:
                return False
            intent = await session.scalar(
                select(NotificationOutbox).where(NotificationOutbox.operator_action_id == action.id)
            )
            if intent is None:
                return False
            intent.payload = {
                "before_subscription_url": subscription.subscription_url,
                "remnawave_username": subscription.username,
                "remnawave_uuid": subscription.uuid,
            }
            await session.commit()
            return True

    async def _complete_revoke_with_notification(
        self,
        *,
        idempotency_key: str,
        audit_payload: dict[str, Any],
        subscription: PanelSubscriptionInfo,
        recipient_telegram_id: int,
    ) -> PanelActionStatus:
        database = self._require_database()
        async with database.session() as session:
            action = await session.scalar(
                select(OperatorAction)
                .where(OperatorAction.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if action is None:
                return "unknown"
            if action.result == "completed":
                return "completed"
            intent = await session.scalar(
                select(NotificationOutbox).where(NotificationOutbox.operator_action_id == action.id)
            )
            if intent is None:
                action.result = "unknown"
                action.payload = {
                    **action.payload,
                    **audit_payload,
                    "requires_reconcile": True,
                    "recovery_reason": "notification_intent_missing",
                }
                await session.commit()
                return "unknown"

            if self.revoke_link_telegram_notification and action.ticket_id is None:
                action.result = "unknown"
                action.payload = {
                    **action.payload,
                    **audit_payload,
                    "requires_reconcile": True,
                    "recovery_reason": "revoke_telegram_notification_ticket_missing",
                }
                await session.commit()
                return "unknown"

            self._fill_revoke_notification(intent, subscription)
            action.result = "completed"
            action.payload = {**action.payload, **audit_payload}
            if self.revoke_link_telegram_notification:
                assert action.ticket_id is not None
                self._queue_revoke_link_telegram_notification(
                    session,
                    ticket_id=action.ticket_id,
                    operator_action_id=action.id,
                    recipient_telegram_id=recipient_telegram_id,
                    subscription_url=subscription.subscription_url,
                )
            await session.commit()
            return "completed"

    async def _complete_gift_with_notification(
        self,
        *,
        idempotency_key: str,
        audit_payload: dict[str, Any],
        extend_days: int,
        expire_at: datetime,
    ) -> PanelActionStatus:
        database = self._require_database()
        async with database.session() as session:
            action = await session.scalar(
                select(OperatorAction)
                .where(OperatorAction.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if action is None:
                return "unknown"
            if action.result == "completed":
                return "completed"
            identity_value = action.payload.get("identity_value")
            if action.ticket_id is None or not isinstance(identity_value, str):
                action.result = "unknown"
                action.payload = {
                    **action.payload,
                    **audit_payload,
                    "requires_reconcile": True,
                    "recovery_reason": "gift_notification_recipient_missing",
                }
                await session.commit()
                return "unknown"
            try:
                recipient_telegram_id = int(identity_value)
            except ValueError:
                action.result = "unknown"
                action.payload = {
                    **action.payload,
                    **audit_payload,
                    "requires_reconcile": True,
                    "recovery_reason": "gift_notification_recipient_invalid",
                }
                await session.commit()
                return "unknown"
            action.result = "completed"
            action.payload = {**action.payload, **audit_payload}
            self._queue_gift_notification(
                session,
                ticket_id=action.ticket_id,
                operator_action_id=action.id,
                recipient_telegram_id=recipient_telegram_id,
                extend_days=extend_days,
                expire_at=expire_at,
            )
            await session.commit()
            return "completed"

    @staticmethod
    def _queue_gift_notification(
        session: AsyncSession,
        *,
        ticket_id: str,
        operator_action_id: str,
        recipient_telegram_id: int,
        extend_days: int,
        expire_at: datetime,
    ) -> None:
        text = gift_notification_text(extend_days, expire_at)
        session.add(
            TicketMessage(
                ticket_id=ticket_id,
                direction=Direction.OPERATOR_TO_USER,
                channel="system",
                content=text,
            )
        )
        session.add(
            DeliveryOutbox(
                ticket_id=ticket_id,
                direction=Direction.OPERATOR_TO_USER,
                idempotency_key=f"panel-gift:{operator_action_id}:user-notification",
                payload={
                    "kind": "send_text",
                    "target_chat_id": recipient_telegram_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
            )
        )

    @staticmethod
    def _queue_revoke_link_telegram_notification(
        session: AsyncSession,
        *,
        ticket_id: str,
        operator_action_id: str,
        recipient_telegram_id: int,
        subscription_url: str,
    ) -> None:
        text = revoke_link_notification_text(subscription_url)
        session.add(
            TicketMessage(
                ticket_id=ticket_id,
                direction=Direction.OPERATOR_TO_USER,
                channel="system",
                content=text,
            )
        )
        session.add(
            DeliveryOutbox(
                ticket_id=ticket_id,
                direction=Direction.OPERATOR_TO_USER,
                idempotency_key=(f"panel-revoke-link:{operator_action_id}:telegram-notification"),
                payload={
                    "kind": "send_text",
                    "target_chat_id": recipient_telegram_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
            )
        )

    @staticmethod
    def _fill_revoke_notification(
        intent: NotificationOutbox,
        subscription: PanelSubscriptionInfo | RemnawaveUser,
        *,
        recovered: bool = False,
    ) -> None:
        intent.payload = {
            "subscription_url": subscription.subscription_url,
            "remnawave_username": subscription.username,
            "remnawave_uuid": subscription.uuid,
            **({"recovered_after_restart": True} if recovered else {}),
        }
        intent.status = NotificationStatus.PENDING
        intent.next_attempt_at = utcnow()
        intent.claimed_at = None
        intent.last_error = None

    async def _finish_action(
        self, idempotency_key: str, status: PanelActionStatus, payload_update: dict[str, Any]
    ) -> None:
        database = self._require_database()
        async with database.session() as session:
            action = await session.scalar(
                select(OperatorAction)
                .where(OperatorAction.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if action is None:
                return
            if action.result == "completed" and status != "completed":
                return
            action.result = status
            action.payload = {**action.payload, **payload_update}
            if action.action == "remnawave_revoke_subscription_link" and status not in {
                "completed",
                "unknown",
            }:
                intent = await session.scalar(
                    select(NotificationOutbox).where(
                        NotificationOutbox.operator_action_id == action.id
                    )
                )
                if intent is not None:
                    intent.status = NotificationStatus.CANCELLED
            await session.commit()

    @staticmethod
    def _record_panel_event(
        ticket: TicketView, operator_telegram_id: int, action: str, status: PanelActionStatus
    ) -> None:
        record_event(
            "panel_action_completed" if status == "completed" else "panel_action_failed",
            ticket_id=ticket.id,
            operator_telegram_id=operator_telegram_id,
            panel="remnawave",
            panel_action=action,
            panel_action_status=status,
        )
