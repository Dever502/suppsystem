from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from supportbot.audit import record_event
from supportbot.database import Database
from supportbot.models import NotificationOutbox, NotificationStatus, OperatorAction, utcnow
from supportbot.panel_types import (
    PanelActionResult as PanelActionResult,
)
from supportbot.panel_types import (
    PanelActionStatus as PanelActionStatus,
)
from supportbot.panel_types import (
    PanelLookupStatus as PanelLookupStatus,
)
from supportbot.panel_types import (
    PanelSubscriptionInfo as PanelSubscriptionInfo,
)
from supportbot.panel_types import (
    PanelSubscriptionLookup as PanelSubscriptionLookup,
)
from supportbot.panel_types import (
    RemnawaveOperator,
    RemnawaveReader,
)
from supportbot.panel_types import lookup_status_from_error as _lookup_status_from_error
from supportbot.panel_types import subscription_info as _subscription_info
from supportbot.remnawave import RemnawaveError, RemnawaveUser
from supportbot.service_types import TicketView
from supportbot.trace import get_trace_id

GIFT_RECONCILE_ATTEMPTS = 3


class PanelPersistenceService:
    def __init__(
        self,
        remnawave: RemnawaveReader,
        database: Database | None = None,
        *,
        reconcile_delay_seconds: float = 10.0,
        support_group_id: int | None = None,
    ) -> None:
        self.remnawave = remnawave
        self.database = database
        self.reconcile_delay_seconds = reconcile_delay_seconds
        self.support_group_id = support_group_id

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
                status=_lookup_status_from_error(error),
                identity_provider=identity_provider,
                identity_value=identity_value,
            )
        return PanelSubscriptionLookup(
            status="found",
            identity_provider=identity_provider,
            identity_value=identity_value,
            subscription=_subscription_info(user),
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
        async with database.session() as session:
            if await session.scalar(
                select(OperatorAction.id).where(OperatorAction.idempotency_key == idempotency_key)
            ):
                return "duplicate"
            unresolved_action = await session.scalar(
                select(OperatorAction.id).where(
                    OperatorAction.ticket_id == ticket.id,
                    OperatorAction.action.startswith("remnawave_"),
                    OperatorAction.result.in_(("started", "unknown")),
                )
            )
            if unresolved_action is not None:
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
                if await session.scalar(
                    select(OperatorAction.id).where(
                        OperatorAction.idempotency_key == idempotency_key
                    )
                ):
                    return "duplicate"
                if await session.scalar(
                    select(OperatorAction.id).where(
                        OperatorAction.ticket_id == ticket.id,
                        OperatorAction.action.startswith("remnawave_"),
                        OperatorAction.result.in_(("started", "unknown")),
                    )
                ):
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

            self._fill_revoke_notification(intent, subscription=subscription)
            action.result = "completed"
            action.payload = {**action.payload, **audit_payload}
            await session.commit()
            return "completed"

    @staticmethod
    def _fill_revoke_notification(
        intent: NotificationOutbox,
        *,
        subscription: PanelSubscriptionInfo | None = None,
        user: RemnawaveUser | None = None,
        recovered: bool = False,
        outcome_confirmed: bool = True,
    ) -> None:
        if subscription is not None:
            subscription_url = subscription.subscription_url
            username = subscription.username
            user_uuid = subscription.uuid
        elif user is not None:
            subscription_url = user.subscription_url
            username = user.username
            user_uuid = user.uuid
        else:
            raise ValueError("subscription or user is required to fill notification intent")
        intent.payload = {
            "subscription_url": subscription_url,
            "remnawave_username": username,
            "remnawave_uuid": user_uuid,
            **({"recovered_after_restart": True} if recovered else {}),
            **({"revoke_outcome_confirmed": False} if not outcome_confirmed else {}),
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
