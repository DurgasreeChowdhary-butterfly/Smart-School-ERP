from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import Settings
from app.security.tokens import (
    TokenValidationError,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)

USER = uuid.uuid4()
SESSION = uuid.uuid4()


def _encode(settings: Settings, **overrides: object) -> str:
    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(USER),
        "sid": str(SESSION),
        "typ": "access",
        "jti": "abc",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), algorithm="HS256")


def test_roundtrip(test_settings: Settings) -> None:
    token, expires_at = create_access_token(test_settings, user_id=USER, session_id=SESSION)
    claims = decode_access_token(test_settings, token)
    assert claims.user_id == USER
    assert claims.session_id == SESSION
    assert claims.expires_at == expires_at.replace(microsecond=0)
    assert (expires_at - claims.issued_at) <= timedelta(
        minutes=test_settings.access_token_ttl_minutes, seconds=1
    )


def test_claims_contain_no_personal_data(test_settings: Settings) -> None:
    token, _ = create_access_token(test_settings, user_id=USER, session_id=SESSION)
    payload = jwt.decode(token, options={"verify_signature": False})
    assert set(payload) == {"iss", "aud", "sub", "sid", "typ", "jti", "iat", "nbf", "exp"}


def test_expired_token_rejected(test_settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    token, _ = create_access_token(test_settings, user_id=USER, session_id=SESSION, now=past)
    with pytest.raises(TokenValidationError, match="ExpiredSignature"):
        decode_access_token(test_settings, token)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "Bearer x.y.z"])
def test_malformed_token_rejected(test_settings: Settings, token: str) -> None:
    with pytest.raises(TokenValidationError):
        decode_access_token(test_settings, token)


def test_wrong_token_type_rejected(test_settings: Settings) -> None:
    with pytest.raises(TokenValidationError, match="wrong token type"):
        decode_access_token(test_settings, _encode(test_settings, typ="refresh"))


def test_wrong_signature_rejected(test_settings: Settings) -> None:
    other = test_settings.model_copy(update={"jwt_secret_key": test_settings.secret_key})
    token, _ = create_access_token(other, user_id=USER, session_id=SESSION)
    with pytest.raises(TokenValidationError, match="InvalidSignature"):
        decode_access_token(test_settings, token)


@pytest.mark.parametrize(
    "overrides",
    [{"aud": "someone-else"}, {"iss": "evil"}, {"sid": None}, {"jti": None}, {"sub": "not-a-uuid"}],
)
def test_wrong_or_missing_claims_rejected(
    test_settings: Settings, overrides: dict[str, object]
) -> None:
    with pytest.raises(TokenValidationError):
        decode_access_token(test_settings, _encode(test_settings, **overrides))


def test_alg_none_rejected(test_settings: Settings) -> None:
    def b64(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    valid = jwt.decode(_encode(test_settings), options={"verify_signature": False})
    unsigned = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(valid)}."
    with pytest.raises(TokenValidationError):
        decode_access_token(test_settings, unsigned)


def test_refresh_tokens_are_random_and_hashed() -> None:
    first, second = generate_refresh_token(), generate_refresh_token()
    assert first != second
    assert len(first) >= 64
    digest = hash_refresh_token(first)
    assert len(digest) == 64
    assert first not in digest
    assert digest == hash_refresh_token(first)
