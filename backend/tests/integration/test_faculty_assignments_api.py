"""Faculty assignments: CRUD, lifecycle/history, duplicates, filters, same-school and
class/section validation, RBAC and audit."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import World

pytestmark = pytest.mark.db
R = RoleName
URL = "/api/v1/faculty-assignments"


@pytest.fixture
def teacher(fworld: World) -> dict[str, Any]:
    response = fworld.faculty(email="maths.teacher@school.test")
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


def _patch(world: World, assignment_id: str, body: dict[str, Any], headers: Any = None) -> Any:
    return world.client.patch(f"{URL}/{assignment_id}", headers=headers or world.admin_a, json=body)


# ========================================================================== create
def test_create_and_read(fworld: World, teacher: dict[str, Any], db_session: Session) -> None:
    response = fworld.assign(teacher["id"], effective_from="2026-06-01", effective_to="2027-03-31")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["faculty_id"] == teacher["id"]
    assert body["school_id"] == str(SCHOOL_A)
    assert (body["class_id"], body["section_id"], body["subject_id"]) == (
        fworld.ids["class_a"],
        fworld.ids["section_a"],
        fworld.ids["math_a"],
    )
    assert (body["status"], body["is_active"]) == ("active", True)
    got = fworld.client.get(f"{URL}/{body['id']}", headers=fworld.admin_a)
    assert got.json() == body
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "faculty_assignment.created")
    ).one()
    assert event.resource_id == body["id"]
    assert event.details["section_id"] == fworld.ids["section_a"]
    assert event.details["subject_id"] == fworld.ids["math_a"]
    assert event.details["school_id"] == str(SCHOOL_A)


def test_multiple_assignments_per_faculty(fworld: World, teacher: dict[str, Any]) -> None:
    for sec, subject in (
        ("section_a", "math_a"),
        ("section_a_b", "math_a"),
        ("section_a", "sci_a"),
    ):
        assert fworld.assign(teacher["id"], sec=sec, subject=subject).status_code == 201
    assert fworld.assign(teacher["id"], cls="class2_a", sec="section2_a").status_code == 201
    listing = fworld.client.get(URL, headers=fworld.admin_a, params={"faculty_id": teacher["id"]})
    assert listing.json()["total"] == 4


def test_two_faculty_may_share_a_placement(fworld: World, teacher: dict[str, Any]) -> None:
    other = fworld.faculty().json()
    assert fworld.assign(teacher["id"]).status_code == 201
    assert fworld.assign(other["id"]).status_code == 201  # co-teaching is allowed


def test_duplicate_active_assignment_rejected(fworld: World, teacher: dict[str, Any]) -> None:
    assert fworld.assign(teacher["id"]).status_code == 201
    duplicate = fworld.assign(teacher["id"])
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["details"][0]["loc"] == ["body", "subject_id"]


@pytest.mark.parametrize(
    ("override", "field", "message"),
    [
        ({"year": "year_b"}, "academic_year_id", "Academic year not found."),
        ({"cls": "class_b", "sec": "section_b"}, "class_id", "Class not found."),
        ({"sec": "section_b"}, "section_id", "Section not found."),
        ({"subject": "math_b"}, "subject_id", "Subject not found."),
    ],
)
def test_cross_school_references_rejected(
    fworld: World, teacher: dict[str, Any], override: dict[str, str], field: str, message: str
) -> None:
    """School A's admin cannot build an assignment out of School B's records, and gets the
    same answer as for an unknown id (no probing of another school's records)."""
    response = fworld.assign(teacher["id"], **override)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["details"][0]["loc"] == ["body", field]
    assert error["details"][0]["message"] == message
    fworld.ids["unknown"] = str(uuid.uuid4())
    key = {"academic_year_id": "year", "class_id": "cls", "section_id": "sec"}.get(field, "subject")
    unknown = fworld.assign(teacher["id"], **{**override, key: "unknown"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"] == error["details"]


def test_cross_school_faculty_rejected(fworld: World) -> None:
    b_teacher = fworld.faculty(fworld.admin_b).json()
    response = fworld.assign(b_teacher["id"])
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Faculty member not found."
    assert fworld.assign(str(uuid.uuid4())).status_code == 422


def test_section_must_belong_to_class(fworld: World, teacher: dict[str, Any]) -> None:
    # Class 2 with class 1's section.
    response = fworld.assign(teacher["id"], cls="class2_a", sec="section_a")
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Section does not belong to the class."


@pytest.mark.parametrize(
    ("path", "key", "field"),
    [
        ("/api/v1/subjects", "math_a", "subject_id"),
        ("/api/v1/sections", "section_a", "section_id"),
        ("/api/v1/academic-years", "year_a", "academic_year_id"),
    ],
)
def test_inactive_structure_rejected(
    fworld: World, teacher: dict[str, Any], path: str, key: str, field: str
) -> None:
    deactivate = fworld.client.patch(
        f"{path}/{fworld.ids[key]}", headers=fworld.admin_a, json={"is_active": False}
    )
    assert deactivate.status_code == 200, deactivate.text
    response = fworld.assign(teacher["id"])
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", field]


def test_inactive_class_rejected(fworld: World, teacher: dict[str, Any]) -> None:
    for sec in ("section_a", "section_a_b"):
        fworld.client.patch(
            f"/api/v1/sections/{fworld.ids[sec]}", headers=fworld.admin_a, json={"is_active": False}
        )
    fworld.client.patch(
        f"/api/v1/classes/{fworld.ids['class_a']}",
        headers=fworld.admin_a,
        json={"is_active": False},
    )
    response = fworld.assign(teacher["id"])
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "class_id"]


def test_inactive_faculty_rejected(fworld: World, teacher: dict[str, Any]) -> None:
    fworld.client.patch(
        f"/api/v1/faculty/{teacher['id']}", headers=fworld.admin_a, json={"status": "inactive"}
    )
    response = fworld.assign(teacher["id"])
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Faculty member is inactive."


@pytest.mark.parametrize(
    "dates",
    [
        {"effective_from": "2027-01-01", "effective_to": "2026-12-31"},
        {"effective_from": "2027-01-01", "effective_to": "2027-01-01"},
        {"effective_from": "2025-01-01"},  # before the academic year
        {"effective_to": "2027-06-01"},  # after the academic year
        {"effective_from": "not-a-date"},
    ],
)
def test_effective_date_validation(
    fworld: World, teacher: dict[str, Any], dates: dict[str, str]
) -> None:
    assert fworld.assign(teacher["id"], **dates).status_code == 422


@pytest.mark.parametrize("extra", [{"status": "inactive"}, {"school_id": str(SCHOOL_B)}, {"x": 1}])
def test_create_rejects_unknown_fields(
    fworld: World, teacher: dict[str, Any], extra: dict[str, Any]
) -> None:
    assert fworld.assign(teacher["id"], **extra).status_code == 422


# ========================================================================== update
def test_lifecycle_keeps_history(
    fworld: World, teacher: dict[str, Any], db_session: Session
) -> None:
    first = fworld.assign(teacher["id"]).json()
    ended = _patch(fworld, first["id"], {"status": "inactive", "effective_to": "2026-12-31"})
    assert ended.status_code == 200, ended.text
    assert (ended.json()["status"], ended.json()["is_active"]) == ("inactive", False)
    # A new assignment for the same placement can start; the old row stays as history.
    second = fworld.assign(teacher["id"], effective_from="2027-01-01")
    assert second.status_code == 201
    # Reactivating the old row would duplicate the active one: refused by the database.
    assert _patch(fworld, first["id"], {"status": "active"}).status_code == 409
    _patch(fworld, second.json()["id"], {"status": "inactive"})
    again = _patch(fworld, first["id"], {"status": "active"})
    assert again.status_code == 200
    assert db_session.scalar(text("SELECT count(*) FROM faculty_assignments")) == 2
    assert list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.action.like("faculty_assignment.%"))
            .order_by(AuditLog.created_at)
        )
    ) == [
        "faculty_assignment.created",
        "faculty_assignment.deactivated",
        "faculty_assignment.created",
        "faculty_assignment.deactivated",
        "faculty_assignment.reactivated",
    ]


