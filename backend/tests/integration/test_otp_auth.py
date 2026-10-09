"""Mobile OTP sign-in (parent use case)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.auth import AuthSession, OtpChallenge
from app.models.user import UserStatus
from app.security.permissions import RoleName
from tests.conftest import CapturingOtpSender, UserFactory, bearer

pytestmark = pytest.mark.db

REQUEST = "/api/v1/auth/otp/request"
VERIFY = "/api/v1/auth/otp/verify"
PHONE = "+919800000001"


@pytest.fixture
def parent(make_user: UserFactory) -> Any:
    return make_user(roles=(RoleName.PARENT,), phone=PHONE, password=None)


def _request(client: TestClient, phone: str = PHONE) -> Any:
    return client.post(REQUEST, json={"phone": phone})


def _verify(client: TestClient, code: str, phone: str = PHONE) -> Any:
    return client.post(VERIFY, json={"phone": phone, "code": code})


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10 ** len(code):0{len(code)}d}"


def _skip_cooldown(db_session: Session) -> None:
    db_session.execute(
        text("UPDATE otp_challenges SET created_at = created_at - interval '2 minutes'")
    )


def test_parent_signs_in_with_otp(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    response = _request(db_client, "+91 98000 00001")  # normalised to E.164
    assert response.status_code == 202
    assert response.json() == {
        "message": "If the number is registered, a code has been sent.",
        "expires_in": 300,
        "resend_after": 60,
    }
    code = otp_outbox.last_code(PHONE)
    assert len(code) == 6

    tokens = _verify(db_client, code)
    assert tokens.status_code == 200
    me = db_client.get("/api/v1/auth/me", headers=bearer(tokens.json()["access_token"]))
    assert me.json()["roles"] == ["PARENT"]
    assert db_session.scalars(select(AuthSession)).one().auth_method == "otp"


def test_otp_is_stored_only_as_keyed_hash(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    code = otp_outbox.last_code(PHONE)
    challenge = db_session.scalars(select(OtpChallenge)).one()
    assert challenge.code_hash != code
    assert len(challenge.code_hash) == 64
    row = str(db_session.execute(text("SELECT * FROM otp_challenges")).all())
    assert code not in row
    assert PHONE not in row  # the destination is stored only as an HMAC
    assert code not in str(db_session.execute(text("SELECT * FROM audit_logs")).all())


def test_unknown_number_gets_identical_response_and_no_message(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    known = _request(db_client)
    unknown = _request(db_client, "+919800000099")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert [phone for phone, _ in otp_outbox.sent] == [PHONE]
    stray = db_session.scalars(select(OtpChallenge).where(OtpChallenge.user_id.is_(None))).one()
    assert stray.user_id is None  # recorded for rate limiting, can never verify


def test_inactive_account_receives_no_code(
    db_client: TestClient, otp_outbox: CapturingOtpSender, make_user: UserFactory
) -> None:
    make_user(roles=(RoleName.PARENT,), phone=PHONE, password=None, status=UserStatus.SUSPENDED)
    assert _request(db_client).status_code == 202
    assert otp_outbox.sent == []


def test_incorrect_otp_fails(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    response = _verify(db_client, _wrong(otp_outbox.last_code(PHONE)))
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "invalid_credentials",
        "message": "The code is invalid or has expired.",
        "details": [],
        "request_id": response.headers["X-Request-ID"],
    }
    assert db_session.scalars(select(OtpChallenge)).one().attempts == 1


def test_expired_otp_fails(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    db_session.execute(
        text("UPDATE otp_challenges SET expires_at = :t"),
        {"t": datetime.now(UTC) - timedelta(seconds=1)},
    )
    assert _verify(db_client, otp_outbox.last_code(PHONE)).status_code == 401
    assert db_session.scalars(select(OtpChallenge)).one().invalidated_at is not None


def test_max_attempts_enforced_even_for_correct_code(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    code = otp_outbox.last_code(PHONE)
    for _ in range(5):
        assert _verify(db_client, _wrong(code)).status_code == 401
    assert _verify(db_client, code).status_code == 401
    challenge = db_session.scalars(select(OtpChallenge)).one()
    assert challenge.attempts == 5
    assert challenge.invalidated_at is not None
    assert challenge.consumed_at is None


def test_successful_otp_is_consumed_and_cannot_be_reused(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    code = otp_outbox.last_code(PHONE)
    assert _verify(db_client, code).status_code == 200
    assert db_session.scalars(select(OtpChallenge)).one().consumed_at is not None
    assert _verify(db_client, code).status_code == 401


def test_new_request_invalidates_previous_code(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    first = otp_outbox.last_code(PHONE)
    _skip_cooldown(db_session)
    _request(db_client)
    second = otp_outbox.last_code(PHONE)
    if first != second:
        assert _verify(db_client, first).status_code == 401
    assert _verify(db_client, second).status_code == 200
    challenges = db_session.scalars(select(OtpChallenge).order_by(OtpChallenge.created_at)).all()
    assert challenges[0].invalidated_at is not None


def test_resend_cooldown(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    assert _request(db_client).status_code == 202
    response = _request(db_client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert 0 < int(response.headers["Retry-After"]) <= 61
    assert len(otp_outbox.sent) == 1
    _skip_cooldown(db_session)
    assert _request(db_client).status_code == 202


def test_hourly_limit_per_number(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    for _ in range(5):
        assert _request(db_client).status_code == 202
        _skip_cooldown(db_session)
    response = _request(db_client)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) == 3600
    throttled = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.otp.throttled")
    ).all()
    assert throttled[-1].details["scope"] == "identifier_hourly_limit"


def test_hourly_limit_per_ip(
    db_client: TestClient, otp_outbox: CapturingOtpSender, db_session: Session
) -> None:
    for i in range(30):
        assert _request(db_client, f"+9198000{i:05d}").status_code == 202
    response = _request(db_client, "+919811199999")
    assert response.status_code == 429
    events = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.otp.throttled")
    ).all()
    assert events[-1].details["scope"] == "ip_hourly_limit"


def test_otp_disabled_returns_503(db_client: TestClient, app: FastAPI) -> None:
    app.state.otp_sender = None
    response = _request(db_client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_delivery_failure_returns_503(db_client: TestClient, app: FastAPI, parent: Any) -> None:
    class BrokenSender:
        def send(self, *, phone: str, code: str, expires_in_seconds: int) -> None:
            raise ConnectionError("provider down")

    app.state.otp_sender = BrokenSender()
    response = _request(db_client)
    assert response.status_code == 503
    assert "provider" not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"phone": "9800000001"},  # no country code
        {"phone": "+91abc"},
        {"phone": PHONE, "purpose": "password_reset"},
    ],
)
def test_request_validation(
    db_client: TestClient, otp_outbox: CapturingOtpSender, payload: dict[str, str]
) -> None:
    assert db_client.post(REQUEST, json=payload).status_code == 422


@pytest.mark.parametrize("code", ["12ab56", "123", "12345678901", ""])
def test_verify_validation(db_client: TestClient, code: str) -> None:
    response = _verify(db_client, code)
    assert response.status_code == 422
    assert code not in response.text or code == ""


def test_verify_without_request_fails(db_client: TestClient, parent: Any) -> None:
    assert _verify(db_client, "123456").status_code == 401


def test_otp_events_are_audited(
    db_client: TestClient, otp_outbox: CapturingOtpSender, parent: Any, db_session: Session
) -> None:
    _request(db_client)
    code = otp_outbox.last_code(PHONE)
    _verify(db_client, _wrong(code))
    _verify(db_client, code)
    actions = db_session.scalars(select(AuditLog.action).order_by(AuditLog.created_at)).all()
    assert (
        actions[-5:]
        == [
            "user.created",
            "auth.otp.requested",
            "auth.otp.failed",
            "auth.otp.verified",
            "auth.login.succeeded",
        ][-5:]
    )
    dumped = str(db_session.execute(text("SELECT metadata FROM audit_logs")).all())
    assert code not in dumped
