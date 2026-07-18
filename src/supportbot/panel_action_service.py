from __future__ import annotations

from typing import Any

from supportbot.panel_reconciliation_service import PanelReconciliationService
from supportbot.panel_types import PanelActionResult, PanelActionStatus, safe_remnawave_context
from supportbot.service_types import TicketView


class PanelActionService(PanelReconciliationService):
    async def extend_subscription_for_ticket(
        self,
        *,
        ticket: TicketView,
        operator_telegram_id: int,
        extend_days: int,
        idempotency_key: str,
    ) -> PanelActionResult:
        async def mutate(user_uuid: str) -> tuple[PanelActionStatus, dict[str, Any]]:
            result = await self._operator().extend_user_expiration(
                user_uuid=user_uuid, extend_days=extend_days
            )
            return (
                "completed" if result.affected_rows == 1 else "unexpected_response",
                {"affected_rows": result.affected_rows},
            )

        return await self._run_ticket_action(
            ticket=ticket,
            operator_telegram_id=operator_telegram_id,
            action="extend_subscription",
            idempotency_key=idempotency_key,
            request_payload={"extend_days": extend_days},
            mutation=mutate,
        )

    async def reset_key_for_ticket(
        self,
        *,
        ticket: TicketView,
        operator_telegram_id: int,
        idempotency_key: str,
    ) -> PanelActionResult:
        async def mutate(user_uuid: str) -> tuple[PanelActionStatus, dict[str, Any]]:
            updated = await self._operator().revoke_user_subscription(
                user_uuid=user_uuid, revoke_only_passwords=True
            )
            return "completed", safe_remnawave_context(updated)

        return await self._run_ticket_action(
            ticket=ticket,
            operator_telegram_id=operator_telegram_id,
            action="reset_key",
            idempotency_key=idempotency_key,
            request_payload={"revoke_only_passwords": True},
            mutation=mutate,
        )

    async def revoke_subscription_link_for_ticket(
        self,
        *,
        ticket: TicketView,
        operator_telegram_id: int,
        idempotency_key: str,
    ) -> PanelActionResult:
        async def mutate(user_uuid: str) -> tuple[PanelActionStatus, dict[str, Any]]:
            updated = await self._operator().revoke_user_subscription(
                user_uuid=user_uuid, revoke_only_passwords=False
            )
            return "completed", {
                **safe_remnawave_context(updated),
                "_new_subscription_url": updated.subscription_url,
            }

        return await self._run_ticket_action(
            ticket=ticket,
            operator_telegram_id=operator_telegram_id,
            action="revoke_subscription_link",
            idempotency_key=idempotency_key,
            request_payload={"revoke_only_passwords": False},
            mutation=mutate,
        )

    async def reset_devices_for_ticket(
        self,
        *,
        ticket: TicketView,
        operator_telegram_id: int,
        idempotency_key: str,
    ) -> PanelActionResult:
        async def mutate(user_uuid: str) -> tuple[PanelActionStatus, dict[str, Any]]:
            result = await self._operator().reset_user_hwid_devices(user_uuid=user_uuid)
            return "completed", {"devices_removed": result.total}

        return await self._run_ticket_action(
            ticket=ticket,
            operator_telegram_id=operator_telegram_id,
            action="reset_devices",
            idempotency_key=idempotency_key,
            request_payload={},
            mutation=mutate,
        )