def test_update_dates_audited_with_scope(
    fworld: World, teacher: dict[str, Any], db_session: Session
) -> None:
    item = fworld.assign(teacher["id"]).json()
    response = _patch(fworld, item["id"], {"effective_from": "2026-07-01"})
    assert response.json()["effective_from"] == "2026-07-01"
    cleared = _patch(fworld, item["id"], {"effective_from": None})
    assert cleared.json()["effective_from"] is None
    events = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "faculty_assignment.updated")
    ).all()
    assert len(events) == 2
    assert events[0].details["section_id"] == fworld.ids["section_a"]
    assert {"field": "effective_from", "from": None, "to": "2026-07-01"} in events[0].details[
        "changes"
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": None},
        {"section_id": str(uuid.uuid4())},  # placement is immutable
        {"faculty_id": str(uuid.uuid4())},
        {"subject_id": str(uuid.uuid4())},
        {"status": "deleted"},
    ],
)
def test_update_validation(fworld: World, teacher: dict[str, Any], body: dict[str, Any]) -> None:
    item = fworld.assign(teacher["id"]).json()
    assert _patch(fworld, item["id"], body).status_code == 422


def test_update_date_rules(fworld: World, teacher: dict[str, Any]) -> None:
    item = fworld.assign(teacher["id"], effective_from="2026-09-01").json()
    assert _patch(fworld, item["id"], {"effective_to": "2026-08-01"}).status_code == 422
    assert _patch(fworld, item["id"], {"effective_to": "2028-01-01"}).status_code == 422
    assert _patch(fworld, item["id"], {"effective_to": "2026-10-01"}).status_code == 200


