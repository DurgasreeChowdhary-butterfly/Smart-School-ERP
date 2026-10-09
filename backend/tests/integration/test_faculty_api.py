"""Faculty records: CRUD, validation, identity/authentication, isolation, RBAC and audit."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.faculty import Faculty
from app.models.user import User, UserStatus
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory, UserFactory, bearer, login
from tests.integration.conftest import FACULTY_PASSWORD, World

pytestmark = pytest.mark.db
R = RoleName
URL = "/api/v1/faculty"


def _actions(db_session: Session, prefix: str) -> list[str]:
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.action.like(f"{prefix}.%"))
            .order_by(AuditLog.created_at)
        )
    )


def _dump(db_session: Session) -> str:
    return "".join(
        str(db_session.execute(text(f"SELECT * FROM {table}")).all())  # noqa: S608
        for table in ("faculty", "users", "audit_logs")
    )


# ========================================================================== creation
def test_create_faculty_creates_faculty_user(world: World, db_session: Session) -> None:
    response = world.faculty(
        email=" Ravi.T@School.Example ",
        mobile="+91 98000 22222",
        employee_id=" emp-007 ",
        middle_name=" K ",
        designation="Senior Teacher",
        department="Mathematics",
        joining_date="2024-06-01",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["employee_id"] == "EMP-007"
    assert body["email"] == "ravi.t@school.example"
    assert body["mobile"] == "+919800022222"
    assert body["middle_name"] == "K"
    assert body["status"] == "active"
    assert body["is_active"] is True
    assert body["school_id"] == str(SCHOOL_A)
    assert "password" not in response.text
    assert FACULTY_PASSWORD not in response.text

    faculty = db_session.get(Faculty, uuid.UUID(body["id"]))
    assert faculty is not None
    user = db_session.get(User, faculty.user_id)
    assert user is not None
    assert str(user.id) == body["user_id"]
    assert user.school_id == SCHOOL_A
    assert user.full_name == "Fac K " + body["last_name"]
    assert user.password_hash is not None
    assert user.password_hash.startswith("$argon2id$")
    roles = world.client.get(f"/api/v1/users/{user.id}/roles", headers=world.admin_a).json()
    assert roles["roles"] == ["FACULTY"]
    # The plaintext password is nowhere in the database (only the Argon2id hash).
    assert FACULTY_PASSWORD not in _dump(db_session)
    assert _actions(db_session, "faculty") == ["faculty.created"]
    assert "user.created" in _actions(db_session, "user")


@pytest.mark.parametrize(
    "extra",
    [
        {"password": None},  # new accounts need a password
        {"email": None, "mobile": None, "user_id": None},  # and a sign-in identifier
        {"employee_id": "bad id!"},
        {"employee_id": ""},
        {"first_name": "  "},
        {"email": "not-an-email"},
        {"mobile": "12345"},
        {"unknown": "x"},
        {"is_active": False},
        {"school_id": str(SCHOOL_B)},
    ],
)
def test_create_validation(world: World, extra: dict[str, Any]) -> None:
    body = {
        "employee_id": "E1",
        "first_name": "F",
        "last_name": "L",
        "email": "valid@school.test",
        "password": FACULTY_PASSWORD,
        **extra,
    }
    body = {k: v for k, v in body.items() if v is not None}
    assert world.client.post(URL, headers=world.admin_a, json=body).status_code == 422


def test_password_policy_applies(world: World) -> None:
    response = world.faculty(email="ravindra@school.test", password="ravindra-2026-x")
    assert response.status_code == 422
    assert world.faculty(password="short").status_code == 422


def test_duplicate_employee_id_rejected_case_insensitively(
    world: World, db_session: Session
) -> None:
    assert world.faculty(employee_id="emp-1").status_code == 201
    duplicate = world.faculty(employee_id=" EMP-1 ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["details"][0]["loc"] == ["body", "employee_id"]
    # No orphaned login was left behind by the failed attempt.
    users = db_session.scalar(
        text(
            "SELECT count(*) FROM users u JOIN user_roles ur ON ur.user_id = u.id "
            "JOIN roles r ON r.id = ur.role_id WHERE r.name = 'FACULTY'"
        )
    )
    assert users == db_session.scalar(text("SELECT count(*) FROM faculty")) == 1


def test_employee_id_unique_per_school_only(world: World) -> None:
    assert world.faculty(employee_id="SAME").status_code == 201
    assert world.faculty(world.admin_b, employee_id="SAME").status_code == 201


def test_duplicate_email_or_mobile_is_conflict_and_atomic(
    world: World, db_session: Session
) -> None:
    world.faculty(email="dup@school.test", mobile="+919811111111")
    assert world.faculty(email="dup@school.test").status_code == 409
    assert world.faculty(mobile="+919811111111").status_code == 409
    assert db_session.scalar(text("SELECT count(*) FROM faculty")) == 1


def test_attach_existing_faculty_user(
    world: World, make_user: UserFactory, db_session: Session
) -> None:
    teacher = make_user(roles=(R.FACULTY,), email="legacy-teacher@school.test")
    response = world.faculty(user_id=str(teacher.id), first_name="Leg", last_name="Acy")
    assert response.status_code == 201, response.text
    assert response.json()["email"] == "legacy-teacher@school.test"
    assert response.json()["user_id"] == str(teacher.id)
    db_session.refresh(teacher)
    assert teacher.full_name == "Leg Acy"
    # The existing password keeps working: same identity, no second credential.
    assert world.faculty_headers("legacy-teacher@school.test", "Correct-Horse-Battery-9")
    # A user can back only one faculty record.
    again = world.faculty(user_id=str(teacher.id))
    assert again.status_code == 409
    assert again.json()["error"]["details"][0]["loc"] == ["body", "user_id"]


def test_attach_rejects_ineligible_users(world: World, make_user: UserFactory) -> None:
    other_school = make_user(roles=(R.FACULTY,), school_id=SCHOOL_B)
    not_faculty = make_user(roles=(R.MANAGEMENT,))
    inactive = make_user(roles=(R.FACULTY,), status=UserStatus.DEACTIVATED)
    for user, message in (
        (other_school, "User not found."),
        (not_faculty, "The user does not have the FACULTY role."),
        (inactive, "The user is inactive."),
    ):
        response = world.faculty(user_id=str(user.id))
        assert response.status_code == 422
        assert response.json()["error"]["message"] == message
    assert world.faculty(user_id=str(uuid.uuid4())).status_code == 422
    mixed = world.faculty(user_id=str(other_school.id), email="x@school.test")
    assert mixed.status_code == 422


# ============================================================================ reads
def test_list_get_filters_and_pagination(world: World) -> None:
    ids = [world.faculty(employee_id=f"E{i}").json()["id"] for i in range(3)]
    world.faculty(world.admin_b, employee_id="E0")
    page = world.client.get(URL, headers=world.admin_a, params={"page_size": 2}).json()
    assert (page["total"], len(page["items"]), page["pages"]) == (3, 2, 2)
    one = world.client.get(URL, headers=world.admin_a, params={"employee_id": "e1"}).json()
    assert [item["id"] for item in one["items"]] == [ids[1]]
    world.client.patch(f"{URL}/{ids[2]}", headers=world.admin_a, json={"status": "inactive"})
    inactive = world.client.get(URL, headers=world.admin_a, params={"status": "inactive"})
    assert [item["id"] for item in inactive.json()["items"]] == [ids[2]]
    assert world.client.get(f"{URL}/{ids[0]}", headers=world.admin_a).status_code == 200
    assert world.client.get(f"{URL}/{uuid.uuid4()}", headers=world.admin_a).status_code == 404
    assert world.client.get(f"{URL}/not-a-uuid", headers=world.admin_a).status_code == 422


def test_school_isolation(world: World) -> None:
    b_faculty = world.faculty(world.admin_b).json()
    assert world.client.get(URL, headers=world.admin_a).json()["total"] == 0
    assert world.client.get(f"{URL}/{b_faculty['id']}", headers=world.admin_a).status_code == 404
    patch = world.client.patch(
        f"{URL}/{b_faculty['id']}", headers=world.admin_a, json={"first_name": "X"}
    )
    assert patch.status_code == 404
    # Naming another school in the header is refused, not honoured.
    headers = {**world.admin_a, "X-School-ID": str(SCHOOL_B)}
    assert world.client.get(URL, headers=headers).status_code == 404


def test_super_admin_manages_faculty_in_selected_school(
    world: World, auth_headers: HeadersFactory
) -> None:
    root_b = auth_headers(R.SUPER_ADMIN, SCHOOL_B)
    created = world.faculty(root_b)
    assert created.status_code == 201
    assert created.json()["school_id"] == str(SCHOOL_B)
    assert world.client.get(URL, headers=world.admin_b).json()["total"] == 1
    assert world.client.get(URL, headers=world.admin_a).json()["total"] == 0
    no_school = auth_headers(R.SUPER_ADMIN, None)
    assert world.client.get(URL, headers=no_school).status_code == 400


def test_management_reads_but_cannot_manage(world: World, auth_headers: HeadersFactory) -> None:
    mgmt = auth_headers(R.MANAGEMENT)
    faculty = world.faculty().json()
    assert world.client.get(URL, headers=mgmt).json()["total"] == 1
    assert world.client.get(f"{URL}/{faculty['id']}", headers=mgmt).status_code == 200
    assert world.faculty(mgmt).status_code == 403
    patch = world.client.patch(f"{URL}/{faculty['id']}", headers=mgmt, json={"first_name": "X"})
    assert patch.status_code == 403


@pytest.mark.parametrize("role", [R.PARENT, R.STUDENT])
def test_parents_and_students_cannot_read_faculty(
    world: World, auth_headers: HeadersFactory, role: RoleName
) -> None:
    faculty = world.faculty().json()
    headers = auth_headers(role)
    assert world.client.get(URL, headers=headers).status_code == 403
    assert world.client.get(f"{URL}/{faculty['id']}", headers=headers).status_code == 403
    assert world.client.get(f"{URL}/me", headers=headers).status_code == 403


# ========================================================================== updates
def test_update_syncs_user_identity_and_audits_contacts_by_name(
    world: World, db_session: Session
) -> None:
    faculty = world.faculty(email="old@school.test").json()
    response = world.client.patch(
        f"{URL}/{faculty['id']}",
        headers=world.admin_a,
        json={
            "first_name": "Meera",
            "email": "new@school.test",
            "mobile": "+919822222222",
            "designation": "HOD",
            "employee_id": "emp-new",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["email"], body["mobile"], body["employee_id"]) == (
        "new@school.test",
        "+919822222222",
        "EMP-NEW",
    )
    user = db_session.get(User, uuid.UUID(faculty["user_id"]))
    assert user is not None
    assert (user.email, user.phone) == ("new@school.test", "+919822222222")
    assert user.full_name.startswith("Meera ")
    # Signs in with the new identifiers only.
    assert world.faculty_headers("+919822222222")
    assert (
        world.client.post(
            "/api/v1/auth/login", json={"identifier": "old@school.test", "password": "x" * 12}
        ).status_code
        == 401
    )
    event = db_session.scalars(select(AuditLog).where(AuditLog.action == "faculty.updated")).one()
    assert {"field": "email", "redacted": True} in event.details["changes"]
    assert "new@school.test" not in str(event.details)
    assert "+919822222222" not in str(event.details)
    assert {"field": "designation", "from": None, "to": "HOD"} in event.details["changes"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"first_name": None},
        {"status": "retired"},
        {"user_id": str(uuid.uuid4())},
        {"school_id": str(SCHOOL_B)},
        {"password": "Another-Secret-123"},
    ],
)
def test_update_validation(world: World, body: dict[str, Any]) -> None:
    faculty = world.faculty().json()
    response = world.client.patch(f"{URL}/{faculty['id']}", headers=world.admin_a, json=body)
    assert response.status_code == 422


def test_cannot_remove_last_sign_in_identifier(world: World) -> None:
    faculty = world.faculty(email="only@school.test").json()
    response = world.client.patch(
        f"{URL}/{faculty['id']}", headers=world.admin_a, json={"email": None}
    )
    assert response.status_code == 422
    both = world.faculty(email="both@school.test", mobile="+919833333333").json()
    ok = world.client.patch(f"{URL}/{both['id']}", headers=world.admin_a, json={"email": None})
    assert ok.status_code == 200
    assert ok.json()["email"] is None


@pytest.mark.parametrize("body", [{"email": "taken@school.test"}, {"employee_id": "taken"}])
def test_update_conflicts(world: World, body: dict[str, str]) -> None:
    world.faculty(email="taken@school.test", employee_id="TAKEN")
    faculty = world.faculty().json()
    response = world.client.patch(f"{URL}/{faculty['id']}", headers=world.admin_a, json=body)
    assert response.status_code == 409


def test_noop_update_is_not_audited(world: World, db_session: Session) -> None:
    faculty = world.faculty().json()
    response = world.client.patch(
        f"{URL}/{faculty['id']}", headers=world.admin_a, json={"first_name": "Fac"}
    )
    assert response.status_code == 200
    assert _actions(db_session, "faculty") == ["faculty.created"]


# ================================================================= authentication
def test_faculty_signs_in_with_email_or_mobile(world: World) -> None:
    world.faculty(email="sign@school.test", mobile="+919844444444")
    for identifier in ("sign@school.test", "+919844444444"):
        me = world.client.get("/api/v1/auth/me", headers=world.faculty_headers(identifier))
        assert me.status_code == 200
        assert me.json()["roles"] == ["FACULTY"]
    wrong = world.client.post(
        "/api/v1/auth/login", json={"identifier": "sign@school.test", "password": "Wrong-Pass-123"}
    )
    assert wrong.status_code == 401


def test_deactivation_blocks_login_access_and_refresh_then_reactivation_restores(
    world: World, db_session: Session
) -> None:
    faculty = world.faculty(email="leaver@school.test").json()
    tokens = login(world.client, "leaver@school.test", FACULTY_PASSWORD)
    headers = bearer(tokens["access_token"])
    assert world.client.get(f"{URL}/me", headers=headers).status_code == 200

    off = world.client.patch(
        f"{URL}/{faculty['id']}", headers=world.admin_a, json={"status": "inactive"}
    )
    assert off.status_code == 200
    assert (off.json()["status"], off.json()["is_active"]) == ("inactive", False)
    user = db_session.get(User, uuid.UUID(faculty["user_id"]))
    assert user is not None
    assert user.status == "deactivated"
    # Phase 2 session checks apply immediately: token, refresh and login all fail.
    assert world.client.get(f"{URL}/me", headers=headers).status_code == 401
    refresh = world.client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refresh.status_code == 401
    assert (
        world.client.post(
            "/api/v1/auth/login",
            json={"identifier": "leaver@school.test", "password": FACULTY_PASSWORD},
        ).status_code
        == 401
    )

    on = world.client.patch(
        f"{URL}/{faculty['id']}", headers=world.admin_a, json={"status": "active"}
    )
    assert on.json()["is_active"] is True
    assert world.faculty_headers("leaver@school.test")
    assert _actions(db_session, "faculty") == [
        "faculty.created",
        "faculty.deactivated",
        "faculty.reactivated",
    ]


# ======================================================================= self view
def test_faculty_self_view(world: World) -> None:
    mine = world.faculty(email="me@school.test").json()
    other = world.faculty().json()
    headers = world.faculty_headers("me@school.test")
    me = world.client.get(f"{URL}/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == mine["id"]
    listing = world.client.get(URL, headers=headers).json()
    assert [item["id"] for item in listing["items"]] == [mine["id"]]
    assert world.client.get(f"{URL}/{mine['id']}", headers=headers).status_code == 200
    # Faculty A cannot read Faculty B (IDOR): indistinguishable from an unknown id.
    assert world.client.get(f"{URL}/{other['id']}", headers=headers).status_code == 404
    filtered = world.client.get(URL, headers=headers, params={"employee_id": other["employee_id"]})
    assert filtered.json()["total"] == 0


def test_faculty_cannot_manage_anything(fworld: World) -> None:
    world = fworld
    mine = world.faculty(email="me2@school.test").json()
    headers = world.faculty_headers("me2@school.test")
    assert world.faculty(headers).status_code == 403
    patch = world.client.patch(f"{URL}/{mine['id']}", headers=headers, json={"first_name": "X"})
    assert patch.status_code == 403
    assert world.assign(mine["id"], headers).status_code == 403
    roles = world.client.put(
        f"/api/v1/users/{mine['user_id']}/roles", headers=headers, json={"roles": ["SCHOOL_ADMIN"]}
    )
    assert roles.status_code == 403
    school = world.client.patch("/api/v1/school", headers=headers, json={"name": "Mine"})
    assert school.status_code == 403
    assert world.student("X1", headers).status_code == 403
    assert world.parent(headers).status_code == 403
    assert world.client.get("/api/v1/parents", headers=headers).status_code == 403


def test_faculty_role_without_record_has_no_profile(
    world: World, auth_headers: HeadersFactory
) -> None:
    headers = auth_headers(R.FACULTY)
    assert world.client.get(f"{URL}/me", headers=headers).status_code == 404
    assert world.client.get(f"{URL}/me/assignments", headers=headers).status_code == 404
    assert world.client.get(URL, headers=headers).json()["total"] == 0


def test_staff_have_no_faculty_self_endpoints(world: World) -> None:
    assert world.client.get(f"{URL}/me", headers=world.admin_a).status_code == 403


def test_audit_never_contains_secrets(world: World, db_session: Session) -> None:
    faculty = world.faculty(email="audit@school.test").json()
    tokens = login(world.client, "audit@school.test", FACULTY_PASSWORD)
    world.client.patch(f"{URL}/{faculty['id']}", headers=world.admin_a, json={"status": "inactive"})
    dump = str(db_session.execute(text("SELECT * FROM audit_logs")).all())
    for secret in (FACULTY_PASSWORD, tokens["access_token"], tokens["refresh_token"], "$argon2"):
        assert secret not in dump
    created = db_session.scalars(select(AuditLog).where(AuditLog.action == "faculty.created")).one()
    assert created.details["school_id"] == str(SCHOOL_A)
    assert created.details["employee_id"] == faculty["employee_id"]
    assert created.resource_type == "faculty"
    assert created.user_id is not None
