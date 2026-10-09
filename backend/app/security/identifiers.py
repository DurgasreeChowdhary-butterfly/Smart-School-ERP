"""Normalisation of login identifiers (email / mobile number)."""

from __future__ import annotations

import re
from enum import StrEnum

# E.164: "+", country code (no leading 0), up to 15 digits in total.
E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
_PHONE_SEPARATORS = re.compile(r"[\s\-().]")


class IdentifierKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    STUDENT = "student"  # school code + admission number
    UNKNOWN = "unknown"


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_phone(value: str) -> str | None:
    """Return the E.164 form, or None if the value is not an international number."""
    candidate = _PHONE_SEPARATORS.sub("", value.strip())
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:]
    return candidate if E164_PATTERN.fullmatch(candidate) else None


def parse_login_identifier(raw: str) -> tuple[IdentifierKind, str]:
    """Classify a login identifier. Unrecognised values are UNKNOWN (never an error,
    so malformed input is indistinguishable from a non-existent account)."""
    value = raw.strip()
    if "@" in value:
        return IdentifierKind.EMAIL, normalize_email(value)
    phone = normalize_phone(value)
    if phone is not None:
        return IdentifierKind.PHONE, phone
    return IdentifierKind.UNKNOWN, value.lower()


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}{'*' * max(0, len(phone) - 5)}{phone[-2:]}"
