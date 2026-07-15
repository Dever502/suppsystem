from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from supportbot.audit import record_event
from supportbot.durable_work import enqueue_panel_reconciliation
from supportbot.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
    Ticket,
)
from supportbot.panel_persistence_service import PanelPersistenceService
from supportbot.panel_types import (
    Mutation,
)
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
    action_status_from_error as _action_status_from_error,
)
from supportbot.panel_types import (
    action_status_from_lookup as _action_status_from_lookup,
)
from supportbot.panel_types import (
    mutation_user as _mutation_user,
)
from supportbot.panel_types import (
    optional_result_int as _optional_result_int,
)
from supportbot.panel_types import (
    safe_subscription_context as _safe_subscription_context,
)
from supportbot.remnawave import RemnawaveError, RemnawaveUnknownOutcomeError
from supportbot.service_types import TicketView


class PanelReconciliationService(PanelPersistenceService):
    async def recover_interrupted_actions(self) -> int:
        """Recover in-flight actions without depending on Remnawave at startup."""

        database = self._require_database()
        async with database.session() as session:
            actions = list(
                (
                    await session.scalars(
                        select(OperatorAction).where(
                            OperatorAction.action.startswith("remnawave_"),
                            OperatorAction.result == "started",
                        )
                    )
                ).all()
            )
            for action in actions:
                if action.action == "remnawave_revoke_subscription_link":
                    intent = await session.scalar(
                        select(NotificationOutbox).where(
                            NotificationOutbox.operator_action_id == action.id
                        )
                    )
                    if intent is not None and not isinstance(
                        intent.payload.get("before_subscription_url"), str
                    ):
                        action.result = "not_applied"
                        action.payload = {
                            **action.payload,
                            "requires_reconcile": False,
                            "recovery_reason": "interrupted_before_mutation",
                        }
                        intent.status = NotificationStatus.CANCELLED
                        continue
                action.result = "unknown"
                action.payload = {
                    **action.payload,
                    "requires_reconcile": True,
                    "recovery_reason": "process_interrupted",
                }
            await session.commit()

        for action in actions:
            record_event(
                "panel_action_recovery_required",
                ticket_id=action.ticket_id,
                panel_action=action.action,
            )
        return len(actions)

    async def _run_ticket_action(
        self,
        *,
        ticket: TicketView,
        operator_telegram_id: int,
        action: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        mutation: Mutation,
    ) -> PanelActionResult:
        identity_provider = "telegram"
        identity_value = str(ticket.telegram_user_id)
        if action == "extend_subscription":
            extend_days = request_payload.get("extend_days")
            if not isinstance(extend_days, int) or not 1 <= extend_days <= 9999:
                return PanelActionResult(
                    action=action,
                    status="validation_error",
                    changed=False,
                    identity_provider=identity_provider,
                    identity_value=identity_value,
                )

        reserved = await self._reserve_action(
            ticket=ticket,
            operator_telegram_id=operator_telegram_id,
            action=action,
            idempotency_key=idempotency_key,
            payload={
                "identity_provider": identity_provider,
                "identity_value": identity_value,
                **request_payload,
            },
        )
        if reserved != "reserved":
            return PanelActionResult(
                action=action,
                status=reserved,
                changed=False,
                identity_provider=identity_provider,
                identity_value=identity_value,
            )

        lookup = await self.get_subscription_for_ticket(ticket)
        if lookup.subscription is None:
            status = _action_status_from_lookup(lookup.status)
            await self._finish_action(idempotency_key, status, {})
            self._record_panel_event(ticket, operator_telegram_id, action, status)
            return PanelActionResult(
                action=action,
                status=status,
                changed=False,
                identity_provider=identity_provider,
                identity_value=identity_value,
            )

        subscription = lookup.subscription
        if action == "revoke_subscription_link" and not await self._prepare_revoke_intent(
            idempotency_key,
            subscription,
        ):
            status = "unexpected_response"
            await self._finish_action(idempotency_key, status, {})
            self._record_panel_event(ticket, operator_telegram_id, action, status)
            return PanelActionResult(
                action=action,
                status=status,
                changed=False,
                identity_provider=identity_provider,
                identity_value=identity_value,
            )
        result_subscription = subscription
        mutation_user = _mutation_user(subscription)
        if action in {"extend_subscription", "revoke_subscription_link"}:
            # Persist reconciliation before calling Remnawave. A crash or timeout
            # after the external mutation can then be resolved by the worker.
            await self._queue_durable_reconciliation(
                ticket=ticket,
                action=action,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                before=subscription,
            )
        try:
            status, result_payload = await mutation(mutation_user)
        except RemnawaveUnknownOutcomeError:
            if action in {"extend_subscription", "revoke_subscription_link"}:
                status = "unknown"
                result_payload = {
                    "automatic_reconcile": "queued",
                    "reconciliation_pending": True,
                }
            else:
                status = "unknown"
                result_payload = {
                    "automatic_reconcile": "manual_review_required",
                    "requires_reconcile": True,
                }
        except RemnawaveError as error:
            status = _action_status_from_error(error)
            result_payload = {}

        new_subscription_url = result_payload.pop("_new_subscription_url", None)
        if action == "revoke_subscription_link" and isinstance(new_subscription_url, str):
            result_subscription = replace(subscription, subscription_url=new_subscription_url)
        audit_payload = {
            **_safe_subscription_context(subscription),
            **result_payload,
            **({"requires_reconcile": True} if status == "unknown" else {}),
        }
        if action == "revoke_subscription_link" and status == "completed":
            status = await self._complete_revoke_with_notification(
                idempotency_key=idempotency_key,
                audit_payload=audit_payload,
                subscription=result_subscription,
            )
        else:
            await self._finish_action(idempotency_key, status, audit_payload)
        self._record_panel_event(ticket, operator_telegram_id, action, status)
        return PanelActionResult(
            action=action,
            status=status,
            changed=status == "completed",
            identity_provider=identity_provider,
            identity_value=identity_value,
            subscription=result_subscription if status == "completed" else None,
            affected_rows=_optional_result_int(result_payload.get("affected_rows")),
            devices_removed=_optional_result_int(result_payload.get("devices_removed")),
        )

    async def _queue_durable_reconciliation(
        self,
        *,
        ticket: TicketView,
        action: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        before: PanelSubscriptionInfo,
    ) -> None:
        database = self._require_database()
        async with database.session() as session:
            operator_action = await session.scalar(
                select(OperatorAction).where(OperatorAction.idempotency_key == idempotency_key)
            )
            if operator_action is None:
                raise RuntimeError("reserved Remnawave action is missing")
            await enqueue_panel_reconciliation(
                session,
                ticket_id=ticket.id,
                operator_action_id=operator_action.id,
                delay_seconds=self.reconcile_delay_seconds,
                payload={
                    "action": action,
                    "identity_value": str(ticket.telegram_user_id),
                    "request_payload": request_payload,
                    "before_expire_at": before.expire_at.isoformat(),
                    "before_subscription_url": before.subscription_url,
                },
            )
            await session.commit()

    async def reconcile_durable_action(
        self, operator_action_id: str, payload: dict[str, object]
    ) -> bool:
        database = self._require_database()
        async with database.session() as session:
            current_action = await session.get(OperatorAction, operator_action_id)
            if current_action is None or current_action.result not in {"started", "unknown"}:
                return True
        identity_value = payload.get("identity_value")
        action_name = payload.get("action")
        if not isinstance(identity_value, str) or not isinstance(action_name, str):
            raise RuntimeError("invalid Remnawave reconciliation payload")
        user = await self.remnawave.get_user_by_telegram_id(int(identity_value))
        applied = False
        if action_name == "extend_subscription":
            before_raw = payload.get("before_expire_at")
            request = payload.get("request_payload")
            if not isinstance(before_raw, str) or not isinstance(request, dict):
                raise RuntimeError("invalid gift reconciliation payload")
            extend_days = request.get("extend_days")
            if not isinstance(extend_days, int):
                raise RuntimeError("invalid gift duration")
            expected = datetime.fromisoformat(before_raw) + timedelta(days=extend_days)
            applied = user.expire_at == expected
        elif action_name == "revoke_subscription_link":
            before_url = payload.get("before_subscription_url")
            if not isinstance(before_url, str):
                raise RuntimeError("invalid revoke reconciliation payload")
            applied = user.subscription_url != before_url
        if not applied:
            return False
        async with database.session() as session:
            operator_action = await session.scalar(
                select(OperatorAction)
                .where(OperatorAction.id == operator_action_id)
                .with_for_update()
            )
            if operator_action is None or operator_action.result not in {"started", "unknown"}:
                return True
            operator_action.result = "completed"
            operator_action.payload = {
                **operator_action.payload,
                "reconciliation_pending": False,
                "automatic_reconcile": "applied",
                "requires_reconcile": False,
            }
            if action_name == "revoke_subscription_link":
                intent = await session.scalar(
                    select(NotificationOutbox).where(
                        NotificationOutbox.operator_action_id == operator_action_id
                    )
                )
                if intent is not None:
                    self._fill_revoke_notification(intent, user=user, recovered=True)
            if self.support_group_id is not None and operator_action.ticket_id is not None:
                ticket = await session.get(Ticket, operator_action.ticket_id)
                if ticket is not None:
                    text = (
                        "✅ Автоматическая сверка Remnawave: подписка продлена."
                        if action_name == "extend_subscription"
                        else "✅ Автоматическая сверка Remnawave: ссылка перевыпущена."
                    )
                    session.add(
                        DeliveryOutbox(
                            ticket_id=ticket.id,
                            direction=Direction.USER_TO_OPERATOR,
                            idempotency_key=(
                                f"panel-reconcile:{operator_action_id}:operator-notification"
                            ),
                            payload={
                                "kind": "send_text",
                                "target_chat_id": self.support_group_id,
                                "target_thread_id": ticket.topic_id,
                                "text": text,
                            },
                            status=(
                                DeliveryStatus.PENDING
                                if ticket.topic_id is not None
                                else DeliveryStatus.WAITING_TOPIC
                            ),
                        )
                    )
            await session.commit()
        return True
