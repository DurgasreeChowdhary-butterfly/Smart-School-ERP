"""Students, parents and links: CRUD, validation, isolation and RBAC (staff view)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.people import Student
from app.models.user import User
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import STUDENT_PASSWORD, World

pytestmark = pytest.mark.db
R = RoleName


def _actions(db_session: Session, prefix: str) -> list[str]:
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.action.like(f"{prefix}.%"))
            .order_by(AuditLog.created_at)
        )
    )


# ========================================================================== students
def test_create_student_normalises_and_hides_secrets(world: World, db_session: Session) -> None:
    response = world.student(
        " 2026/001 ",
        password=STUDENT_PASSWORD,
        middle_name="  Kumar ",
        email=" Asha@Mail.Example ",
        phone="+91 98000 11111",
        gender="female",
        date_of_birth="2012-04-05",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["admission_number"] == "2026/001"
    assert body["middle_name"] == "Kumar"
    assert body["email"] == "asha@mail.example"
    assert body["phone"] == "+919800011111"
    assert body["has_login"] is True
    assert body["school_id"] == str(SCHOOL_A)
    assert "password" not in response.text
    assert STUDENT_PASSWORD not in response.text

    # The password exists only as an Argon2id hash on the linked STUDENT user.
    student = db_session.get(Student, uuid.UUID(body["id"]))
    assert student is not None
    user = db_session.get(User, student.user_id)
    assert user is not None
    assert user.password_hash is not None
    assert user.password_hash.startswith("$argon2id$")
    dump = str(db_session.execute(text("SELECT * FROM students")).all())
    dump += str(db_session.execute(text("SELECT * FROM users")).all())
    dump += str(db_session.execute(text("SELECT * FROM audit_logs")).all())
    assert STUDENT_PASSWORD not in dump


def test_student_without_password_has_no_login(world: World) -> None:
    body = world.student("S1").json()
    assert body["has_login"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"first_name": "   "},
        {"admission_number": "has space"},
        {"admission_number": "/leading"},
        {"gender": "unknown"},
        {"date_of_birth": "2999-01-01"},
        {"email": "nope"},
        {"phone": "12345"},
        {"status": "active"},  # not settable at creation
        {"school_id": str(SCHOOL_B)},
        {"password": "short"},  # violates the password policy
    ],
)
def test_student_create_validation(world: World, extra: dict[str, Any]) -> None:
    body = {"admission_number": "S1", "first_name": "A", "last_name": "B", **extra}
    response = world.client.post("/api/v1/students", headers=world.admin_a, json=body)
    assert response.status_code == 422, response.text


def test_duplicate_admission_number_rejected_case_insensitively(world: World) -> None:
    world.student("adm-1")
    response = world.student("ADM-1")
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["loc"] == ["body", "admission_number"]


def test_admission_number_unique_per_school_only(world: World) -> None:
    assert world.student("S1").status_code == 201
    assert world.student("S1", world.admin_b).status_code == 201


def test_list_and_filters(world: World) -> None:
    s1 = world.student("S1", last_name="Alpha").json()
    s2 = world.student("S2", last_name="Beta").json()
    world.student("S3", last_name="Gamma")
    world.enroll(s1["id"])
    world.enroll(s2["id"], cls="class2_a", sec="section2_a")
    world.client.patch(
        f"/api/v1/students/{s2['id']}", headers=world.admin_a, json={"status": "graduated"}
    )

    def names(query: str) -> list[str]:
        response = world.client.get(f"/api/v1/students{query}", headers=world.admin_a)
        assert response.status_code == 200, response.text
        return [s["last_name"] for s in response.json()["items"]]

    assert names("") == ["Alpha", "Beta", "Gamma"]
    assert names(f"?class_id={world.ids['class_a']}") == ["Alpha"]
    assert names(f"?section_id={world.ids['section2_a']}") == ["Beta"]
    assert names(f"?academic_year_id={world.ids['year_a']}") == ["Alpha", "Beta"]
    assert names("?status=graduated") == ["Beta"]
    assert names("?admission_number=s3") == ["Gamma"]
    assert names(f"?class_id={world.ids['class_b']}") == []  # other school's class: nothing


def test_update_student_and_status_lifecycle(world: World, db_session: Session) -> None:
    student = world.student("S1", password=STUDENT_PASSWORD).json()
    path = f"/api/v1/students/{student['id']}"
    updated = world.client.patch(
        path,
        headers=world.admin_a,
        json={"first_name": "Asha", "address": "12 Road", "phone": None},
    )
    assert updated.status_code == 200
    assert updated.json()["first_name"] == "Asha"

    assert (
        world.client.patch(path, headers=world.admin_a, json={"status": "inactive"}).json()[
            "status"
        ]
        == "inactive"
    )
    assert world.login_student("S1").status_code == 401  # login disabled while inactive
    assert (
        world.client.patch(path, headers=world.admin_a, json={"status": "active"}).status_code
        == 200
    )
    assert world.login_student("S1").status_code == 200

    assert _actions(db_session, "student") == [
        "student.created",
        "student.updated",
        "student.deactivated",
        "student.reactivated",
    ]
    update_event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "student.updated")
    ).one()
    changes = {c["field"]: c for c in update_event.details["changes"]}
    assert changes["first_name"]["to"] == "Asha"
    # Personal data is audited by field name only.
    assert changes["address"] == {"field": "address", "redacted": True}
    assert "12 Road" not in str(update_event.details)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"admission_number": "NEW"},  # login identifier is immutable
        {"password": "New-Password-123"},  # no password change via PATCH
        {"first_name": None},
        {"status": "expelled"},
        {"school_id": str(SCHOOL_B)},
    ],
)
def test_student_update_validation(world: World, body: dict[str, Any]) -> None:
    student = world.student("S1").json()
    response = world.client.patch(
        f"/api/v1/students/{student['id']}", headers=world.admin_a, json=body
    )
    assert response.status_code == 422


def test_management_reads_but_cannot_write_students(
    world: World, auth_headers: HeadersFactory
) -> None:
    mgmt = auth_headers(R.MANAGEMENT)
    student = world.student("S1").json()
    assert world.client.get("/api/v1/students", headers=mgmt).json()["total"] == 1
    assert world.client.get(f"/api/v1/students/{student['id']}", headers=mgmt).status_code == 200
    assert world.student("S2", mgmt).status_code == 403
    patch = world.client.patch(
        f"/api/v1/students/{student['id']}", headers=mgmt, json={"first_name": "X"}
    )
    assert patch.status_code == 403
    assert world.enroll(student["id"], mgmt).status_code == 403


def test_faculty_without_assignments_sees_no_students(
    world: World, auth_headers: HeadersFactory
) -> None:
    """Phase 5: FACULTY reach students only through assignments; with none, nothing."""
    faculty = auth_headers(R.FACULTY)
    student = world.student("S1").json()
    world.enroll(student["id"])
    assert world.client.get("/api/v1/students", headers=faculty).json()["total"] == 0
    assert world.client.get(f"/api/v1/students/{student['id']}", headers=faculty).status_code == 404
    assert world.client.get("/api/v1/enrollments", headers=faculty).json()["total"] == 0


def test_status_change_requires_deactivate_permission(
    world: World, db_session: Session, auth_headers: HeadersFactory
) -> None:
    """Revoke student.deactivate from SCHOOL_ADMIN: status changes are refused."""
    student = world.student("S1").json()
    db_session.execute(
        text(
            "DELETE FROM role_permissions WHERE role_id = (SELECT id FROM roles WHERE name = "
            "'SCHOOL_ADMIN') "
            "AND permission_id = (SELECT id FROM permissions WHERE name = 'student.deactivate')"
        )
    )
    path = f"/api/v1/students/{student['id']}"
    assert (
        world.client.patch(path, headers=world.admin_a, json={"status": "inactive"}).status_code
        == 403
    )
    assert (
        world.client.patch(path, headers=world.admin_a, json={"first_name": "Ok"}).status_code
        == 200
    )


# ========================================================================== parents
def test_create_parent_creates_parent_user(world: World, db_session: Session) -> None:
    response = world.parent(mobile="+91 99000 00001", email="Mum@Mail.Example", occupation="Doctor")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mobile"] == "+919900000001"
    assert body["email"] == "mum@mail.example"
    roles = (
        db_session.execute(
            text(
                "SELECT r.name FROM parents p JOIN user_roles ur ON ur.user_id = p.user_id "
                "JOIN roles r ON r.id = ur.role_id WHERE p.id = :p"
            ),
            {"p": body["id"]},
        )
        .scalars()
        .all()
    )
    assert roles == ["PARENT"]
    password_hash = db_session.execute(
        text(
            "SELECT u.password_hash FROM parents p JOIN users u ON u.id = p.user_id WHERE p.id = :p"
        ),
        {"p": body["id"]},
    ).scalar()
    assert password_hash is None  # parents sign in with OTP only


def test_parent_mobile_must_be_unique(world: World) -> None:
    world.parent(mobile="+919900000001")
    duplicate = world.parent(mobile="+91 99000 00001")
    assert duplicate.status_code == 409
    other_school = world.parent(world.admin_b, mobile="+919900000001")
    assert other_school.status_code == 409  # mobile is a platform-wide login identifier


@pytest.mark.parametrize(
    "extra",
    [{"mobile": "9900000001"}, {"first_name": "  "}, {"email": "bad"}, {"password": "x"}],
)
def test_parent_create_validation(world: World, extra: dict[str, Any]) -> None:
    assert world.parent(**extra).status_code == 422


def test_update_parent_syncs_user_identity(world: World, db_session: Session) -> None:
    parent = world.parent(mobile="+919900000001").json()
    path = f"/api/v1/parents/{parent['id']}"
    response = world.client.patch(
        path,
        headers=world.admin_a,
        json={"mobile": "+919900000002", "last_name": "Rao", "email": None},
    )
    assert response.status_code == 200
    assert response.json()["mobile"] == "+919900000002"
    user = db_session.execute(
        text(
            "SELECT u.phone, u.full_name FROM parents p JOIN users u ON u.id = p.user_id WHERE "
            "p.id = :p"
        ),
        {"p": parent["id"]},
    ).one()
    assert tuple(user) == ("+919900000002", "Par Rao")
    deactivated = world.client.patch(path, headers=world.admin_a, json={"is_active": False})
    assert deactivated.json()["is_active"] is False
    assert _actions(db_session, "parent") == [
        "parent.created",
        "parent.updated",
        "parent.deactivated",
    ]
    event = db_session.scalars(select(AuditLog).where(AuditLog.action == "parent.updated")).one()
    assert "+919900000002" not in str(event.details)  # mobile audited by name only


def test_parent_update_conflicting_mobile(world: World) -> None:
    world.parent(mobile="+919900000001")
    other = world.parent(mobile="+919900000002").json()
    response = world.client.patch(
        f"/api/v1/parents/{other['id']}", headers=world.admin_a, json={"mobile": "+919900000001"}
    )
    assert response.status_code == 409


def test_parent_list_for_staff(world: World, auth_headers: HeadersFactory) -> None:
    world.parent()
    world.parent()
    world.parent(world.admin_b)
    assert world.client.get("/api/v1/parents", headers=world.admin_a).json()["total"] == 2
    mgmt = auth_headers(R.MANAGEMENT)
    assert world.client.get("/api/v1/parents", headers=mgmt).json()["total"] == 2  # user.read
    assert world.parent(mgmt).status_code == 403


# ============================================================== student-parent links
def test_link_multiple_parents_and_children(world: World, db_session: Session) -> None:
    s1, s2 = world.student("S1").json(), world.student("S2").json()
    mum, dad = world.parent().json(), world.parent().json()
    assert world.link(s1["id"], mum["id"], is_primary=True).status_code == 201
    assert world.link(s1["id"], dad["id"], relationship_type="father").status_code == 201
    assert world.link(s2["id"], mum["id"]).status_code == 201

    parents = world.client.get(f"/api/v1/students/{s1['id']}/parents", headers=world.admin_a).json()
    assert [(p["parent"]["id"], p["is_primary"]) for p in parents] == [
        (mum["id"], True),
        (dad["id"], False),
    ]
    children = world.client.get(
        f"/api/v1/parents/{mum['id']}/children", headers=world.admin_a
    ).json()
    assert {c["student"]["id"] for c in children} == {s1["id"], s2["id"]}
    assert _actions(db_session, "student_parent") == ["student_parent.created"] * 3


def test_duplicate_link_rejected(world: World) -> None:
    student, parent = world.student("S1").json(), world.parent().json()
    world.link(student["id"], parent["id"])
    assert world.link(student["id"], parent["id"], relationship_type="guardian").status_code == 409


def test_only_one_primary_contact(world: World) -> None:
    student = world.student("S1").json()
    world.link(student["id"], world.parent().json()["id"], is_primary=True)
    second = world.link(student["id"], world.parent().json()["id"], is_primary=True)
    assert second.status_code == 409


def test_cross_school_parent_cannot_be_linked(world: World) -> None:
    student = world.student("S1").json()
    foreign = world.parent(world.admin_b).json()
    unknown = world.link(student["id"], "00000000-0000-4000-8000-000000000999")
    response = world.link(student["id"], foreign["id"])
    assert response.status_code == unknown.status_code == 422
    assert (
        response.json()["error"]["message"]
        == unknown.json()["error"]["message"]
        == "Parent not found."
    )
    # Nor can school A's admin link to school B's student.
    b_student = world.student("SB", world.admin_b).json()
    a_parent = world.parent().json()
    assert world.link(b_student["id"], a_parent["id"]).status_code == 404


def test_link_deactivation_preserves_history(world: World, db_session: Session) -> None:
    student, parent = world.student("S1").json(), world.parent().json()
    world.link(student["id"], parent["id"])
    path = f"/api/v1/students/{student['id']}/parents/{parent['id']}"
    response = world.client.patch(path, headers=world.admin_a, json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    # Still listed for staff (history), and relinking says to reactivate.
    listing = world.client.get(
        f"/api/v1/students/{student['id']}/parents", headers=world.admin_a
    ).json()
    assert [link["is_active"] for link in listing] == [False]
    relink = world.link(student["id"], parent["id"])
    assert relink.status_code == 409
    assert "reactivate" in relink.json()["error"]["message"]
    assert (
        world.client.patch(path, headers=world.admin_a, json={"is_active": True}).status_code == 200
    )
    assert _actions(db_session, "student_parent") == [
        "student_parent.created",
        "student_parent.deactivated",
        "student_parent.reactivated",
    ]


def test_inactive_parent_cannot_be_linked_or_relinked(world: World) -> None:
    student, parent = world.student("S1").json(), world.parent().json()
    world.link(student["id"], parent["id"])
    path = f"/api/v1/students/{student['id']}/parents/{parent['id']}"
    world.client.patch(path, headers=world.admin_a, json={"is_active": False})
    world.client.patch(
        f"/api/v1/parents/{parent['id']}", headers=world.admin_a, json={"is_active": False}
    )
    assert (
        world.client.patch(path, headers=world.admin_a, json={"is_active": True}).status_code == 422
    )
    other = world.student("S2").json()
    assert world.link(other["id"], parent["id"]).status_code == 422


def test_unknown_link_is_404(world: World) -> None:
    student, parent = world.student("S1").json(), world.parent().json()
    response = world.client.patch(
        f"/api/v1/students/{student['id']}/parents/{parent['id']}",
        headers=world.admin_a,
        json={"is_primary": True},
    )
    assert response.status_code == 404


def test_super_admin_manages_people_in_selected_school(
    world: World, auth_headers: HeadersFactory
) -> None:
    root_b = auth_headers(R.SUPER_ADMIN, SCHOOL_B)
    created = world.student("ROOT-1", root_b)
    assert created.status_code == 201
    assert created.json()["school_id"] == str(SCHOOL_B)
    assert world.client.get("/api/v1/students", headers=world.admin_b).json()["total"] == 1
    assert world.client.get("/api/v1/students", headers=world.admin_a).json()["total"] == 0
