from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from supportbot.audit import record_event
from supportbot.durable_work import MAX_RECONCILIATION_ATTEMPTS, enqueue_panel_reconciliation
from supportbot.models import (
    DeliveryOutbox,
    DeliveryStatus,
    Direction,
    NotificationOutbox,
    NotificationStatus,
    OperatorAction,
    ReconciliationOutbox,
    Ticket,
    WorkStatus,
    utcnow,
)
from supportbot.panel_persistence_service import PanelPersistenceService
from supportbot.panel_types import (
    Mutation,
    PanelActionResult,
    PanelSubscriptionInfo,
    action_status_from_error,
    action_status_from_lookup,
    optional_result_int,
    safe_remnawave_context,
)
from supportbot.remnawave import (
    RemnawaveError,
    RemnawaveUnexpectedResponseError,
    RemnawaveUnknownOutcomeError,
    RemnawaveUser,
)
from supportbot.service_types import TicketView
from supportbot.trace import get_trace_id

RECONCILE_ATTEMPTS = 3


class ReconciliationPayloadError(RuntimeError):
    pass


class PanelReconciliationService(PanelPersistenceService):
    async def recover_interrupted_actions(self) -> int:
        """Classify interrupted actions from durable pre-mutation evidence."""

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
                job = await session.scalar(
                    select(ReconciliationOutbox).where(
                        ReconciliationOutbox.operator_action_id == action.id
                    )
                )
                intent = (
                    await session.scalar(
                        select(NotificationOutbox).where(
                            NotificationOutbox.operator_action_id == action.id
                        )
                    )
                    if action.action == "remnawave_revoke_subscription_link"
                    else None
                )
                if job is None:
                    action.result = "not_applied"
                    action.payload = {
                        **action.payload,
                        "requires_reconcile": False,
                        "recovery_reason": "interrupted_before_mutation",
                    }
                    if intent is not None:
                        intent.status = NotificationStatus.CANCELLED
                    continue
                action.result = "unknown"
                action.payload = {
                    **action.payload,
                    "requires_reconcile": True,
                    "recovery_reason": "process_interrupted",
                }
                job.status = WorkStatus.PENDING
                job.next_attempt_at = utcnow()
                job.claimed_at = None
                job.claim_token = None
            await session.commit()

        for action in actions:
            record_event(
                "panel_action_recovery_required",
                ticket_id=action.ticket_id,
                panel_action=action.action,
                panel_action_status=action.result,
            )
        return len(actions)

    async def resolve_inconclusive_action(
        self,
        *,
        ticket_id: str,
        operator_action_id: str,
        operator_telegram_id: int,
        resolution: str,
        idempotency_key: str,
    ) -> bool:
        """Record an evidence-backed manual decision without repeating the mutation."""

        if resolution not in {"applied", "not_applied"}:
            raise ValueError("resolution must be applied or not_applied")
        database = self._require_database()
        duplicate_query = select(OperatorAction.id).where(
            OperatorAction.idempotency_key == idempotency_key
        )
        allowed_actions = (
            "remnawave_extend_subscription",
            "remnawave_revoke_subscription_link",
            "remnawave_reset_key",
            "remnawave_reset_devices",
        )
        revoke_identity_value: str | None = None
        async with database.session() as session:
            if await session.scalar(duplicate_query):
                return False
            action = await session.scalar(
                select(OperatorAction).where(
                    OperatorAction.id == operator_action_id,
                    OperatorAction.ticket_id == ticket_id,
                    OperatorAction.action.in_(allowed_actions),
                    OperatorAction.result == "unknown",
                )
            )
            if action is None or action.payload.get("automatic_reconcile") != "inconclusive":
                return False
            action_name = action.action
            action_payload = dict(action.payload)
            revoke_before_url: str | None = None
            if action_name == "remnawave_revoke_subscription_link" and resolution == "applied":
                intent = await session.scalar(
                    select(NotificationOutbox).where(
                        NotificationOutbox.operator_action_id == operator_action_id,
                        NotificationOutbox.status == NotificationStatus.AWAITING_PAYLOAD,
                    )
                )
                identity_value = action.payload.get("identity_value")
                if intent is None or not isinstance(identity_value, str):
                    return False
                raw_before_url = intent.payload.get("before_subscription_url")
                if not isinstance(raw_before_url, str):
                    return False
                revoke_before_url = raw_before_url
                revoke_identity_value = identity_value

        revoke_user: RemnawaveUser | None = None
        if revoke_before_url is not None:
            assert revoke_identity_value is not None
            try:
                revoke_user = await self.remnawave.get_user_by_telegram_id(
                    int(revoke_identity_value)
                )
            except (RemnawaveError, ValueError):
                return False
            if revoke_user.subscription_url == revoke_before_url:
                return False

        reconciled_at = utcnow()
        manual_payload = {
            **action_payload,
            "requires_reconcile": False,
            "reconciliation_pending": False,
            "manual_reconcile": resolution,
            "manual_reconciled_by_telegram_id": operator_telegram_id,
            "manual_reconciled_at": reconciled_at.isoformat(),
            "manual_reconcile_command_key": idempotency_key,
        }

        async with database.session() as session:
            updated = await session.execute(
                update(OperatorAction)
                .where(
                    OperatorAction.id == operator_action_id,
                    OperatorAction.ticket_id == ticket_id,
                    OperatorAction.action.in_(allowed_actions),
                    OperatorAction.result == "unknown",
                )
                .values(
                    result="completed" if resolution == "applied" else "not_applied",
                    payload=manual_payload,
                    updated_at=reconciled_at,
                    completed_at=reconciled_at,
                )
                .returning(OperatorAction.action)
            )
            row = updated.first()
            if row is None:
                await session.rollback()
                return False
            if row.action == "remnawave_revoke_subscription_link":
                intent = await session.scalar(
                    select(NotificationOutbox).where(
                        NotificationOutbox.operator_action_id == operator_action_id,
                        NotificationOutbox.status == NotificationStatus.AWAITING_PAYLOAD,
                    )
                )
                if intent is not None:
                    if resolution == "applied":
                        if revoke_user is None:
                            await session.rollback()
                            return False
                        self._fill_revoke_notification(intent, revoke_user, recovered=True)
                    else:
                        intent.status = NotificationStatus.CANCELLED
                else:
                    await session.rollback()
                    return False
            session.add(
                OperatorAction(
                    ticket_id=ticket_id,
                    operator_telegram_id=operator_telegram_id,
                    action="resolve_remnawave_action",
                    idempotency_key=idempotency_key,
                    payload={
                        "operator_action_id": operator_action_id,
                        "resolution": resolution,
                    },
                    result="completed",
                    trace_id=get_trace_id(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                if await session.scalar(duplicate_query):
                    return False
                raise
        record_event(
            "panel_action_manually_reconciled",
            ticket_id=ticket_id,
            operator_telegram_id=operator_telegram_id,
            operator_action_id=operator_action_id,
            panel_action=action_name,
            panel_action_status=resolution,
        )
        return True

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
            status = action_status_from_lookup(lookup.status)
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
        try:
            reconciliation_payload = self._reconciliation_payload(
                action=action, request_payload=request_payload, before=subscription
            )
        except RemnawaveError as error:
            status = action_status_from_error(error)
            await self._finish_action(idempotency_key, status, {})
            self._record_panel_event(ticket, operator_telegram_id, action, status)
            return PanelActionResult(
                action=action,
                status=status,
                changed=False,
                identity_provider=identity_provider,
                identity_value=identity_value,
            )
        # Durable evidence is committed before every external mutation.
        await self._queue_durable_reconciliation(
            ticket=ticket,
            idempotency_key=idempotency_key,
            payload=reconciliation_payload,
        )
        try:
            status, result_payload = await mutation(subscription.uuid)
        except RemnawaveUnknownOutcomeError:
            status = "unknown"
            result_payload = {
                "automatic_reconcile": "queued",
                "reconciliation_pending": True,
            }
        except RemnawaveError as error:
            status = action_status_from_error(error)
            result_payload = {}

        new_subscription_url = result_payload.pop("_new_subscription_url", None)
        if action == "revoke_subscription_link" and isinstance(new_subscription_url, str):
            result_subscription = replace(subscription, subscription_url=new_subscription_url)
        audit_payload = {
            **safe_remnawave_context(subscription),
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
            affected_rows=optional_result_int(result_payload.get("affected_rows")),
            devices_removed=optional_result_int(result_payload.get("devices_removed")),
        )

    def _reconciliation_payload(
        self,
        *,
        action: str,
        request_payload: dict[str, Any],
        before: PanelSubscriptionInfo,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": action,
            "request_payload": request_payload,
        }
        if action == "extend_subscription":
            payload["before_expire_at"] = before.expire_at.isoformat()
        elif action == "revoke_subscription_link":
            payload["before_subscription_url"] = before.subscription_url
        elif action == "reset_key":
            if before.credential_fingerprint is None:
                raise RemnawaveUnexpectedResponseError("Remnawave 2.8 user credentials are missing")
            payload["before_credential_fingerprint"] = before.credential_fingerprint
        elif action == "reset_devices":
            pass
        else:
            raise RuntimeError(f"unsupported Remnawave action: {action}")
        return payload

    async def _queue_durable_reconciliation(
        self,
        *,
        ticket: TicketView,
        idempotency_key: str,
        payload: dict[str, object],
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
                    "identity_value": str(ticket.telegram_user_id),
                    **payload,
                },
            )
            await session.commit()

    async def reconcile_durable_action(
        self,
        operator_action_id: str,
        payload: dict[str, object],
        *,
        attempt_count: int = 1,
    ) -> bool:
        database = self._require_database()
        async with database.session() as session:
            current_action = await session.get(OperatorAction, operator_action_id)
            if current_action is None or current_action.result not in {"started", "unknown"}:
                return True
            if current_action.payload.get("automatic_reconcile") == "inconclusive":
                return True
            if current_action.result == "started":
                return False
            action_payload = dict(current_action.payload)
            action_updated_at = current_action.updated_at
            stored_action_name = current_action.action.removeprefix("remnawave_")

        outcome: Literal["applied", "not_applied", "inconclusive"]
        user: RemnawaveUser | None = None
        terminal_error: Exception | None = None
        terminal_failure: str | None = None
        try:
            identity_value = payload.get("identity_value")
            action_name = payload.get("action")
            if not isinstance(identity_value, str) or not isinstance(action_name, str):
                raise ReconciliationPayloadError("invalid Remnawave reconciliation payload")
            if action_name != stored_action_name:
                raise ReconciliationPayloadError("Remnawave reconciliation action mismatch")
            try:
                telegram_id = int(identity_value)
            except ValueError as error:
                raise ReconciliationPayloadError(
                    "invalid Remnawave reconciliation identity"
                ) from error
            user = await self.remnawave.get_user_by_telegram_id(telegram_id)
            if action_name == "extend_subscription":
                before_raw = payload.get("before_expire_at")
                request = payload.get("request_payload")
                if not isinstance(before_raw, str) or not isinstance(request, dict):
                    raise ReconciliationPayloadError("invalid gift reconciliation payload")
                extend_days = request.get("extend_days")
                if not isinstance(extend_days, int) or not 1 <= extend_days <= 9999:
                    raise ReconciliationPayloadError("invalid gift duration")
                try:
                    before = datetime.fromisoformat(before_raw)
                    expected = before + timedelta(days=extend_days)
                except (OverflowError, ValueError) as error:
                    raise ReconciliationPayloadError("invalid gift timestamp") from error
                outcome = (
                    "applied"
                    if user.expire_at == expected
                    else "not_applied"
                    if user.expire_at == before
                    else "inconclusive"
                )
            elif action_name == "revoke_subscription_link":
                before_url = payload.get("before_subscription_url")
                if not isinstance(before_url, str):
                    raise ReconciliationPayloadError("invalid revoke reconciliation payload")
                outcome = "applied" if user.subscription_url != before_url else "not_applied"
            elif action_name == "reset_key":
                before_fingerprint = payload.get("before_credential_fingerprint")
                if not isinstance(before_fingerprint, str):
                    raise ReconciliationPayloadError("invalid reset-key reconciliation payload")
                if user.credential_fingerprint is None:
                    raise RemnawaveUnexpectedResponseError(
                        "Remnawave 2.8 user credentials are missing"
                    )
                outcome = (
                    "applied"
                    if user.credential_fingerprint != before_fingerprint
                    else "not_applied"
                )
            elif action_name == "reset_devices":
                devices = await self._operator().get_user_hwid_devices(user_uuid=user.uuid)
                outcome = "applied" if devices.total == 0 else "inconclusive"
            else:
                raise ReconciliationPayloadError(f"unsupported Remnawave action: {action_name}")
        except ReconciliationPayloadError as error:
            action_name = stored_action_name
            outcome = "inconclusive"
            terminal_error = error
            terminal_failure = "invalid_payload"
        except RemnawaveError as error:
            if attempt_count < MAX_RECONCILIATION_ATTEMPTS:
                raise
            action_name = stored_action_name
            outcome = "inconclusive"
            terminal_error = error
            terminal_failure = "attempts_exhausted"
        if terminal_error is None and outcome != "applied" and attempt_count < RECONCILE_ATTEMPTS:
            return False

        reconciled_at = utcnow()
        reconciled_payload = {
            **action_payload,
            "reconciliation_pending": False,
            "automatic_reconcile": outcome,
            "requires_reconcile": outcome == "inconclusive",
        }
        if terminal_error is not None:
            reconciled_payload.update(
                reconciliation_failure=terminal_failure,
                reconciliation_error_type=type(terminal_error).__name__,
            )
        async with database.session() as session:
            updated = await session.execute(
                update(OperatorAction)
                .where(
                    OperatorAction.id == operator_action_id,
                    OperatorAction.result == "unknown",
                    OperatorAction.updated_at == action_updated_at,
                )
                .values(
                    result={
                        "applied": "completed",
                        "not_applied": "not_applied",
                        "inconclusive": "unknown",
                    }[outcome],
                    payload=reconciled_payload,
                    updated_at=reconciled_at,
                    completed_at=reconciled_at if outcome != "inconclusive" else None,
                )
                .returning(OperatorAction.ticket_id)
            )
            row = updated.first()
            if row is None:
                await session.rollback()
                return True
            if action_name == "revoke_subscription_link":
                intent = await session.scalar(
                    select(NotificationOutbox).where(
                        NotificationOutbox.operator_action_id == operator_action_id
                    )
                )
                if intent is not None:
                    if outcome == "applied":
                        assert user is not None
                        self._fill_revoke_notification(intent, user, recovered=True)
                    elif outcome == "not_applied":
                        intent.status = NotificationStatus.CANCELLED
            if self.support_group_id is not None and row.ticket_id is not None:
                ticket = await session.get(Ticket, row.ticket_id)
                if ticket is not None:
                    if outcome == "applied":
                        completed_text = {
                            "extend_subscription": "подписка продлена",
                            "revoke_subscription_link": "ссылка перевыпущена",
                            "reset_key": "ключи обновлены",
                            "reset_devices": "устройства сброшены",
                        }[action_name]
                        text = f"✅ Автоматическая сверка Remnawave: {completed_text}."
                    elif outcome == "not_applied":
                        text = (
                            "⚠️ Автоматическая сверка Remnawave: изменение не обнаружено; "
                            "команду можно повторить."
                        )
                    else:
                        text = (
                            "⚠️ Автоматическая сверка Remnawave не смогла однозначно "
                            "определить результат; не повторяйте команду до ручной проверки.\n"
                            f"Action ID: <code>{operator_action_id}</code>\n"
                            "После проверки: <code>/resolvepanel &lt;action_uuid&gt; "
                            "applied|not_applied</code>"
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
                                "parse_mode": "HTML",
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
