from __future__ import annotations

import logging
from typing import Any

from suppsystem.trace import get_trace_id

logger = logging.getLogger("suppsystem.audit")


def record_event(name: str, **fields: Any) -> None:
    """Write structured domain events. Callers must not include secrets or message bodies."""

    logger.info(name, extra={"event": name, "trace_id": get_trace_id(), **fields})
