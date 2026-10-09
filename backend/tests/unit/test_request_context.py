from __future__ import annotations

from starlette.requests import Request

from app.core.request_context import RequestContext


def _request(client: tuple[str, int] | None, user_agent: bytes | None = None) -> Request:
    headers = [(b"user-agent", user_agent)] if user_agent is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": client,
        "state": {"request_id": "req-1"},
    }
    return Request(scope)


def test_valid_ipv4_and_ipv6_are_kept() -> None:
    assert RequestContext.from_request(_request(("198.51.100.7", 1))).ip_address == "198.51.100.7"
    assert RequestContext.from_request(_request(("2001:db8::1", 1))).ip_address == "2001:db8::1"


def test_invalid_or_missing_client_ip_is_dropped() -> None:
    assert RequestContext.from_request(_request(("testclient", 1))).ip_address is None
    assert RequestContext.from_request(_request(None)).ip_address is None


def test_user_agent_is_sanitised_and_truncated() -> None:
    ctx = RequestContext.from_request(_request(("198.51.100.7", 1), b"Agent\x01/1.0" + b"x" * 600))
    assert ctx.user_agent is not None
    assert "\x01" not in ctx.user_agent
    assert len(ctx.user_agent) == 512
    assert ctx.request_id == "req-1"


def test_empty_user_agent_is_none() -> None:
    assert RequestContext.from_request(_request(("198.51.100.7", 1), b"")).user_agent is None
    assert RequestContext.from_request(_request(("198.51.100.7", 1))).user_agent is None
