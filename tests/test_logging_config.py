from __future__ import annotations

import ast
import json
import logging
import sys
from pathlib import Path

from sqlalchemy.exc import StatementError

from resolvate.logging_config import LOG_EXTRA_FIELDS, JsonFormatter, redact_text


def formatted(record: logging.LogRecord) -> dict[str, object]:
    return json.loads(JsonFormatter().format(record))


def test_redact_text_removes_common_secret_shapes() -> None:
    text = (
        "url=https://user:private-password@example.com/path?api_token=abc123 "
        "auth=Bearer secret-token"
    )

    redacted = redact_text(text)

    assert "private-password" not in redacted
    assert "abc123" not in redacted
    assert "secret-token" not in redacted
    assert "https://***@example.com/path?api_token=***" in redacted
    assert "Bearer ***" in redacted


def test_json_formatter_redacts_message_extra_and_exception() -> None:
    try:
        raise RuntimeError("postgresql://user:secret-db-password@db.example/support")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.getLogger("test").makeRecord(
        name="test",
        level=logging.ERROR,
        fn=__file__,
        lno=1,
        msg="request failed with Bearer raw-api-token",
        args=(),
        exc_info=exc_info,
        func="test",
        extra={
            "event": "redaction_test",
            "error_message": "webhook failed: https://receiver.example/cb?secret=raw-secret",
        },
    )

    payload = formatted(record)
    encoded = json.dumps(payload)

    assert "raw-api-token" not in encoded
    assert "raw-secret" not in encoded
    assert "secret-db-password" not in encoded
    assert payload["message"] == "request failed with Bearer ***"
    assert payload["error_message"] == "webhook failed: https://receiver.example/cb?secret=***"


def test_sqlalchemy_hidden_parameters_do_not_reach_exception_log() -> None:
    try:
        raise StatementError(
            "database write failed",
            "INSERT INTO notifications (subscription_url) VALUES (:value)",
            {"value": "https://sub.example/private-bearer-token"},
            RuntimeError("write failed"),
            hide_parameters=True,
        )
    except StatementError:
        exc_info = sys.exc_info()

    record = logging.getLogger("test").makeRecord(
        name="test",
        level=logging.ERROR,
        fn=__file__,
        lno=1,
        msg="database operation failed",
        args=(),
        exc_info=exc_info,
        func="test",
    )

    encoded = json.dumps(formatted(record))
    assert "private-bearer-token" not in encoded
    assert "SQL parameters hidden due to hide_parameters=True" in encoded


def test_all_emitted_structured_fields_are_in_vetted_allowlist() -> None:
    emitted_fields: set[str] = set()
    source_root = Path(__file__).parents[1] / "src" / "resolvate"
    for source in source_root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    emitted_fields.update(
                        key.value
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
            if isinstance(node.func, ast.Name) and node.func.id == "record_event":
                emitted_fields.update(
                    keyword.arg for keyword in node.keywords if keyword.arg is not None
                )

    assert emitted_fields <= set(LOG_EXTRA_FIELDS)


def test_json_formatter_preserves_important_correlation_fields() -> None:
    correlation = {
        "operator_action_id": "action-id",
        "notification_id": "notification-id",
        "reconciliation_id": "reconciliation-id",
        "telegram_update_id": 123,
        "event_type": "subscription.revoked",
        "status_code": 202,
    }
    record = logging.getLogger("test").makeRecord(
        name="test",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="correlated event",
        args=(),
        exc_info=None,
        func="test",
        extra=correlation,
    )

    payload = formatted(record)
    for field, value in correlation.items():
        assert payload[field] == value
