"""/school and /schools: own-school access, SUPER_ADMIN selection, isolation, inactivity."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.school import School
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory, UserFactory, bearer, login

pytestmark = pytest.mark.db

R = RoleName


def _get(client: TestClient, headers: dict[str, str]) -> Any:
    return client.get("/api/v1/school", headers=headers)


def _patch(client: TestClient, headers: dict[str, str], body: dict[str, Any]) -> Any:
    return client.patch("/api/v1/school", headers=headers, json=body)


# ------------------------------------------------------------------------- reading
@pytest.mark.parametrize("role", [R.SCHOOL_ADMIN, R.MANAGEMENT, R.FACULTY, R.PARENT, R.STUDENT])
def test_school_users_read_their_own_school(
    db_client: TestClient, auth_headers: HeadersFactory, role: RoleName
) -> None:
    response = _get(db_client, auth_headers(role))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(SCHOOL_A)
    assert body["code"] == "SCH-A"
    assert body["timezone"] == "UTC"


def test_school_user_cannot_select_another_school(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    headers = auth_headers(R.SCHOOL_ADMIN)
    own = _get(db_client, {**headers, "X-School-ID": str(SCHOOL_A)})
    other = _get(db_client, {**headers, "X-School-ID": str(SCHOOL_B)})
    unknown = _get(db_client, {**headers, "X-School-ID": "00000000-0000-4000-8000-000000000999"})
    assert own.status_code == 200
    # Another school and a non-existent school are indistinguishable.
    assert other.status_code == unknown.status_code == 404
    assert other.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_super_admin_must_select_a_school(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    response = _get(db_client, auth_headers(R.SUPER_ADMIN, select_school=False))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "school_selection_required"


def test_super_admin_operates_across_schools(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    root = auth_headers(R.SUPER_ADMIN, select_school=False)
    a = _get(db_client, {**root, "X-School-ID": str(SCHOOL_A)})
    b = _get(db_client, {**root, "X-School-ID": str(SCHOOL_B)})
    assert (a.json()["id"], b.json()["id"]) == (str(SCHOOL_A), str(SCHOOL_B))
    missing = _get(db_client, {**root, "X-School-ID": "00000000-0000-4000-8000-000000000999"})
    assert missing.status_code == 404


def test_malformed_school_header_is_rejected(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    root = auth_headers(R.SUPER_ADMIN, select_school=False)
    assert _get(db_client, {**root, "X-School-ID": "not-a-uuid"}).status_code == 422


def test_unauthenticated_is_401(db_client: TestClient) -> None:
    assert db_client.get("/api/v1/school").status_code == 401


# ------------------------------------------------------------------------ updating
def test_school_admin_updates_own_school(
    db_client: TestClient, auth_headers: HeadersFactory, db_session: Session
) -> None:
    response = _patch(
        db_client,
        auth_headers(R.SCHOOL_ADMIN),
        {
            "name": "  Green Hills School ",
            "email": " Office@GreenHills.Example ",
            "phone": "+91 98765 43210",
            "timezone": "Asia/Kolkata",
            "city": "Pune",
            "logo_url": "https://cdn.example/logo.png",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Green Hills School"
    assert body["email"] == "office@greenhills.example"
    assert body["phone"] == "+919876543210"
    assert body["timezone"] == "Asia/Kolkata"

    event = db_session.scalars(select(AuditLog).where(AuditLog.action == "school.updated")).one()
    assert event.resource_id == str(SCHOOL_A)
    changed = {c["field"]: c for c in event.details["changes"]}
    assert changed["name"] == {"field": "name", "from": "School A", "to": "Green Hills School"}
    assert set(changed) == {"name", "email", "phone", "timezone", "city", "logo_url"}


def test_optional_fields_can_be_cleared(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    headers = auth_headers(R.SCHOOL_ADMIN)
    _patch(db_client, headers, {"city": "Pune"})
    response = _patch(db_client, headers, {"city": None})
    assert response.status_code == 200
    assert response.json()["city"] is None


def test_noop_update_is_not_audited(
    db_client: TestClient, auth_headers: HeadersFactory, db_session: Session
) -> None:
    assert _patch(db_client, auth_headers(R.SCHOOL_ADMIN), {"name": "School A"}).status_code == 200
    assert (
        db_session.scalars(select(AuditLog).where(AuditLog.action == "school.updated")).all() == []
    )


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing to update
        {"code": "NEW"},  # immutable
        {"id": "00000000-0000-4000-8000-000000000001"},
        {"name": None},  # required field cannot be nulled
        {"name": "   "},
        {"timezone": "Mars/Olympus"},
        {"timezone": None},
        {"email": "not-an-email"},
        {"phone": "12345"},
        {"logo_url": "http://insecure.example/logo.png"},
        {"logo_url": "javascript:alert(1)"},
        {"name": "x" * 201},
    ],
)
def test_update_validation(
    db_client: TestClient, auth_headers: HeadersFactory, body: dict[str, Any]
) -> None:
    response = _patch(db_client, auth_headers(R.SCHOOL_ADMIN), body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("role", [R.MANAGEMENT, R.FACULTY, R.PARENT, R.STUDENT])
def test_only_school_managers_can_update(
    db_client: TestClient, auth_headers: HeadersFactory, role: RoleName
) -> None:
    response = _patch(db_client, auth_headers(role), {"name": "Hijacked"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_school_admin_cannot_update_another_school(
    db_client: TestClient, auth_headers: HeadersFactory, db_session: Session
) -> None:
    headers = {**auth_headers(R.SCHOOL_ADMIN), "X-School-ID": str(SCHOOL_B)}
    assert _patch(db_client, headers, {"name": "Hijacked"}).status_code == 404
    assert db_session.get(School, SCHOOL_B).name == "School B"  # type: ignore[union-attr]


def test_school_admin_cannot_change_activation(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    response = _patch(db_client, auth_headers(R.SCHOOL_ADMIN), {"is_active": False})
    assert response.status_code == 403


def test_super_admin_updates_any_school(
    db_client: TestClient, auth_headers: HeadersFactory
) -> None:
    response = _patch(db_client, auth_headers(R.SUPER_ADMIN, SCHOOL_B), {"name": "B Renamed"})
    assert response.status_code == 200
    assert response.json()["id"] == str(SCHOOL_B)


# --------------------------------------------------------------------- inactivity
def test_deactivated_school_suspends_its_users(
    db_client: TestClient, make_user: UserFactory, auth_headers: HeadersFactory, db_session: Session
) -> None:
    teacher = make_user(roles=(R.FACULTY,))
    assert teacher.email is not None
    tokens = login(db_client, teacher.email)
    root = auth_headers(R.SUPER_ADMIN, SCHOOL_A)

    assert _patch(db_client, root, {"is_active": False}).json()["is_active"] is False

    # Existing access and refresh tokens stop working; new sign-ins fail generically.
    assert (
        db_client.get("/api/v1/auth/me", headers=bearer(tokens["access_token"])).status_code == 401
    )
    refresh = db_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh.status_code == 401
    relogin = db_client.post(
        "/api/v1/auth/login",
        json={"identifier": teacher.email, "password": "Correct-Horse-Battery-9"},
    )
    assert relogin.status_code == 401
    assert relogin.json()["error"]["code"] == "invalid_credentials"
    failure = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "auth.login.failed")
        .order_by(AuditLog.created_at.desc())
    ).first()
    assert failure is not None
    assert failure.details["reason"] == "school_inactive"

    # The platform admin can still read and reactivate it; history is preserved.
    assert _get(db_client, root).json()["is_active"] is False
    assert _patch(db_client, root, {"is_active": True}).status_code == 200
    assert login(db_client, teacher.email)["access_token"]


def test_other_schools_unaffected_by_deactivation(
    db_client: TestClient, make_user: UserFactory, auth_headers: HeadersFactory
) -> None:
    b_user = make_user(roles=(R.FACULTY,), school_id=SCHOOL_B)
    assert b_user.email is not None
    _patch(db_client, auth_headers(R.SUPER_ADMIN, SCHOOL_A), {"is_active": False})
    assert login(db_client, b_user.email)["access_token"]


# ------------------------------------------------------------------- /schools list
def test_super_admin_lists_all_schools(db_client: TestClient, auth_headers: HeadersFactory) -> None:
    response = db_client.get(
        "/api/v1/schools", headers=auth_headers(R.SUPER_ADMIN, select_school=False)
    )
    assert response.status_code == 200
    codes = [s["code"] for s in response.json()["items"]]
    assert {"SCH-A", "SCH-B"} <= set(codes)


@pytest.mark.parametrize("role", [R.SCHOOL_ADMIN, R.MANAGEMENT, R.FACULTY])
def test_school_users_cannot_list_all_schools(
    db_client: TestClient, auth_headers: HeadersFactory, role: RoleName
) -> None:
    assert db_client.get("/api/v1/schools", headers=auth_headers(role)).status_code == 403
