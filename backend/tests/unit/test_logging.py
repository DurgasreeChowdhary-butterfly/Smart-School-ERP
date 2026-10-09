from __future__ import annotations

import json
import logging
import sys

from app.core.logging import (
    ConsoleFormatter,
    JsonFormatter,
    RequestContextFilter,
    request_id_ctx,
)


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    RequestContextFilter().filter(record)
    return record


def test_json_formatter_emits_structured_record_with_request_id() -> None:
    token = request_id_ctx.set("req-123")
    try:
        output = JsonFormatter().format(_record(status_code=200, duration_ms=1.5))
    finally:
        request_id_ctx.reset(token)

    payload = json.loads(output)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["request_id"] == "req-123"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 1.5
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_request_id_is_none_outside_request() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["request_id"] is None


def test_console_formatter_appends_extra_fields() -> None:
    output = ConsoleFormatter().format(_record("request completed", method="GET", status_code=200))
    assert output.endswith("request completed method=GET status_code=200")
    assert "[None]" in output
