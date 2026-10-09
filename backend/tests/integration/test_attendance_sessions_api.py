"""Attendance sessions: create/read/update, uniqueness, validation, filters, time zone,
school isolation, RBAC and audit."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import ATTENDANCE, SESSION_DATE, AttendanceWorld

pytestmark = pytest.mark.db
R = RoleName


def _error(response: object) -> dict[str, object]:
    body: dict[str, object] = response.json()["error"]  # type: ignore[attr-defined]
    return body


# ========================================================================== create
def test_create_subject_session(aworld: AttendanceWorld, db_session: Session) -> None:
    response = aworld.session(start_time="09:00", end_time="09:45")
    assert response.status_code == 201, response.text
    body = response.json()
    ids = aworld.w.ids
    assert (body["section_id"], body["subject_id"]) == (ids["section_a"], ids["math_a"])
    assert body["school_id"] == str(SCHOOL_A)
    assert (body["session_type"], body["period"], body["status"], body["is_active"]) == (
        "subject",
        1,
        "active",
        True,
    )
    assert (body["session_date"], body["start_time"], body["end_time"]) == (
        SESSION_DATE,
        "09:00:00",
        "09:45:00",
    )
    assert aworld.get(f"/sessions/{body['id']}").json() == body
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "attendance_session.created")
    ).one()
    assert event.resource_id == body["id"]
    assert event.details["section_id"] == ids["section_a"]
    assert event.details["subject_id"] == ids["math_a"]
    assert event.details["session_date"] == SESSION_DATE


def test_create_daily_session(aworld: AttendanceWorld) -> None:
    response = aworld.session(subject=None)
    assert response.status_code == 201, response.text
    assert (response.json()["session_type"], response.json()["subject_id"]) == ("daily", None)


def test_duplicate_sessions(aworld: AttendanceWorld) -> None:
    """Duplicates are per kind: (year, section, date, period) for daily roll calls and
    (year, section, subject, date, period) for subject roll calls."""
    assert aworld.session().status_code == 201
    duplicate = aworld.session()
    assert duplicate.status_code == 409
    assert _error(duplicate)["details"][0]["loc"] == ["body", "session_date"]  # type: ignore[index]
    assert aworld.session(period=2).status_code == 201  # double period
    assert aworld.session(subject="sci_a").status_code == 201
    assert aworld.session(sec="section_a_b").status_code == 201
    assert aworld.session(date="2026-07-11").status_code == 201
    assert aworld.session(subject=None).status_code == 201  # daily alongside subject
    assert aworld.session(subject=None).status_code == 409
    assert aworld.session(subject=None, period=2).status_code == 201  # afternoon roll call


def test_inactive_session_is_history_and_reactivation_respects_uniqueness(
    aworld: AttendanceWorld,
) -> None:
    first = aworld.session_id()
    assert aworld.patch(f"/sessions/{first}", {"status": "inactive"}).status_code == 200
    second = aworld.session_id()  # the slot is free again
    assert second != first
    conflict = aworld.patch(f"/sessions/{first}", {"status": "active"})
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    ("override", "field", "message"),
    [
        ({"date": "2026-05-31"}, "session_date", "Must fall within the academic year."),
        ({"cls": "class2_a"}, "section_id", "Section does not belong to the class."),
        ({"year": "year_b"}, "academic_year_id", "Academic year not found."),
        ({"cls": "class_b", "sec": "section_b"}, "class_id", "Class not found."),
        ({"sec": "section_b"}, "section_id", "Section not found."),
        ({"subject": "math_b"}, "subject_id", "Subject not found."),
    ],
)
def test_create_validation(
    aworld: AttendanceWorld, override: dict[str, str], field: str, message: str
) -> None:
    response = aworld.session(**override)  # type: ignore[arg-type]
    assert response.status_code == 422, response.text
    detail = _error(response)["details"][0]  # type: ignore[index]
    assert (detail["loc"], detail["message"]) == (["body", field], message)


def test_future_date_rejected(aworld: AttendanceWorld) -> None:
    tomorrow = datetime.now(ZoneInfo("UTC")).date() + timedelta(days=2)
    response = aworld.session(date=str(tomorrow))
    assert response.status_code == 422
    assert _error(response)["details"][0]["loc"] == ["body", "session_date"]  # type: ignore[index]


def test_session_date_uses_the_school_time_zone(
    aworld: AttendanceWorld, db_session: Session
) -> None:
    """Pacific/Kiritimati (UTC+14) and Pacific/Pago_Pago (UTC-11) are 25 hours apart, so
    their calendar dates always differ: "today" must be the school's, not the server's."""
    ahead, behind = "Pacific/Kiritimati", "Pacific/Pago_Pago"
    today_ahead = datetime.now(ZoneInfo(ahead)).date()
    assert today_ahead > datetime.now(ZoneInfo(behind)).date()

    def set_zone(name: str) -> None:
        db_session.execute(
            text("UPDATE schools SET timezone = :tz WHERE id = :s"), {"tz": name, "s": SCHOOL_A}
        )

    set_zone(ahead)
    assert aworld.session(date=str(today_ahead)).status_code == 201
    set_zone(behind)
    rejected = aworld.session(date=str(today_ahead), period=2)
    assert rejected.status_code == 422
    assert "future" in _error(rejected)["details"][0]["message"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("path", "key", "field"),
    [
        ("/api/v1/subjects", "math_a", "subject_id"),
        ("/api/v1/sections", "section_a", "section_id"),
        ("/api/v1/academic-years", "year_a", "academic_year_id"),
    ],
)
def test_inactive_structure_rejected(
    aworld: AttendanceWorld, path: str, key: str, field: str
) -> None:
    deactivate = aworld.client.patch(
        f"{path}/{aworld.w.ids[key]}", headers=aworld.admin, json={"is_active": False}
    )
    assert deactivate.status_code == 200, deactivate.text
    response = aworld.session()
    assert response.status_code == 422
    assert _error(response)["details"][0]["loc"] == ["body", field]  # type: ignore[index]


def test_inactive_class_rejected(aworld: AttendanceWorld) -> None:
    ids = aworld.w.ids
    for section in ("section_a", "section_a_b"):
        aworld.client.patch(
            f"/api/v1/sections/{ids[section]}", headers=aworld.admin, json={"is_active": False}
        )
    deactivate = aworld.client.patch(
        f"/api/v1/classes/{ids['class_a']}", headers=aworld.admin, json={"is_active": False}
    )
    assert deactivate.status_code == 200, deactivate.text
    response = aworld.session()
    assert response.status_code == 422
    assert _error(response)["details"][0]["loc"] == ["body", "class_id"]  # type: ignore[index]


@pytest.mark.parametrize(
    "extra",
    [
        {"period": 0},
        {"period": 21},
        {"start_time": "10:00", "end_time": "09:00"},
        {"session_type": "daily"},  # derived from subject_id, never accepted
        {"school_id": str(SCHOOL_B)},
    ],
)
def test_schema_validation(aworld: AttendanceWorld, extra: dict[str, object]) -> None:
    assert aworld.session(**extra).status_code == 422


# =========================================================================== update
def test_update_times_and_status_with_audit(aworld: AttendanceWorld, db_session: Session) -> None:
    sid = aworld.session_id()
    updated = aworld.patch(f"/sessions/{sid}", {"start_time": "10:00", "end_time": "10:40"})
    assert updated.status_code == 200
    assert updated.json()["start_time"] == "10:00:00"
    assert aworld.patch(f"/sessions/{sid}", {"end_time": "09:00"}).status_code == 422
    assert aworld.patch(f"/sessions/{sid}", {"status": "inactive"}).json()["is_active"] is False
    assert aworld.patch(f"/sessions/{sid}", {"status": "active"}).json()["is_active"] is True
    assert aworld.patch(f"/sessions/{sid}", {"status": "active"}).status_code == 200  # no-op
    actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.resource_id == sid).order_by(AuditLog.created_at)
    ).all()
    assert actions == [
        "attendance_session.created",
        "attendance_session.updated",
        "attendance_session.deactivated",
        "attendance_session.reactivated",
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"section_id": str(uuid.uuid4())},
        {"session_date": "2026-07-11"},
        {"subject_id": None},
        {"status": "deleted"},
    ],
)
def test_update_rejects_immutable_or_invalid(
    aworld: AttendanceWorld, body: dict[str, object]
) -> None:
    sid = aworld.session_id()
    assert aworld.patch(f"/sessions/{sid}", body).status_code == 422


# ===================================================================== reads/filters
def test_filters_and_pagination(aworld: AttendanceWorld) -> None:
    math = aworld.session_id()
    sci = aworld.session_id(subject="sci_a")
    daily = aworld.session_id(subject=None)
    other_day = aworld.session_id(date="2026-07-20")
    other_section = aworld.session_id(sec="section_a_b")
    inactive = aworld.session_id(date="2026-07-21")
    aworld.patch(f"/sessions/{inactive}", {"status": "inactive"})
    ids = aworld.w.ids
    everything = {math, sci, daily, other_day, other_section, inactive}
    assert aworld.ids("/sessions", page_size=100) == everything
    assert aworld.ids("/sessions", subject_id=ids["sci_a"]) == {sci}
    assert aworld.ids("/sessions", session_type="daily") == {daily}
    assert aworld.ids("/sessions", section_id=ids["section_a_b"]) == {other_section}
    assert aworld.ids("/sessions", session_date=SESSION_DATE) == {math, sci, daily, other_section}
    assert aworld.ids("/sessions", from_date="2026-07-15") == {other_day, inactive}
    assert aworld.ids("/sessions", to_date="2026-07-15", class_id=ids["class_a"]) == {
        math,
        sci,
        daily,
        other_section,
    }
    assert aworld.ids("/sessions", status="inactive") == {inactive}
    assert aworld.ids("/sessions", academic_year_id=ids["year_a_prev"]) == set()
    page = aworld.get("/sessions", page=2, page_size=4).json()
    assert (page["total"], page["pages"], len(page["items"])) == (6, 2, 2)
    assert aworld.get(f"/sessions/{uuid.uuid4()}").status_code == 404


# ======================================================================= isolation
def test_school_isolation(aworld: AttendanceWorld) -> None:
    sid = aworld.session_id()
    admin_b = aworld.w.admin_b
    assert aworld.get(f"/sessions/{sid}", admin_b).status_code == 404
    assert aworld.ids("/sessions", admin_b) == set()
    assert aworld.patch(f"/sessions/{sid}", {"status": "inactive"}, admin_b).status_code == 404
    assert (
        aworld.get("/sessions", {**aworld.admin, "X-School-ID": str(SCHOOL_B)}).status_code == 404
    )
    b_session = aworld.session_id(
        admin_b, year="year_b", cls="class_b", sec="section_b", subject="math_b"
    )
    assert aworld.get(f"/sessions/{b_session}").status_code == 404


def test_super_admin_selects_school(aworld: AttendanceWorld, auth_headers: HeadersFactory) -> None:
    root = auth_headers(R.SUPER_ADMIN, SCHOOL_A)
    sid = aworld.session_id(root)
    assert aworld.get(f"/sessions/{sid}", root).status_code == 200
    root_b = auth_headers(R.SUPER_ADMIN, SCHOOL_B)
    assert aworld.get(f"/sessions/{sid}", root_b).status_code == 404
    no_school = auth_headers(R.SUPER_ADMIN, None)
    assert aworld.get("/sessions", no_school).status_code == 400


def test_management_reads_but_cannot_write(
    aworld: AttendanceWorld, auth_headers: HeadersFactory
) -> None:
    sid = aworld.session_id()
    management = auth_headers(R.MANAGEMENT)
    assert aworld.ids("/sessions", management) == {sid}
    assert aworld.session(management).status_code == 403
    assert aworld.patch(f"/sessions/{sid}", {"status": "inactive"}, management).status_code == 403


def test_parents_and_students_never_see_or_create_sessions(aworld: AttendanceWorld) -> None:
    sid = aworld.session_id()
    student = aworld.w.student_headers("A1")
    parent = aworld.parent_of("a1")
    for headers in (student, parent):
        assert aworld.ids("/sessions", headers) == set()
        assert aworld.get(f"/sessions/{sid}", headers).status_code == 404
        assert aworld.session(headers).status_code == 403
        assert aworld.patch(f"/sessions/{sid}", {"status": "inactive"}, headers).status_code == 403


def test_unauthenticated_is_rejected(aworld: AttendanceWorld) -> None:
    assert aworld.client.get(f"{ATTENDANCE}/sessions").status_code == 401
