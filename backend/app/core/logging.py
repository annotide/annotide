"""Structured logging setup (OPS-3): ``APP_LOG_LEVEL`` and ``APP_LOG_FORMAT``."""

from __future__ import annotations

import logging
import re
import sys
from typing import cast

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, Processor

from app.core import observability
from app.core.config import get_settings

_SENSITIVE_KEY = re.compile(
    r"authorization|token|secret|password|api[_-]?key|cookie", re.IGNORECASE
)
REDACTED = "[redacted]"


def redact_sensitive(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: mask values whose key names a credential.

    A key matches when it contains `authorization`, `token`, `secret`,
    `password`, `api_key` / `api-key` / `apikey` or `cookie` (any case). Dict
    values are masked one level down as well, e.g. a logged `headers` mapping.
    It fails closed, so a count is masked too: log `prompt_size`, not
    `token_count`.
    """
    for key, value in list(event_dict.items()):
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, dict):
            event_dict[key] = {
                k: REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else v
                for k, v in value.items()
            }
    return event_dict


def configure_logging() -> None:
    """Configure structlog (and stdlib logging) from the current settings."""
    settings = get_settings()
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        observability.add_trace_ids,
        redact_sensitive,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")


def get_logger(name: str) -> FilteringBoundLogger:
    """Return a structlog logger bound to ``name``."""
    return cast(FilteringBoundLogger, structlog.get_logger(name))
