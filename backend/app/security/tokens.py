"""JWT access tokens and opaque refresh tokens.

* Access tokens are short-lived signed JWTs carrying only identifiers
  (``sub`` user id, ``sid`` session id) - no personal data, no roles.
  Roles/permissions are loaded from the database per request so revocation
  and role changes take effect immediately.
* Refresh tokens are opaque high-entropy random strings. Only their SHA-256
  digest is stored; they are rotated on every use.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.config import Settings
from app.security.hashing import sha256_hex

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - claim value, not a secret
_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "sub", "sid", "jti", "typ", "iss", "aud"]
_CLOCK_SKEW_SECONDS = 10


class TokenValidationError(Exception):
    """Raised for any invalid, expired or wrong-type token (reason is for logs only)."""


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    token_id: str
    issued_at: datetime
    expires_at: datetime


def create_access_token(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "jti": uuid.uuid4().hex,
        "iat": int(issued_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires_at


def decode_access_token(settings: Settings, token: str) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # pin the algorithm: no "none"/alg confusion
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise TokenValidationError(type(exc).__name__) from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise TokenValidationError("wrong token type")
    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            session_id=uuid.UUID(str(payload["sid"])),
            token_id=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except (ValueError, TypeError) as exc:
        raise TokenValidationError("malformed claims") from exc


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return sha256_hex(token)
