"""Structured logging built on the standard library.

JSON output (``LOG_JSON=true``) is intended for production log aggregation;
the plain console format is friendlier for local development. Every record is
enriched with the current request id when one is available.
"""

from __future__ import annotations

import json
import logging
import logging.config
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes present on every LogRecord; anything else was passed via ``extra``.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "request_id", "taskName"}
)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable format for development; appends ``extra`` fields as key=value."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{key}={value}" for key, value in _extra_fields(record).items())
        if not extras:
            return line
        # Keep any traceback (added by the base formatter) after the extras.
        head, sep, tail = line.partition("\n")
        return f"{head} {extras}{sep}{tail}"


def configure_logging(settings: Settings) -> None:
    formatter = "json" if settings.log_json else "console"
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"request_context": {"()": RequestContextFilter}},
            "formatters": {
                "json": {"()": JsonFormatter},
                "console": {"()": ConsoleFormatter},
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter,
                    "filters": ["request_context"],
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["default"], "level": settings.log_level},
            "loggers": {
                # Route uvicorn through our handler; our middleware writes access logs.
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": False},
                "sqlalchemy.engine": {
                    "level": "INFO" if settings.db_echo else "WARNING",
                    "handlers": [],
                    "propagate": True,
                },
            },
        }
    )
