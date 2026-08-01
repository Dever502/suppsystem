"""Stable public facade for ticket application services."""

from suppsystem.channel_ticket_service import ChannelAwareTicketService
from suppsystem.outbox_repository import OutboxRepository
from suppsystem.service_types import DeliveryJob as DeliveryJob
from suppsystem.service_types import NotificationJob as NotificationJob
from suppsystem.service_types import TicketView as TicketView
from suppsystem.ticket_ingress_service import TicketIngressService
from suppsystem.ticket_lifecycle_service import TicketLifecycleService
from suppsystem.ticket_message_service import TicketMessageService


class TicketService(
    ChannelAwareTicketService,
    TicketIngressService,
    TicketLifecycleService,
    TicketMessageService,
    OutboxRepository,
):
    """Complete ticket use-case facade used by transports and workers."""
