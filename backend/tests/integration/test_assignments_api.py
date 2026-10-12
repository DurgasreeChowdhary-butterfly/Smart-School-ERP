"""Assignment management over HTTP: validation, lifecycle and faculty scope."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_B, HeadersFactory
from tests.integration.conftest import HomeworkWorld, iso_in, today_iso

pytestmark = pytest.mark.db


def _audits(db_session: Session, action: str) -> list[AuditLog]:
    return list(db_session.scalars(select(AuditLog).where(AuditLog.action == action)))


# ===================================================================== creation
def test_create_by_admin_and_by_faculty_in_scope(
    hworld: HomeworkWorld, db_session: Session
) -> None:
    created = hworld.create(max_marks=20, allow_late=True)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["status"], body["max_marks"], body["allow_late"], body["allow_resubmission"]) == (
        "draft",
        20,
        True,
        True,
    )
    assert body["published_at"] is None
    faculty = hworld.create(hworld.a.f)
    assert faculty.status_code == 201, faculty.text
    audit = _audits(db_session, "assignment.created")
    assert len(audit) == 2
    assert audit[0].details["graded"] is True
    assert "Solve questions" not in str([a.details for a in audit])


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"due_at": "2026-10-20T10:00:00"}, "due_at"),  # naive date-time: no offset
        ({"assigned_on": "2025-05-01"}, "assigned_on"),
        ({"assigned_on": "2027-06-01"}, "assigned_on"),
        ({"max_marks": 0}, "max_marks"),
        ({"max_marks": 1000.5}, "max_marks"),
        ({"max_marks": 10.555}, "max_marks"),
        ({"title": "   "}, "title"),
        ({"instructions": ""}, "instructions"),
        ({"instructions": "bad\x00text"}, "instructions"),
        ({"school_id": "00000000-0000-4000-8000-00000000000b"}, "school_id"),
    ],
)
def test_validation(hworld: HomeworkWorld, override: dict[str, Any], field: str) -> None:
    response = hworld.create(**override)
    assert response.status_code == 422, response.text


def test_due_date_rules_use_the_school_time_zone(hworld: HomeworkWorld) -> None:
    # Due the day before it is set (school-local): refused.
    before = hworld.create(assigned_on="2026-10-20", due_at="2026-10-19T12:00:00+00:00")
    assert before.status_code == 422
    assert before.json()["error"]["details"][0]["loc"] == ["body", "due_at"]
    # 2026-10-20T01:00+05:30 is still 19 October in UTC (the test school's time zone).
    shifted = hworld.create(assigned_on="2026-10-20", due_at="2026-10-20T01:00:00+05:30")
    assert shifted.status_code == 422
    same_day = hworld.create(assigned_on="2026-10-20", due_at="2026-10-20T23:00:00+00:00")
    assert same_day.status_code == 201
    beyond = hworld.create(due_at="2027-06-02T10:00:00+00:00")
    assert beyond.status_code == 422


def test_placement_must_be_in_school_and_consistent(hworld: HomeworkWorld) -> None:
    for override, message in (
        ({"year": "year_b"}, "Academic year not found."),
        ({"cls": "class_b"}, "Class not found."),
        ({"sec": "section_b"}, "Section not found."),
        ({"subject": "math_b"}, "Subject not found."),
        ({"sec": "section2_a"}, "Section does not belong to the class."),
    ):
        response = hworld.create(**override)
        assert response.status_code == 422, override
        assert response.json()["error"]["message"] == message


@pytest.mark.parametrize(
    "override",
    [
        {"subject": "sci_a"},
        {"sec": "section_a_b"},
        {"cls": "class2_a", "sec": "section2_a"},
        {"year": "year_a_prev", "assigned_on": "2026-01-10", "due_at": "2026-01-20T10:00:00+00:00"},
        {"year": "year_b", "cls": "class_b", "sec": "section_b", "subject": "math_b"},
    ],
)
def test_faculty_cannot_create_outside_their_assignment(
    hworld: HomeworkWorld, override: dict[str, Any]
) -> None:
    response = hworld.create(hworld.a.f, **override)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == (
        "You are not assigned to this section and subject on that date."
    )


def test_who_creates(hworld: HomeworkWorld, auth_headers: HeadersFactory) -> None:
    assert hworld.create(auth_headers(RoleName.MANAGEMENT)).status_code == 403
    assert hworld.create(hworld.parent_of("a1")).status_code == 403
    assert hworld.create(hworld.student("A1")).status_code == 403
    assert hworld.create(auth_headers(RoleName.SUPER_ADMIN)).status_code == 201
    assert hworld.create(auth_headers(RoleName.SCHOOL_ADMIN, SCHOOL_B)).status_code == 422


def test_assignment_must_be_effective_on_the_assigned_date(hworld: HomeworkWorld) -> None:
    w = hworld.a.w
    late = w.faculty(email="hw-late@school.test").json()["id"]
    assert w.assign(late, effective_from=today_iso()).status_code == 201
    headers = w.faculty_headers("hw-late@school.test")
    assert hworld.create(headers, assigned_on="2026-09-01").status_code == 403
    assert hworld.create(headers).status_code == 201


# ==================================================================== lifecycle
def test_lifecycle(hworld: HomeworkWorld, db_session: Session) -> None:
    assignment_id = hworld.create_id(hworld.a.f)
    assert hworld.action(assignment_id, "close", hworld.a.f).status_code == 409
    published = hworld.action(assignment_id, "publish", hworld.a.f)
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["published_by_user_id"]
    assert hworld.action(assignment_id, "publish", hworld.a.f).status_code == 409
    closed = hworld.action(assignment_id, "close", hworld.a.f)
    assert closed.status_code == 200
    assert closed.json()["closed_at"]
    for verb in ("publish", "close"):
        assert hworld.action(assignment_id, verb).status_code == 409
    assert hworld.patch(assignment_id, {"title": "New"}).status_code == 409
    assert [len(_audits(db_session, f"assignment.{a}")) for a in ("published", "closed")] == [1, 1]


def test_publish_needs_a_future_due_time(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.create_id(due_at=iso_in(-0.01), assigned_on="2026-06-15")
    refused = hworld.action(assignment_id, "publish")
    assert refused.status_code == 422
    assert hworld.patch(assignment_id, {"due_at": iso_in(2)}).status_code == 200
    assert hworld.action(assignment_id, "publish").status_code == 200


def test_editing_rules_by_status(hworld: HomeworkWorld, db_session: Session) -> None:
    assignment_id = hworld.create_id(max_marks=10)
    draft = hworld.patch(
        assignment_id, {"max_marks": 25, "title": "Revised", "instructions": "New"}
    )
    assert draft.status_code == 200, draft.text
    assert (draft.json()["max_marks"], draft.json()["title"]) == (25, "Revised")
    assert hworld.action(assignment_id, "publish").status_code == 200
    for body in ({"max_marks": 30}, {"max_marks": None}, {"assigned_on": "2026-09-01"}):
        assert hworld.patch(assignment_id, body).status_code == 409, body
    allowed = hworld.patch(
        assignment_id, {"due_at": iso_in(10), "allow_late": True, "allow_resubmission": False}
    )
    assert allowed.status_code == 200, allowed.text
    for body in (
        {},
        {"section_id": hworld.ids["section_a_b"]},
        {"status": "draft"},
        {"title": None},
    ):
        assert hworld.patch(assignment_id, body).status_code == 422, body
    changes = _audits(db_session, "assignment.updated")[0].details["changes"]
    assert {"field": "instructions", "redacted": True} in changes


def test_faculty_manage_only_their_assignments(hworld: HomeworkWorld) -> None:
    a = hworld.a
    mine = hworld.create_id(a.f)
    staff_made = hworld.create_id(sec="section_a_b")
    assert hworld.patch(mine, {"title": "x"}, a.g).status_code == 404  # G cannot even see it
    assert hworld.action(mine, "publish", a.h).status_code == 404
    assert hworld.patch(staff_made, {"title": "x"}, a.h).status_code == 200
    assert hworld.patch(staff_made, {"title": "y"}, a.f).status_code == 404


# ======================================================================== reads
def test_list_filters_and_scope(hworld: HomeworkWorld, auth_headers: HeadersFactory) -> None:
    a = hworld.a
    math_a = hworld.create_id()
    sci_a = hworld.create_id(subject="sci_a")
    math_b = hworld.published(sec="section_a_b")
    assert {i["id"] for i in hworld.items("/assignments")} == {math_a, sci_a, math_b}
    assert {i["id"] for i in hworld.items("/assignments", subject_id=hworld.ids["sci_a"])} == {
        sci_a
    }
    assert {i["id"] for i in hworld.items("/assignments", status="published")} == {math_b}
    assert {i["id"] for i in hworld.items("/assignments", a.f)} == {math_a}
    assert {i["id"] for i in hworld.items("/assignments", a.g)} == {sci_a}
    assert {i["id"] for i in hworld.items("/assignments", a.h)} == {math_b}
    # Filters only narrow.
    assert hworld.items("/assignments", a.f, section_id=hworld.ids["section_a_b"]) == []
    assert hworld.items("/assignments", a.f, subject_id=hworld.ids["sci_a"]) == []
    management = auth_headers(RoleName.MANAGEMENT)
    assert len(hworld.items("/assignments", management)) == 3
    assert hworld.get(f"/assignments/{sci_a}", a.f).status_code == 404
    page = hworld.get("/assignments", page_size=2).json()
    assert (page["total"], len(page["items"])) == (3, 2)


def test_drafts_are_hidden_from_students_and_parents(hworld: HomeworkWorld) -> None:
    draft = hworld.create_id()
    published = hworld.published(title="Published")
    for headers in (hworld.student("A1"), hworld.parent_of("a1")):
        assert {i["id"] for i in hworld.items("/assignments", headers)} == {published}
        assert hworld.get(f"/assignments/{draft}", headers).status_code == 404
        assert hworld.get(f"/assignments/{published}", headers).status_code == 200
        assert hworld.items("/assignments", headers, status="draft") == []


def test_students_see_only_their_sections_assignments(hworld: HomeworkWorld) -> None:
    section_a = hworld.published()
    section_b = hworld.published(sec="section_a_b")
    other_class = hworld.published(cls="class2_a", sec="section2_a")
    _, _, ab = hworld.new_student("AB2", sec="section_a_b")
    assert {i["id"] for i in hworld.items("/assignments", hworld.student("A1"))} == {section_a}
    assert {i["id"] for i in hworld.items("/assignments", ab)} == {section_b}
    for assignment_id in (section_b, other_class):
        assert hworld.get(f"/assignments/{assignment_id}", hworld.student("A1")).status_code == 404


def test_roster_and_outstanding(hworld: HomeworkWorld, auth_headers: HeadersFactory) -> None:
    assignment_id = hworld.published(max_marks=10)
    hworld.submission_id(assignment_id, hworld.student("A1"))
    roster = {s["student_id"]: s for s in hworld.items(f"/assignments/{assignment_id}/students")}
    assert set(roster) == {hworld.sid("a1"), hworld.sid("a2")}
    assert roster[hworld.sid("a1")]["submission_status"] == "submitted"
    # Missing work is outstanding, never zero marks.
    a2 = roster[hworld.sid("a2")]
    assert (a2["submission_status"], a2["marks_awarded"], a2["submission_id"]) == (
        "not_submitted",
        None,
        None,
    )
    outstanding = hworld.items(f"/assignments/{assignment_id}/students", outstanding=True)
    assert [s["student_id"] for s in outstanding] == [hworld.sid("a2")]
    assert len(hworld.items(f"/assignments/{assignment_id}/students", hworld.a.f)) == 2
    assert (
        len(
            hworld.items(
                f"/assignments/{assignment_id}/students", auth_headers(RoleName.MANAGEMENT)
            )
        )
        == 2
    )
    for headers in (hworld.student("A1"), hworld.parent_of("a1")):
        assert hworld.get(f"/assignments/{assignment_id}/students", headers).status_code == 403
    assert hworld.get(f"/assignments/{assignment_id}/students", hworld.a.g).status_code == 404
