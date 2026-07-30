from __future__ import annotations


def is_missing_topic_error(error: object) -> bool:
    """Return whether Telegram has confirmed that a forum topic no longer exists."""

    error_text = str(error).casefold()
    markers = (
        "topic_id_invalid",
        "message thread not found",
        "message thread is not found",
    )
    return any(marker in error_text for marker in markers)
