from __future__ import annotations

import asyncio
import logging
import sys
from ipaddress import ip_address
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from pydantic import ValidationError

from suppsystem.api import create_app
from suppsystem.api_server import ApiServer
from suppsystem.config import Settings, get_settings
from suppsystem.database import Database
from suppsystem.delivery import DeliveryWorker
from suppsystem.durable_work import DurableWorkRepository
from suppsystem.heartbeat import Heartbeat
from suppsystem.logging_config import configure_logging
from suppsystem.metrics import MetricsRegistry
from suppsystem.migrations import upgrade_database
from suppsystem.notification_webhook import NotificationWebhookWorker
from suppsystem.panel import PanelService
from suppsystem.reconciliation import ReconciliationWorker
from suppsystem.remnawave import RemnawaveClient
from suppsystem.runtime_health import RuntimeHealth
from suppsystem.runtime_supervision import shutdown_runtime, supervise_ingress
from suppsystem.services import TicketService
from suppsystem.telegram_adapter import TelegramSupportAdapter
from suppsystem.telegram_ingress import DurableTelegramIngressMiddleware, TelegramIngressWorker
from suppsystem.telegram_lifecycle import create_polling_task
from suppsystem.telegram_limits import TelegramInboundRateLimiter, TelegramRateLimiter
from suppsystem.telegram_system_topics import TelegramSystemTopicService
from suppsystem.trace import TraceMiddleware

logger = logging.getLogger(__name__)


