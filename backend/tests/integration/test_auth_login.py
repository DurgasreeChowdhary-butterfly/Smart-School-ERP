from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.auth import LoginAttempt
from app.models.user import UserStatus
from app.security.passwords import Argon2Params, hash_password, verify_password
from app.security.permissions import RoleName
from tests.conftest import DEFAULT_PASSWORD, UserFactory, login

pytestmark = pytest.mark.db

LOGIN = "/api/v1/auth/login"


def _post(client: TestClient, identifier: str, password: str) -> Any:
    return client.post(LOGIN, json={"identifier": identifier, "password": password})


def test_admin_email_login_succeeds(db_client: TestClient, make_user: UserFactory) -> None:
    user = make_user(roles=(RoleName.SCHOOL_ADMIN,), email="admin@school.test")

    response = db_client.post(
        LOGIN, json={"identifier": "  Admin@School.TEST ", "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert body["refresh_expires_in"] > body["expires_in"]
    assert user.last_login_at is not None


def test_faculty_can_log_in_with_mobile_number(
    db_client: TestClient, make_user: UserFactory
) -> None:
    make_user(roles=(RoleName.FACULTY,), phone="+919876543210")
    body = login(db_client, "+91 98765 43210")
    assert body["access_token"]


def test_super_admin_login(db_client: TestClient, make_user: UserFactory) -> None:
    make_user(roles=(RoleName.SUPER_ADMIN,), email="root@platform.test")
    assert login(db_client, "root@platform.test")["access_token"]


def _assert_generic_failure(response: Any) -> None:
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["message"] == "Invalid credentials."
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_password_fails(db_client: TestClient, make_user: UserFactory) -> None:
    make_user(email="t@school.test")
    _assert_generic_failure(_post(db_client, "t@school.test", "Wrong-Password-1"))


@pytest.mark.parametrize("status", [UserStatus.SUSPENDED, UserStatus.DEACTIVATED])
def test_inactive_user_cannot_log_in(
    db_client: TestClient, make_user: UserFactory, status: UserStatus
) -> None:
    make_user(email="gone@school.test", status=status)
    _assert_generic_failure(_post(db_client, "gone@school.test", DEFAULT_PASSWORD))


def test_otp_only_account_cannot_use_password_login(
    db_client: TestClient, make_user: UserFactory
) -> None:
    make_user(roles=(RoleName.PARENT,), phone="+919811111111", password=None)
    _assert_generic_failure(_post(db_client, "+919811111111", "anything-at-all"))


def test_user_without_roles_cannot_log_in(
    db_client: TestClient, make_user: UserFactory, db_session: Session
) -> None:
    user = make_user(email="norole@school.test")
    db_session.execute(text("DELETE FROM user_roles WHERE user_id = :id"), {"id": user.id})
    _assert_generic_failure(_post(db_client, "norole@school.test", DEFAULT_PASSWORD))


def test_unknown_account_is_indistinguishable_from_wrong_password(
    db_client: TestClient, make_user: UserFactory
) -> None:
    make_user(email="exists@school.test")
    known = _post(db_client, "exists@school.test", "Wrong-Password-1")
    unknown = _post(db_client, "nobody@school.test", "Wrong-Password-1")
    malformed = _post(db_client, "not-an-email-or-phone", "Wrong-Password-1")
    for response in (known, unknown, malformed):
        _assert_generic_failure(response)
    assert known.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_failure_reasons_are_audited_without_secrets(
    db_client: TestClient, make_user: UserFactory, db_session: Session
) -> None:
    user = make_user(email="audit@school.test")
    _post(db_client, "audit@school.test", "Wrong-Password-1")
    _post(db_client, "ghost@school.test", "Wrong-Password-2")

    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login.failed").order_by(AuditLog.created_at)
    ).all()
    assert [r.details["reason"] for r in rows] == ["invalid_password", "unknown_identifier"]
    assert rows[0].user_id == user.id
    assert rows[1].user_id is None
    assert str(rows[0].ip_address) == "203.0.113.10"
    dumped = str([(r.details, r.user_agent) for r in rows])
    assert "Wrong-Password" not in dumped
    assert "ghost@school.test" not in dumped


@pytest.mark.parametrize(
    "payload",
    [
        {"identifier": "a@b.c"},
        {"password": "x"},
        {"identifier": "", "password": "x"},
        {"identifier": "a@b.c", "password": ""},
        {"identifier": "a@b.c", "password": "x" * 1025},
        {"identifier": "a@b.c", "password": "x", "extra": 1},
    ],
)
def test_login_validation(db_client: TestClient, payload: dict[str, object]) -> None:
    response = db_client.post(LOGIN, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_password_is_rehashed_when_parameters_change(
    db_client: TestClient, make_user: UserFactory, db_session: Session
) -> None:
    user = make_user(email="rehash@school.test")
    old = hash_password(
        DEFAULT_PASSWORD, Argon2Params(time_cost=2, memory_cost_kib=2048, parallelism=1)
    )
    user.password_hash = old
    db_session.flush()

    login(db_client, "rehash@school.test")

    assert user.password_hash != old
    assert user.password_hash is not None
    assert verify_password(DEFAULT_PASSWORD, user.password_hash)


class TestBruteForceThrottling:
    def test_identifier_is_locked_after_max_failures(
        self, db_client: TestClient, make_user: UserFactory
    ) -> None:
        make_user(email="victim@school.test")
        for _ in range(5):
            assert _post(db_client, "victim@school.test", "Wrong-Password-1").status_code == 401

        response = _post(db_client, "victim@school.test", DEFAULT_PASSWORD)  # even correct
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
        assert int(response.headers["Retry-After"]) > 0

    def test_unknown_identifiers_are_throttled_identically(self, db_client: TestClient) -> None:
        for _ in range(5):
            _post(db_client, "nobody@school.test", "Wrong-Password-1")
        assert _post(db_client, "nobody@school.test", "x").status_code == 429

    def test_success_resets_the_counter(
        self, db_client: TestClient, make_user: UserFactory
    ) -> None:
        make_user(email="typo@school.test")
        for _ in range(4):
            _post(db_client, "typo@school.test", "Wrong-Password-1")
        login(db_client, "typo@school.test")
        for _ in range(4):
            assert _post(db_client, "typo@school.test", "Wrong-Password-1").status_code == 401
        login(db_client, "typo@school.test")

    def test_old_failures_outside_window_do_not_count(
        self, db_client: TestClient, make_user: UserFactory, db_session: Session
    ) -> None:
        make_user(email="old@school.test")
        for _ in range(5):
            _post(db_client, "old@school.test", "Wrong-Password-1")
        db_session.execute(
            text("UPDATE login_attempts SET created_at = :t"),
            {"t": datetime(2020, 1, 1, tzinfo=UTC)},
        )
        login(db_client, "old@school.test")

    def test_ip_wide_limit(self, db_client: TestClient, db_session: Session) -> None:
        now = datetime.now(UTC)
        db_session.add_all(
            LoginAttempt(
                identifier_hash=f"{i:064d}",
                ip_address="203.0.113.10",
                succeeded=False,
                created_at=now,
            )
            for i in range(50)
        )
        db_session.flush()
        response = _post(db_client, "fresh@school.test", "whatever-123")
        assert response.status_code == 429

    def test_throttling_is_audited(self, db_client: TestClient, db_session: Session) -> None:
        for _ in range(6):
            _post(db_client, "spray@school.test", "Wrong-Password-1")
        throttled = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "auth.login.throttled")
        ).all()
        assert len(throttled) == 1
        assert throttled[0].details["scope"] == "identifier"