def test_reactivation_revalidates(fworld: World, teacher: dict[str, Any]) -> None:
    item = fworld.assign(teacher["id"]).json()
    _patch(fworld, item["id"], {"status": "inactive"})
    fworld.client.patch(
        f"/api/v1/subjects/{fworld.ids['math_a']}",
        headers=fworld.admin_a,
        json={"is_active": False},
    )
    assert _patch(fworld, item["id"], {"status": "active"}).status_code == 422
    fworld.client.patch(
        f"/api/v1/subjects/{fworld.ids['math_a']}", headers=fworld.admin_a, json={"is_active": True}
    )
    fworld.client.patch(
        f"/api/v1/faculty/{teacher['id']}", headers=fworld.admin_a, json={"status": "inactive"}
    )
    response = _patch(fworld, item["id"], {"status": "active"})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Reactivate the faculty member first."


def test_noop_update_not_audited(
    fworld: World, teacher: dict[str, Any], db_session: Session
) -> None:
    item = fworld.assign(teacher["id"]).json()
    assert _patch(fworld, item["id"], {"status": "active"}).status_code == 200
    assert (
        db_session.scalar(
            select(AuditLog.id).where(AuditLog.action == "faculty_assignment.updated")
        )
        is None
    )


# ===================================================================== list/filters
def test_filters_and_pagination(fworld: World, teacher: dict[str, Any]) -> None:
    other = fworld.faculty().json()
    a1 = fworld.assign(teacher["id"]).json()
    a2 = fworld.assign(teacher["id"], sec="section_a_b", subject="sci_a").json()
    a3 = fworld.assign(other["id"], cls="class2_a", sec="section2_a").json()
    _patch(fworld, a2["id"], {"status": "inactive"})

    def ids(**params: str) -> set[str]:
        page = fworld.client.get(URL, headers=fworld.admin_a, params=params).json()
        return {item["id"] for item in page["items"]}

    assert ids() == {a1["id"], a2["id"], a3["id"]}
    assert ids(faculty_id=teacher["id"]) == {a1["id"], a2["id"]}
    assert ids(section_id=fworld.ids["section_a_b"]) == {a2["id"]}
    assert ids(class_id=fworld.ids["class2_a"]) == {a3["id"]}
    assert ids(subject_id=fworld.ids["math_a"]) == {a1["id"], a3["id"]}
    assert ids(academic_year_id=fworld.ids["year_a"]) == {a1["id"], a2["id"], a3["id"]}
    assert ids(academic_year_id=fworld.ids["year_a_prev"]) == set()
    assert ids(status="inactive") == {a2["id"]}
    assert ids(status="active", faculty_id=teacher["id"]) == {a1["id"]}
    page = fworld.client.get(URL, headers=fworld.admin_a, params={"page_size": 1}).json()
    assert (page["total"], len(page["items"])) == (3, 1)
    bad = fworld.client.get(URL, headers=fworld.admin_a, params={"status": "deleted"})
    assert bad.status_code == 422


def test_school_isolation(fworld: World, teacher: dict[str, Any]) -> None:
    b_teacher = fworld.faculty(fworld.admin_b).json()
    b_item = fworld.assign(
        b_teacher["id"],
        fworld.admin_b,
        year="year_b",
        cls="class_b",
        sec="section_b",
        subject="math_b",
    ).json()
    fworld.assign(teacher["id"])
    assert fworld.client.get(URL, headers=fworld.admin_a).json()["total"] == 1
    assert fworld.client.get(f"{URL}/{b_item['id']}", headers=fworld.admin_a).status_code == 404
    assert _patch(fworld, b_item["id"], {"status": "inactive"}).status_code == 404
    filtered = fworld.client.get(
        URL, headers=fworld.admin_a, params={"faculty_id": b_teacher["id"]}
    )
    assert filtered.json()["total"] == 0


def test_super_admin_and_management(
    fworld: World, teacher: dict[str, Any], auth_headers: HeadersFactory
) -> None:
    root = auth_headers(R.SUPER_ADMIN, SCHOOL_A)
    item = fworld.assign(teacher["id"], root)
    assert item.status_code == 201
    mgmt = auth_headers(R.MANAGEMENT)
    assert fworld.client.get(URL, headers=mgmt).json()["total"] == 1
    assert fworld.client.get(f"{URL}/{item.json()['id']}", headers=mgmt).status_code == 200
    assert fworld.assign(teacher["id"], mgmt, subject="sci_a").status_code == 403
    assert _patch(fworld, item.json()["id"], {"status": "inactive"}, mgmt).status_code == 403
    for role in (R.PARENT, R.STUDENT):
        headers = auth_headers(role)
        assert fworld.client.get(URL, headers=headers).status_code == 403
        assert fworld.client.get(f"{URL}/{item.json()['id']}", headers=headers).status_code == 403
