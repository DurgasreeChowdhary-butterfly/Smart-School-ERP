"""Keyed and unkeyed digests for secrets that must be looked up but never stored raw."""

from __future__ import annotations

import hashlib
import hmac

from pydantic import SecretStr

_SEPARATOR = "\x1f"  # unit separator: unambiguous join for domain-separated inputs


def keyed_digest(key: SecretStr, *parts: str) -> str:
    """HMAC-SHA256 over domain-separated parts (hex). Resists offline guessing without the key."""
    message = _SEPARATOR.join(parts).encode("utf-8")
    return hmac.new(key.get_secret_value().encode("utf-8"), message, hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    """Plain SHA-256; only appropriate for high-entropy inputs such as refresh tokens."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def identifier_fingerprint(key: SecretStr, kind: str, normalized_value: str) -> str:
    """Stable pseudonymous key for a login identifier (used for throttling and OTP lookup)."""
    return keyed_digest(key, "identifier", kind, normalized_value)
