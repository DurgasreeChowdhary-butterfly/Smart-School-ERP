"""Student and parent sign-in through the unchanged Phase 2 authentication stack."""

from __future__ import annotations

import jwt
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, HeadersFactory, bearer
from tests.integration.conftest import STUDENT_PASSWORD, World

pytestmark = pytest.mark.db


def test_student_login_resolves_real_student(world: World) -> None:
    student = world.student("2026/007", password=STUDENT_PASSWORD).json()
    response = world.login_student(" 2026/007 ", code="sch-a")  # normalised like creation
    assert response.status_code == 200, response.text
    tokens = response.json()
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    assert set(claims) == {"iss", "aud", "sub", "sid", "typ", "jti", "iat", "nbf", "exp"}
    assert "2026/007" not in str(claims)  # no student data in the token

    me = world.client.get("/api/v1/auth/me", headers=bearer(tokens["access_token"])).json()
    assert me["roles"] == ["STUDENT"]
    assert me["school_id"] == str(SCHOOL_A)
    own = world.client.get(
        f"/api/v1/students/{student['id']}", headers=bearer(tokens["access_token"])
    )
    assert own.status_code == 200


@pytest.mark.parametrize(
    ("adm", "password", "code"),
    [
        ("S1", "Wrong-Password-99", "SCH-A"),
        ("S1", STUDENT_PASSWORD, "SCH-B"),  # right credentials, wrong school
        ("NOPE", STUDENT_PASSWORD, "SCH-A"),
        ("S2", STUDENT_PASSWORD, "SCH-A"),  # student exists but has no login
        ("S1", STUDENT_PASSWORD, "UNKNOWN"),
    ],
)
def test_student_login_failures_are_generic(
    world: World, adm: str, password: str, code: str
) -> None:
    world.student("S1", password=STUDENT_PASSWORD)
    world.student("S2")
    response = world.login_student(adm, password, code)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid credentials."


@pytest.mark.parametrize("status", ["inactive", "transferred", "graduated"])
def test_non_active_student_cannot_log_in(world: World, status: str) -> None:
    student = world.student("S1", password=STUDENT_PASSWORD).json()
    world.client.patch(
        f"/api/v1/students/{student['id']}", headers=world.admin_a, json={"status": status}
    )
    assert world.login_student("S1").status_code == 401


def test_student_login_is_throttled(world: World) -> None:
    world.student("S1", password=STUDENT_PASSWORD)
    for _ in range(5):
        assert world.login_student("S1", "Wrong-Password-99").status_code == 401
    assert world.login_student("S1").status_code == 429


def test_student_refresh_and_logout_unchanged(world: World) -> None:
    world.student("S1", password=STUDENT_PASSWORD)
    tokens = world.login_student("S1").json()
    rotated = world.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated.status_code == 200
    assert (
        world.client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    logout = world.client.post(
        "/api/v1/auth/logout", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert logout.status_code == 204
    assert (
        world.client.get(
            "/api/v1/auth/me", headers=bearer(rotated.json()["access_token"])
        ).status_code
        == 401
    )


def test_student_login_audited_without_secrets(world: World, db_session: Session) -> None:
    world.student("S1", password=STUDENT_PASSWORD)
    world.login_student("S1", "Wrong-Password-99")
    tokens = world.login_student("S1").json()
    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action.like("auth.login.%")).order_by(AuditLog.created_at)
    ).all()
    assert [(r.action, r.details.get("identifier_kind")) for r in rows][-2:] == [
        ("auth.login.failed", "student"),
        ("auth.login.succeeded", None),
    ]
    dump = str(db_session.execute(text("SELECT metadata FROM audit_logs")).all())
    for secret in (
        STUDENT_PASSWORD,
        "Wrong-Password-99",
        tokens["access_token"],
        tokens["refresh_token"],
    ):
        assert secret not in dump


def test_parent_otp_login(world: World, db_session: Session) -> None:
    parent = world.parent(mobile="+919822200001").json()
    headers = world.parent_headers("+919822200001")
    me = world.client.get("/api/v1/auth/me", headers=headers).json()
    assert me["roles"] == ["PARENT"]
    assert world.client.get(f"/api/v1/parents/{parent['id']}", headers=headers).status_code == 200
    verified = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.otp.verified")
    ).one()
    assert verified.user_id is not None
    code = world.outbox.last_code("+919822200001")
    assert f"'{code}'" not in str(db_session.execute(text("SELECT metadata FROM audit_logs")).all())


def test_inactive_parent_cannot_authenticate(world: World) -> None:
    parent = world.parent(mobile="+919822200002").json()
    world.client.patch(
        f"/api/v1/parents/{parent['id']}", headers=world.admin_a, json={"is_active": False}
    )
    response = world.client.post("/api/v1/auth/otp/request", json={"phone": "+919822200002"})
    unknown = world.client.post("/api/v1/auth/otp/request", json={"phone": "+919822299999"})
    assert response.status_code == unknown.status_code == 202
    assert response.json() == unknown.json()  # same generic response as an unknown number
    assert world.outbox.sent == []


def test_parent_of_inactive_school_cannot_authenticate(
    world: World, auth_headers: HeadersFactory
) -> None:
    world.parent(mobile="+919822200003")
    root = auth_headers(RoleName.SUPER_ADMIN, SCHOOL_A)
    world.client.patch("/api/v1/school", headers=root, json={"is_active": False})
    world.client.post("/api/v1/auth/otp/request", json={"phone": "+919822200003"})
    assert world.outbox.sent == []