def _ensure_sqlite_directory(database_url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if database_url.startswith(prefix):
        Path(database_url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)


def _telegram_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _api_host_is_loopback(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_api_settings(settings: Settings) -> None:
    if not settings.api_enabled:
        return
    if settings.api_unsafe_disable_auth:
        if not _api_host_is_loopback(settings.api_host):
            raise RuntimeError(
                "API_UNSAFE_DISABLE_AUTH=true is allowed only when API_HOST is loopback"
            )
        logger.warning(
            "API authentication is disabled; bind is limited to a loopback host",
            extra={"event": "api_auth_disabled", "api_host": settings.api_host},
        )
        return
    if settings.api_admin_token is None or not settings.api_admin_token.get_secret_value():
        raise RuntimeError("API_ADMIN_TOKEN is required when API_ENABLED=true")


def validate_operator_access(settings: Settings) -> None:
    if settings.admin_telegram_ids or settings.api_enabled:
        return
    raise RuntimeError(
        "ADMIN_TELEGRAM_IDS must contain at least one administrator when API_ENABLED=false"
    )


async def validate_support_group(bot: Bot, support_group_id: int) -> None:
    try:
        chat = await bot.get_chat(support_group_id)
        bot_user = await bot.get_me()
        member = await bot.get_chat_member(support_group_id, bot_user.id)
    except TelegramAPIError as error:
        logger.exception(
            "Unable to inspect configured support group",
            extra={
                "event": "support_group_preflight_failed",
                "configured_chat_id": support_group_id,
            },
        )
        raise RuntimeError("Unable to inspect configured support group") from error

    chat_type = _telegram_value(chat.type)
    is_forum = getattr(chat, "is_forum", None)
    member_status = _telegram_value(getattr(member, "status", "unknown"))
    can_manage_topics = getattr(member, "can_manage_topics", None)
    errors: list[str] = []

    if chat_type != "supergroup":
        errors.append("SUPPORT_GROUP_ID must point to a Telegram supergroup")
    if is_forum is not True:
        errors.append("SUPPORT_GROUP_ID must point to a forum group with topics enabled")
    if member_status not in {"administrator", "creator"}:
        errors.append("Suppsystem bot must be an administrator in SUPPORT_GROUP_ID")
    if member_status == "administrator" and can_manage_topics is not True:
        errors.append("Suppsystem bot must have permission to manage topics")

    extra: dict[str, object] = {
        "event": "support_group_preflight",
        "configured_chat_id": support_group_id,
        "chat_id": chat.id,
        "chat_type": chat_type,
        "chat_title": getattr(chat, "title", None),
        "is_forum": is_forum,
        "bot_id": bot_user.id,
        "member_status": member_status,
        "can_manage_topics": can_manage_topics,
    }
    if errors:
        logger.error(
            "Configured support group failed preflight checks",
            extra={**extra, "preflight_errors": errors},
        )
        raise RuntimeError("Invalid Telegram support group configuration: " + "; ".join(errors))

    logger.info("Configured support group passed preflight checks", extra=extra)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    assert settings.database_url is not None
    assert settings.migration_database_url is not None
    _ensure_sqlite_directory(settings.database_url)
    _ensure_sqlite_directory(settings.migration_database_url)
    validate_api_settings(settings)
    validate_operator_access(settings)
    if settings.migrations_at_startup:
        await upgrade_database(settings.migration_database_url)

    database = Database(settings.database_url)
    runtime_health = RuntimeHealth()
    runtime_health.register("database")
    runtime_health.register("telegram_ingress", progress_timeout_seconds=45)
    runtime_health.register("reconciliation", progress_timeout_seconds=45)
    runtime_health.register("api", configured=settings.api_enabled or settings.web_api_enabled)
    runtime_health.register("panel", configured=settings.remnawave_enabled)
    runtime_health.register("delivery_worker", progress_timeout_seconds=45)
    runtime_health.register(
        "notification_webhook",
        configured=settings.notification_webhook_enabled,
        progress_timeout_seconds=45,
    )
    runtime_health.ready("database")
    metrics = MetricsRegistry()
    http_client = httpx.AsyncClient()
    ticket_service = TicketService(database)
    if settings.web_api_enabled:
        await ticket_service.validate_web_identity_mode(settings.web_identity_mode)
    durable_work = DurableWorkRepository(database)
    panel_service: PanelService | None = None
    if settings.remnawave_enabled:
        if settings.remnawave_base_url is None or settings.remnawave_api_token is None:
            raise RuntimeError("Remnawave is enabled but base URL or API token is missing")
        panel_service = PanelService(
            RemnawaveClient(
                base_url=settings.remnawave_base_url,
                api_token=settings.remnawave_api_token,
                timeout_seconds=settings.remnawave_timeout_seconds,
                client=http_client,
                metrics=metrics,
            ),
            database=database,
            reconcile_delay_seconds=settings.remnawave_reconcile_delay_seconds,
            support_group_id=settings.support_group_id,
            revoke_link_telegram_notification=(
                settings.remnawave_revoke_link_telegram_notification
            ),
        )
        recovered_actions = await panel_service.recover_interrupted_actions()
        runtime_health.ready("panel")
        if recovered_actions:
            logger.error(
                "Recovered interrupted Remnawave actions for durable reconciliation",
                extra={
                    "event": "panel_actions_recovery_required",
                    "recovered_action_count": recovered_actions,
                },
            )
    unresolved_topic_claims = await ticket_service.list_topic_provisioning_ticket_ids()
    for ticket_id in unresolved_topic_claims:
        logger.error(
            "Topic provisioning requires explicit recovery",
            extra={"event": "topic_provisioning_recovery_required", "ticket_id": ticket_id},
        )
    api_server: ApiServer | None = None
    bot = Bot(
        token=settings.support_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await validate_support_group(bot, settings.support_group_id)
    except Exception:
        await bot.session.close()
        await http_client.aclose()
        await database.dispose()
        raise
    limiter = TelegramRateLimiter(settings.telegram_min_request_interval_seconds)
    system_topics = TelegramSystemTopicService(
        bot=bot,
        database=database,
        support_group_id=settings.support_group_id,
        limiter=limiter,
    )
    dispatcher = Dispatcher()
    ingress_worker = TelegramIngressWorker(
        bot=bot, dispatcher=dispatcher, repository=durable_work, runtime_health=runtime_health
    )
    dispatcher.update.outer_middleware(TraceMiddleware())
    dispatcher.update.outer_middleware(
        DurableTelegramIngressMiddleware(
            durable_work,
            ingress_worker.wake,
            bot=bot,
            inbound_limiter=TelegramInboundRateLimiter(
                per_minute=settings.telegram_inbound_rate_limit_per_minute,
                per_hour=settings.telegram_inbound_rate_limit_per_hour,
            ),
            outbound_limiter=limiter,
        )
    )
    adapter = TelegramSupportAdapter(
        bot=bot,
        ticket_service=ticket_service,
        settings=settings,
        limiter=limiter,
        panel_service=panel_service,
    )
    dispatcher.include_router(adapter.router)
    await adapter.recover_waiting_topics_after_restart()
    await adapter.ensure_statistics_dashboard()

    if settings.api_enabled or settings.web_api_enabled:
        api_server = ApiServer(
            create_app(
                database=database,
                ticket_service=ticket_service,
                settings=settings,
                runtime_health=runtime_health,
                metrics=metrics,
            ),
            settings,
            runtime_health,
        )

    reconciliation_worker = ReconciliationWorker(
        repository=durable_work,
        reconcile_topic=adapter.reconcile_ticket_topic,
        panel_service=panel_service,
        runtime_health=runtime_health,
    )
    delivery_worker = DeliveryWorker(
        bot=bot,
        ticket_service=ticket_service,
        outbox=ticket_service.outbox,
        settings=settings,
        limiter=limiter,
        heartbeat_path=settings.data_dir / "delivery-worker-heartbeat",
        recover_missing_topic=adapter.recover_missing_topic,
        resolve_system_topic=system_topics.ensure,
        recover_system_topic=system_topics.recover,
        prepare_reopened_customer_topic=adapter.prepare_reopened_customer_topic,
        runtime_health=runtime_health,
    )
    notification_worker: NotificationWebhookWorker | None = None
    notification_worker_task: asyncio.Task[None] | None = None
    if settings.notification_webhook_enabled:
        notification_worker = NotificationWebhookWorker(
            outbox=ticket_service.outbox,
            settings=settings,
            heartbeat_path=settings.data_dir / "notification-webhook-worker-heartbeat",
            runtime_health=runtime_health,
            client=http_client,
            metrics=metrics,
        )
        notification_worker_task = asyncio.create_task(
            notification_worker.run(), name="notification-webhook-worker"
        )
    heartbeat = Heartbeat(settings.data_dir / "heartbeat", progress_probe=runtime_health.is_ready)
    ingress_worker_task = asyncio.create_task(ingress_worker.run(), name="telegram-ingress-worker")
    reconciliation_worker_task = asyncio.create_task(
        reconciliation_worker.run(), name="reconciliation-worker"
    )
    worker_task = asyncio.create_task(delivery_worker.run(), name="delivery-worker")
    heartbeat_task = asyncio.create_task(heartbeat.run(), name="heartbeat")
    api_task = api_server.start() if api_server is not None else None
    polling_task = create_polling_task(
        dispatcher,
        bot,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )

    try:
        logger.info("Starting Suppsystem")
        await supervise_ingress(polling_task, api_task, dispatcher.stop_polling)
    finally:
        await shutdown_runtime(
            polling_task=polling_task,
            stop_polling=dispatcher.stop_polling,
            api_task=api_task,
            request_api_stop=(api_server.request_stop if api_server is not None else None),
            worker_tasks=(
                ingress_worker_task,
                reconciliation_worker_task,
                worker_task,
                heartbeat_task,
                *((notification_worker_task,) if notification_worker_task is not None else ()),
            ),
            stop_workers=(
                ingress_worker.stop,
                reconciliation_worker.stop,
                delivery_worker.stop,
                heartbeat.stop,
                *((notification_worker.stop,) if notification_worker is not None else ()),
            ),
            close_resources=(bot.session.close, http_client.aclose, database.dispose),
        )


def _settings_location(field_name: object) -> str:
    return str(field_name).upper()


def format_configuration_error(error: ValidationError) -> str:
    lines = ["Configuration error:"]
    for item in error.errors(include_input=False, include_url=False):
        message = str(item.get("msg", "invalid configuration"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        location = item.get("loc", ())
        if isinstance(location, tuple) and location:
            message = f"{_settings_location(location[-1])}: {message}"
        lines.append(f"- {message}")
    lines.append("Fix the environment file and restart the service.")
    lines.append("Production env file: /opt/suppsystem/.env")
    return "\n".join(lines)


def main() -> None:
    try:
        asyncio.run(run())
    except ValidationError as error:
        print(format_configuration_error(error), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
