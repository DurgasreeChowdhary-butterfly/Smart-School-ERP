"""Caller metadata (IP, user agent, request id) passed from the API layer to services."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from starlette.requests import Request

_MAX_USER_AGENT_LENGTH = 512


@dataclass(frozen=True, slots=True)
class RequestContext:
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def from_request(cls, request: Request) -> RequestContext:
        # request.client reflects X-Forwarded-For only when uvicorn trusts the proxy
        # (--proxy-headers / --forwarded-allow-ips), so it cannot be spoofed directly.
        host = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        request_id = getattr(request.state, "request_id", None)
        return cls(
            ip_address=_valid_ip(host),
            user_agent=_clean_user_agent(user_agent),
            request_id=request_id if isinstance(request_id, str) else None,
        )


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _clean_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    printable = "".join(ch for ch in value if ch.isprintable())
    return printable[:_MAX_USER_AGENT_LENGTH] or None
