"""OTP delivery channel abstraction.

No real SMS provider is integrated yet. :class:`DevelopmentConsoleOtpSender` is a
**DEVELOPMENT STUB**: it writes the code to the application log so developers can
sign in locally. Configuration validation forbids it in staging/production, and
the class refuses to construct there as a second safeguard.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.config import Environment, OtpDeliveryBackend, Settings
from app.security.identifiers import mask_phone

logger = logging.getLogger(__name__)


class OtpSender(Protocol):
    def send(self, *, phone: str, code: str, expires_in_seconds: int) -> None: ...


class DevelopmentConsoleOtpSender:
    """DEVELOPMENT STUB - logs OTP codes. Never enable outside development/test."""

    def __init__(self, environment: Environment) -> None:
        if environment not in (Environment.DEVELOPMENT, Environment.TEST):
            raise RuntimeError("DevelopmentConsoleOtpSender is not allowed in this environment")

    def send(self, *, phone: str, code: str, expires_in_seconds: int) -> None:
        logger.warning(
            "DEVELOPMENT OTP STUB - code for %s is %s (expires in %ss)",
            mask_phone(phone),
            code,
            expires_in_seconds,
        )


def build_otp_sender(backend: OtpDeliveryBackend, settings: Settings) -> OtpSender | None:
    """Return the configured sender, or None when OTP delivery is disabled."""
    if backend == "development_console":
        return DevelopmentConsoleOtpSender(settings.app_env)
    return None
