"""Stable public facade for ticket application services."""

from supportbot.outbox_repository import OutboxRepository
from supportbot.service_types import DeliveryJob as DeliveryJob
from supportbot.service_types import NotificationJob as NotificationJob
from supportbot.service_types import TicketView as TicketView
from supportbot.ticket_ingress_service import TicketIngressService
from supportbot.ticket_lifecycle_service import TicketLifecycleService
from supportbot.ticket_message_service import TicketMessageService


class TicketService(
    TicketIngressService,
    TicketLifecycleService,
    TicketMessageService,
    OutboxRepository,
):
    """Complete ticket use-case facade used by transports and workers."""
