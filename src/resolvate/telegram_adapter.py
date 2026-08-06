from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart

from resolvate.authorization import AuthorizationService
from resolvate.config import Settings
from resolvate.media_storage import LocalMediaStorage
from resolvate.panel import PanelService
from resolvate.quick_replies import QuickReplyService
from resolvate.services import TicketService
from resolvate.statistics import StatisticsService
from resolvate.telegram_constants import (
    COMMAND_ALREADY_HANDLED_TEXT as COMMAND_ALREADY_HANDLED_TEXT,
)
from resolvate.telegram_constants import (
    PANEL_MUTATION_COMMANDS as PANEL_MUTATION_COMMANDS,
)
from resolvate.telegram_constants import (
    SUPPORT_PENDING_TEXT as SUPPORT_PENDING_TEXT,
)
from resolvate.telegram_constants import (
    TICKET_CLOSED_TEXT as TICKET_CLOSED_TEXT,
)
from resolvate.telegram_constants import TOPIC_COMMANDS as TOPIC_COMMANDS
from resolvate.telegram_constants import WELCOME_TEXT as WELCOME_TEXT
from resolvate.telegram_limits import TelegramRateLimiter
from resolvate.telegram_locks import TicketLockPool as TicketLockPool
from resolvate.telegram_message_utils import RATING_CALLBACK_PREFIX
from resolvate.telegram_operator_handlers import TelegramOperatorHandlers
from resolvate.telegram_panel_handler import (
    GIFT_DAYS_ERROR_TEXT as GIFT_DAYS_ERROR_TEXT,
)
from resolvate.telegram_panel_handler import TelegramPanelCommandHandler
from resolvate.telegram_quick_replies import QUICK_RESPONSE_DELETE_CALLBACK_PREFIX
from resolvate.telegram_statistics import STATISTICS_CALLBACK_PREFIX


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
        media_storage: LocalMediaStorage | None = None,
        statistics_service: StatisticsService | None = None,
        quick_reply_service: QuickReplyService | None = None,
        quick_replies_topic_id: int | None = None,
    ) -> None:
        self.bot = bot
        self.ticket_service = ticket_service
        self.settings = settings
        self.panel_service = panel_service
        self.media_storage = media_storage or LocalMediaStorage(settings.data_dir)
        self.statistics_service = statistics_service or StatisticsService(ticket_service.database)
        self.quick_reply_service = quick_reply_service
        self.quick_replies_topic_id = quick_replies_topic_id
        self.initialize_quick_reply_runtime()
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
        self.router.edited_message.register(
            self.handle_edited_group_message,
            F.chat.id == self.settings.support_group_id,
        )
        self.router.callback_query.register(
            self.handle_quick_response_delete_callback,
            F.data.startswith(f"{QUICK_RESPONSE_DELETE_CALLBACK_PREFIX}:"),
        )
        self.router.callback_query.register(
            self.handle_statistics_callback,
            F.data.startswith(f"{STATISTICS_CALLBACK_PREFIX}:"),
        )
        self.router.callback_query.register(
            self.handle_rating_callback,
            F.data.startswith(f"{RATING_CALLBACK_PREFIX}:"),
        )
