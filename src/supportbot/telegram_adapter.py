"""Stable aiogram adapter facade."""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart

from supportbot.authorization import AuthorizationService
from supportbot.config import Settings
from supportbot.panel import PanelService
from supportbot.services import TicketService
from supportbot.telegram_constants import (
    COMMAND_ALREADY_HANDLED_TEXT as COMMAND_ALREADY_HANDLED_TEXT,
)
from supportbot.telegram_constants import (
    PANEL_MUTATION_COMMANDS as PANEL_MUTATION_COMMANDS,
)
from supportbot.telegram_constants import (
    SUPPORT_PENDING_TEXT as SUPPORT_PENDING_TEXT,
)
from supportbot.telegram_constants import (
    TICKET_CLOSED_TEXT as TICKET_CLOSED_TEXT,
)
from supportbot.telegram_constants import TOPIC_COMMANDS as TOPIC_COMMANDS
from supportbot.telegram_constants import WELCOME_TEXT as WELCOME_TEXT
from supportbot.telegram_limits import TelegramRateLimiter
from supportbot.telegram_locks import TicketLockPool as TicketLockPool
from supportbot.telegram_message_utils import RATING_CALLBACK_PREFIX
from supportbot.telegram_operator_handlers import TelegramOperatorHandlers
from supportbot.telegram_panel_handler import (
    GIFT_DAYS_ERROR_TEXT as GIFT_DAYS_ERROR_TEXT,
)
from supportbot.telegram_panel_handler import TelegramPanelCommandHandler


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
