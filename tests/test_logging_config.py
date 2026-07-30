from __future__ import annotations

import json
import logging
import sys

from suppsystem.logging_config import JsonFormatter, redact_text


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
