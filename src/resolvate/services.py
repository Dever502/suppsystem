"""Stable public facade for ticket application services."""

from resolvate.channel_ticket_service import ChannelAwareTicketService
from resolvate.outbox_repository import OutboxRepository
from resolvate.service_types import DeliveryJob as DeliveryJob
from resolvate.service_types import NotificationJob as NotificationJob
from resolvate.service_types import TicketView as TicketView
from resolvate.ticket_ingress_service import TicketIngressService
from resolvate.ticket_lifecycle_service import TicketLifecycleService
from resolvate.ticket_message_service import TicketMessageService


class TicketService(
    ChannelAwareTicketService,
    TicketIngressService,
    TicketLifecycleService,
    TicketMessageService,
    OutboxRepository,
):
    """Complete ticket use-case facade used by transports and workers."""
