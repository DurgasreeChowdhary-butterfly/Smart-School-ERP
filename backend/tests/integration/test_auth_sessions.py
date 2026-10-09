"""Access tokens via the API, /me, refresh-token rotation, reuse detection and logout."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.audit import AuditLog
from app.models.auth import AuthSession, RefreshToken
from app.models.user import UserStatus
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.tokens import create_access_token, hash_refresh_token
from tests.conftest import UserFactory, bearer, login

pytestmark = pytest.mark.db

ME = "/api/v1/auth/me"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"


@pytest.fixture
def session_tokens(db_client: TestClient, make_user: UserFactory) -> dict[str, Any]:
    make_user(roles=(RoleName.FACULTY,), email="teacher@school.test")
    return login(db_client, "teacher@school.test")


def _refresh(client: TestClient, token: str) -> Any:
    return client.post(REFRESH, json={"refresh_token": token})


# ------------------------------------------------------------------------- access
def test_me_returns_profile_roles_and_permissions(
    db_client: TestClient, session_tokens: dict[str, Any]
) -> None:
    response = db_client.get(ME, headers=bearer(session_tokens["access_token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "teacher@school.test"
    assert body["roles"] == ["FACULTY"]
    assert set(body["permissions"]) == {p.value for p in ROLE_PERMISSIONS[RoleName.FACULTY]}
    assert "password_hash" not in body
    assert body["last_login_at"] is not None


def test_missing_token_is_401(db_client: TestClient) -> None:
    response = db_client.get(ME)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer a.b.c", "Basic dXNlcjpwYXNz", "Bearer"],
)
def test_malformed_authorization_is_401(db_client: TestClient, header: str) -> None:
    response = db_client.get(ME, headers={"Authorization": header})
    assert response.status_code == 401


def test_expired_access_token_is_401(
    db_client: TestClient,
    session_tokens: dict[str, Any],
    test_settings: Settings,
    db_session: Session,
) -> None:
    auth_session = db_session.scalars(select(AuthSession)).one()
    expired, _ = create_access_token(
        test_settings,
        user_id=auth_session.user_id,
        session_id=auth_session.id,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    response = db_client.get(ME, headers=bearer(expired))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_refresh_token_cannot_be_used_as_access_token(
    db_client: TestClient, session_tokens: dict[str, Any]
) -> None:
    response = db_client.get(ME, headers=bearer(session_tokens["refresh_token"]))
    assert response.status_code == 401


def test_token_for_unknown_session_is_rejected(
    db_client: TestClient,
    session_tokens: dict[str, Any],
    test_settings: Settings,
    db_session: Session,
) -> None:
    auth_session = db_session.scalars(select(AuthSession)).one()
    forged, _ = create_access_token(
        test_settings, user_id=auth_session.user_id, session_id=uuid.uuid4()
    )
    assert db_client.get(ME, headers=bearer(forged)).status_code == 401


def test_token_with_mismatched_user_and_session_is_rejected(
    db_client: TestClient,
    session_tokens: dict[str, Any],
    test_settings: Settings,
    db_session: Session,
    make_user: UserFactory,
) -> None:
    other = make_user(email="other@school.test")
    auth_session = db_session.scalars(select(AuthSession)).one()
    forged, _ = create_access_token(test_settings, user_id=other.id, session_id=auth_session.id)
    assert db_client.get(ME, headers=bearer(forged)).status_code == 401


def test_deactivation_takes_effect_immediately(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    db_session.execute(text("UPDATE users SET status = :s"), {"s": UserStatus.DEACTIVATED.value})
    assert db_client.get(ME, headers=bearer(session_tokens["access_token"])).status_code == 401
    assert _refresh(db_client, session_tokens["refresh_token"]).status_code == 401
    auth_session = db_session.scalars(select(AuthSession)).one()
    assert auth_session.revoked_reason == "user_not_eligible"


# ------------------------------------------------------------------------- refresh
def test_refresh_rotates_tokens(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    response = _refresh(db_client, session_tokens["refresh_token"])
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    rotated = response.json()
    assert rotated["refresh_token"] != session_tokens["refresh_token"]
    assert rotated["access_token"] != session_tokens["access_token"]
    assert db_client.get(ME, headers=bearer(rotated["access_token"])).status_code == 200

    old = db_session.scalars(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(session_tokens["refresh_token"])
        )
    ).one()
    new = db_session.scalars(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(rotated["refresh_token"])
        )
    ).one()
    assert old.revoked_at is not None
    assert old.replaced_by_id == new.id
    assert new.session_id == old.session_id
    assert new.revoked_at is None


def test_raw_refresh_tokens_are_never_stored(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    rotated = _refresh(db_client, session_tokens["refresh_token"]).json()
    dump = str(db_session.execute(text("SELECT * FROM refresh_tokens")).all())
    dump += str(db_session.execute(text("SELECT * FROM auth_sessions")).all())
    dump += str(db_session.execute(text("SELECT * FROM audit_logs")).all())
    for secret in (session_tokens["refresh_token"], rotated["refresh_token"]):
        assert secret not in dump
    for secret in (session_tokens["access_token"], rotated["access_token"]):
        assert secret not in dump


def test_old_refresh_token_invalid_after_rotation_and_reuse_kills_family(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    original = session_tokens["refresh_token"]
    rotated = _refresh(db_client, original).json()

    # Attacker (or a confused client) replays the already-rotated token.
    replay = _refresh(db_client, original)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "invalid_token"

    # The legitimate, newest token is now dead too: the whole family was revoked.
    assert _refresh(db_client, rotated["refresh_token"]).status_code == 401
    assert db_client.get(ME, headers=bearer(rotated["access_token"])).status_code == 401

    auth_session = db_session.scalars(select(AuthSession)).one()
    assert auth_session.revoked_reason == "refresh_token_reuse"
    tokens = db_session.scalars(select(RefreshToken)).all()
    assert all(t.revoked_at is not None for t in tokens)
    reused = next(t for t in tokens if t.token_hash == hash_refresh_token(original))
    assert reused.reuse_detected_at is not None

    events = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.refresh.reuse_detected")
    ).all()
    assert len(events) >= 1
    assert events[0].user_id == auth_session.user_id
    assert events[0].resource_id == str(auth_session.id)
    assert events[0].outcome == "failure"
    # Forensic details survive the sanitiser.
    assert events[0].details == {"refresh_record_id": str(reused.id), "was_rotated": True}


def test_reuse_does_not_affect_other_sessions(
    db_client: TestClient, session_tokens: dict[str, Any]
) -> None:
    second_device = login(db_client, "teacher@school.test")
    _refresh(db_client, session_tokens["refresh_token"])
    _refresh(db_client, session_tokens["refresh_token"])  # reuse on device 1
    assert _refresh(db_client, second_device["refresh_token"]).status_code == 200


def test_expired_refresh_token_fails(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    db_session.execute(
        text("UPDATE refresh_tokens SET expires_at = :t"),
        {"t": datetime.now(UTC) - timedelta(seconds=1)},
    )
    response = _refresh(db_client, session_tokens["refresh_token"])
    assert response.status_code == 401
    failure = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.refresh.failed")
    ).one()
    assert failure.details["reason"] == "expired"


def test_session_absolute_lifetime_is_enforced(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    db_session.execute(
        text("UPDATE auth_sessions SET expires_at = :t"),
        {"t": datetime.now(UTC) - timedelta(seconds=1)},
    )
    assert _refresh(db_client, session_tokens["refresh_token"]).status_code == 401
    assert db_client.get(ME, headers=bearer(session_tokens["access_token"])).status_code == 401


def test_revoked_refresh_token_fails(db_client: TestClient, session_tokens: dict[str, Any]) -> None:
    assert (
        db_client.post(LOGOUT, json={"refresh_token": session_tokens["refresh_token"]}).status_code
        == 204
    )
    assert _refresh(db_client, session_tokens["refresh_token"]).status_code == 401


def test_unknown_refresh_token_fails(db_client: TestClient) -> None:
    response = _refresh(db_client, "x" * 64)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


@pytest.mark.parametrize("token", ["", "short", "x" * 300])
def test_refresh_validation(db_client: TestClient, token: str) -> None:
    assert _refresh(db_client, token).status_code == 422


# -------------------------------------------------------------------------- logout
def test_logout_revokes_session_and_access_token(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    response = db_client.post(LOGOUT, json={"refresh_token": session_tokens["refresh_token"]})
    assert response.status_code == 204
    assert response.content == b""
    # Access token stops working immediately (session-bound), not only at expiry.
    assert db_client.get(ME, headers=bearer(session_tokens["access_token"])).status_code == 401

    auth_session = db_session.scalars(select(AuthSession)).one()
    assert auth_session.revoked_reason == "logout"
    logout_event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.logout")
    ).one()
    assert logout_event.user_id == auth_session.user_id


def test_logout_is_idempotent_and_does_not_leak(
    db_client: TestClient, session_tokens: dict[str, Any]
) -> None:
    body = {"refresh_token": session_tokens["refresh_token"]}
    assert db_client.post(LOGOUT, json=body).status_code == 204
    assert db_client.post(LOGOUT, json=body).status_code == 204
    assert db_client.post(LOGOUT, json={"refresh_token": "y" * 64}).status_code == 204


def test_successful_flow_is_audited(
    db_client: TestClient, session_tokens: dict[str, Any], db_session: Session
) -> None:
    rotated = _refresh(db_client, session_tokens["refresh_token"]).json()
    db_client.post(LOGOUT, json={"refresh_token": rotated["refresh_token"]})
    actions = db_session.scalars(select(AuditLog.action).order_by(AuditLog.created_at)).all()
    assert actions[-3:] == ["auth.login.succeeded", "auth.refresh.succeeded", "auth.logout"]
