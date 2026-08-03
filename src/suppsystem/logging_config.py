from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, cast

SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)([^/@\s]+)@", re.IGNORECASE), r"\1***@"),
    (re.compile(r"\b(Bearer)\s+[^\s,;]+", re.IGNORECASE), r"\1 ***"),
    (
        re.compile(r"([?&][^=]*(?:token|secret|password|key)[^=]*=)[^&#\s]+", re.IGNORECASE),
        r"\1***",
    ),
)


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


LOG_EXTRA_FIELDS = (
    "trace_id",
    "event",
    "ticket_id",
    "operator_telegram_id",
    "telegram_user_id",
    "chat_id",
    "target_chat_id",
    "source_chat_id",
    "chat_type",
    "chat_title",
    "configured_chat_id",
    "is_forum",
    "message_id",
    "source_message_id",
    "topic_id",
    "command",
    "delivery_id",
    "delivery_kind",
    "delivery_status",
    "attempt_count",
    "max_attempts",
    "retry_after_seconds",
    "stale_delivery_count",
    "claimed_delivery_count",
    "bot_id",
    "member_status",
    "can_manage_topics",
    "preflight_errors",
    "rating",
    "error_kind",
    "error_message",
    "http_method",
    "http_path",
    "http_status",
    "duration_ms",
    "identity_provider",
)


def _current_task_name() -> str | None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return task.get_name() if task is not None else None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "source": {
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            },
            "process": {
                "id": record.process,
                "name": record.processName,
            },
        }
        task_name = _current_task_name()
        if task_name is not None:
            payload["task"] = task_name

        for field in LOG_EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = redact_value(value)
        if record.exc_info:
            exception_type, exception, _traceback = record.exc_info
            exception_type = cast(type[BaseException], exception_type)
            payload["exception_type"] = exception_type.__name__
            payload["exception_message"] = redact_text(str(exception))
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
