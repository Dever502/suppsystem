"""Stable aiogram adapter facade."""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart

from suppsystem.authorization import AuthorizationService
from suppsystem.config import Settings
from suppsystem.panel import PanelService
from suppsystem.services import TicketService
from suppsystem.telegram_constants import (
    COMMAND_ALREADY_HANDLED_TEXT as COMMAND_ALREADY_HANDLED_TEXT,
)
from suppsystem.telegram_constants import (
    PANEL_MUTATION_COMMANDS as PANEL_MUTATION_COMMANDS,
)
from suppsystem.telegram_constants import (
    SUPPORT_PENDING_TEXT as SUPPORT_PENDING_TEXT,
)
from suppsystem.telegram_constants import (
    TICKET_CLOSED_TEXT as TICKET_CLOSED_TEXT,
)
from suppsystem.telegram_constants import TOPIC_COMMANDS as TOPIC_COMMANDS
from suppsystem.telegram_constants import WELCOME_TEXT as WELCOME_TEXT
from suppsystem.telegram_limits import TelegramRateLimiter
from suppsystem.telegram_locks import TicketLockPool as TicketLockPool
from suppsystem.telegram_message_utils import RATING_CALLBACK_PREFIX
from suppsystem.telegram_operator_handlers import TelegramOperatorHandlers
from suppsystem.telegram_panel_handler import (
    GIFT_DAYS_ERROR_TEXT as GIFT_DAYS_ERROR_TEXT,
)
from suppsystem.telegram_panel_handler import TelegramPanelCommandHandler


class TelegramSupportAdapter(TelegramOperatorHandlers):
    """Compose user, operator and topic handlers into one aiogram router."""

    def __init__(
        self,
        *,
        bot: Bot,
        ticket_service: TicketService,
        settings: Settings,
        limiter: TelegramRateLimiter,
        panel_service: PanelService | None = None,
    ) -> None:
        self.bot = bot
        self.ticket_service = ticket_service
        self.settings = settings
        self.panel_service = panel_service
        self.panel_commands = TelegramPanelCommandHandler(panel_service)
        self.authorization = AuthorizationService(settings)
        self.limiter = limiter
        self.router = Router(name="support")
        self._ticket_locks = TicketLockPool()
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.router.message.register(
            self.handle_start,
            CommandStart(),
            F.chat.type == ChatType.PRIVATE,
        )
        self.router.message.register(
            self.handle_private_message,
            F.chat.type == ChatType.PRIVATE,
        )
        self.router.message.register(
            self.handle_group_message,
            F.chat.id == self.settings.support_group_id,
        )
        self.router.callback_query.register(
            self.handle_rating_callback,
            F.data.startswith(f"{RATING_CALLBACK_PREFIX}:"),
        )
