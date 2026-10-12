"""IDOR, cross-school and filter attacks against assignments and submissions."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.assignments import (
    AssignmentReviewChange,
    AssignmentSubmission,
    AssignmentSubmissionRevision,
)
from app.models.faculty import Faculty
from app.models.people import Parent, Student
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import API, STUDENT_PASSWORD, HomeworkWorld, iso_in, today_iso

pytestmark = pytest.mark.db


def _school_b(hworld: HomeworkWorld) -> dict[str, str]:
    """A published school-B assignment with a reviewed submission."""
    w = hworld.a.w
    b = w.admin_b
    body = hworld.body(
        year="year_b", cls="class_b", sec="section_b", subject="math_b", max_marks=10
    )
    response = hworld.client.post(f"{API}/assignments", headers=b, json=body)
    assert response.status_code == 201, response.text
    assignment = response.json()["id"]
    assert hworld.action(assignment, "publish", b).status_code == 200
    student = w.student("BSTU", b, password=STUDENT_PASSWORD).json()["id"]
    enrolled = w.enroll(student, b, year="year_b", cls="class_b", sec="section_b")
    assert enrolled.status_code == 201
    login = hworld.client.post(
        f"{API}/auth/login",
        json={"school_code": "SCH-B", "identifier": "BSTU", "password": STUDENT_PASSWORD},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    submission = hworld.submission_id(assignment, headers)
    assert hworld.review(submission, b, marks_awarded=5).status_code == 200
    return {"assignment": assignment, "submission": submission, "student": student}


def _counts(db_session: Session) -> tuple[int, int]:
    return (
        int(db_session.scalar(select(func.count()).select_from(AssignmentSubmission)) or 0),
        int(db_session.scalar(select(func.count()).select_from(AssignmentReviewChange)) or 0),
    )


def test_school_a_cannot_reach_school_b(hworld: HomeworkWorld, db_session: Session) -> None:
    b = _school_b(hworld)
    before = _counts(db_session)
    requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"/assignments/{b['assignment']}", None),
        ("PATCH", f"/assignments/{b['assignment']}", {"title": "Hijacked"}),
        ("POST", f"/assignments/{b['assignment']}/publish", None),
        ("POST", f"/assignments/{b['assignment']}/close", None),
        ("GET", f"/assignments/{b['assignment']}/students", None),
        ("GET", f"/assignment-submissions/{b['submission']}", None),
        ("GET", f"/assignment-submissions/{b['submission']}/revisions", None),
        ("GET", f"/assignment-submissions/{b['submission']}/review-history", None),
        (
            "PUT",
            f"/assignment-submissions/{b['submission']}/review",
            {"marks_awarded": 0, "reason": "x"},
        ),
    ]
    for method, path, body in requests:
        response = hworld.client.request(method, f"{API}{path}", headers=hworld.admin, json=body)
        assert response.status_code == 404, (method, path, response.text)
    a1 = hworld.student("A1")
    assert hworld.submit(b["assignment"], a1).status_code == 404
    assert hworld.resubmit(b["submission"], a1).status_code == 404
    assert _counts(db_session) == before
    for path, params in (
        ("/assignments", {"academic_year_id": hworld.ids["year_b"]}),
        ("/assignments", {"section_id": hworld.ids["section_b"]}),
        ("/assignment-submissions", {"student_id": b["student"]}),
        ("/assignment-submissions", {"status": "reviewed"}),
    ):
        assert hworld.items(path, **params) == [], (path, params)
    assert hworld.get("/assignment-submissions", assignment_id=b["assignment"]).status_code == 404


def test_school_selection_cannot_be_forged(
    hworld: HomeworkWorld, auth_headers: HeadersFactory
) -> None:
    b = _school_b(hworld)
    assert (
        hworld.get("/assignments", {**hworld.admin, "X-School-ID": str(SCHOOL_B)}).status_code
        == 404
    )
    root_a = auth_headers(RoleName.SUPER_ADMIN, SCHOOL_A)
    root_b = auth_headers(RoleName.SUPER_ADMIN, SCHOOL_B)
    assert hworld.get(f"/assignments/{b['assignment']}", root_a).status_code == 404
    assert hworld.get(f"/assignments/{b['assignment']}", root_b).status_code == 200
    assert hworld.get(f"/assignment-submissions/{b['submission']}", root_b).status_code == 200
    unselected = auth_headers(RoleName.SUPER_ADMIN, select_school=False)
    assert hworld.get("/assignments", unselected).status_code == 400


def test_unknown_and_swapped_ids(hworld: HomeworkWorld) -> None:
    random_id = str(uuid.uuid4())
    for path in (f"/assignments/{random_id}", f"/assignment-submissions/{random_id}"):
        assert hworld.get(path).status_code == 404
    assert hworld.submit(random_id, hworld.student("A1")).status_code == 404


def test_faculty_filters_only_narrow(hworld: HomeworkWorld) -> None:
    a = hworld.a
    math_a = hworld.published()
    sci_a = hworld.published(subject="sci_a")
    math_b = hworld.published(sec="section_a_b")
    sub_sci = hworld.submission_id(sci_a, hworld.student("A1"))
    hworld.submission_id(math_a, hworld.student("A1"))
    assert {s["assignment_id"] for s in hworld.items("/assignment-submissions", a.f)} == {math_a}
    for params in (
        {"assignment_id": sci_a},
        {"assignment_id": math_b},
    ):
        assert hworld.get("/assignment-submissions", a.f, **params).status_code == 404
    assert (
        hworld.items("/assignment-submissions", a.f, student_id=hworld.sid("a1"), status="reviewed")
        == []
    )
    assert hworld.get(f"/assignment-submissions/{sub_sci}", a.f).status_code == 404
    assert hworld.get(f"/assignment-submissions/{sub_sci}/revisions", a.f).status_code == 404


def test_student_cannot_act_for_another_student(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published()
    theirs = hworld.submission_id(assignment_id, hworld.student("A2"))
    a1 = hworld.student("A1")
    assert hworld.resubmit(theirs, a1).status_code == 404
    assert hworld.get(f"/assignment-submissions/{theirs}/revisions", a1).status_code == 404
    assert hworld.get(f"/assignment-submissions/{theirs}/review-history", a1).status_code == 404
    # The body cannot name a student: the submitter is always the caller.
    forged = hworld.client.post(
        f"{API}/assignments/{assignment_id}/submissions",
        headers=a1,
        json={"content": "x", "student_id": hworld.sid("a2")},
    )
    assert forged.status_code == 422


def test_dates_in_window_for_new_and_past_students(hworld: HomeworkWorld) -> None:
    """Visibility follows enrollment during the assignment window, not the current
    placement alone."""
    assignment_id = hworld.published(assigned_on=today_iso(), due_at=iso_in(3))
    _, _, joiner = hworld.new_student("FUTURE", date=iso_in(10)[:10])
    assert hworld.get(f"/assignments/{assignment_id}", joiner).status_code == 404
    _, _, current = hworld.new_student("CURRENT")
    assert hworld.get(f"/assignments/{assignment_id}", current).status_code == 200


def test_faculty_parent_reviews_only_through_faculty_scope(
    hworld: HomeworkWorld, db_session: Session
) -> None:
    """G teaches science in section A and is also A1's parent. Parent visibility lets G
    read A1's maths submission like any parent; only the faculty scope (science) gives G
    the work itself, the roster and reviews."""
    w = hworld.a.w
    g_user = db_session.scalar(
        select(Faculty.user_id).where(Faculty.id == uuid.UUID(hworld.a.faculty_ids["g"]))
    )
    parent_id = w.parent().json()["id"]
    assert w.link(hworld.sid("a1"), parent_id).status_code == 201
    db_session.execute(
        update(Parent).where(Parent.id == uuid.UUID(parent_id)).values(user_id=g_user)
    )
    db_session.flush()
    roles = hworld.client.put(
        f"{API}/users/{g_user}/roles",
        headers=hworld.admin,
        json={"roles": [RoleName.FACULTY, RoleName.PARENT]},
    )
    assert roles.status_code == 200, roles.text
    g = w.faculty_headers("att-g@school.test")

    assignment_id = hworld.published(max_marks=10)  # maths: F's, not G's
    child = hworld.submission_id(assignment_id, hworld.student("A1"))
    other = hworld.submission_id(assignment_id, hworld.student("A2"))
    assert hworld.get(f"/assignment-submissions/{child}", g).status_code == 200
    # As a parent, G sees the child's status but not the work, the class roster or the
    # other students' submissions.
    assert hworld.get(f"/assignment-submissions/{child}", g).json()["content"] is None
    assert hworld.get(f"/assignment-submissions/{child}/revisions", g).status_code == 403
    assert hworld.get(f"/assignments/{assignment_id}/students", g).status_code == 403
    assert hworld.get(f"/assignment-submissions/{other}", g).status_code == 404
    assert hworld.review(child, g, marks_awarded=10).status_code == 403
    assert hworld.review(other, g, marks_awarded=10).status_code == 404
    assert db_session.scalar(select(func.count()).select_from(AssignmentReviewChange)) == 0

    science = hworld.published(subject="sci_a")
    work = hworld.submission_id(science, hworld.student("A1"), "Lab report")
    assert hworld.get(f"/assignment-submissions/{work}", g).json()["content"] == "Lab report"
    assert hworld.get(f"/assignment-submissions/{work}/revisions", g).status_code == 200
    assert hworld.get(f"/assignments/{science}/students", g).status_code == 200


def test_student_parent_resubmits_only_their_own_work(
    hworld: HomeworkWorld, db_session: Session
) -> None:
    """A2 is also A1's parent (e.g. an older sibling as guardian). Parent visibility lets
    A2 read A1's submission, but A2 can never add a revision to it."""
    w = hworld.a.w
    a2_user = db_session.scalar(
        select(Student.user_id).where(Student.id == uuid.UUID(hworld.sid("a2")))
    )
    parent_id = w.parent().json()["id"]
    assert w.link(hworld.sid("a1"), parent_id).status_code == 201
    db_session.execute(
        update(Parent).where(Parent.id == uuid.UUID(parent_id)).values(user_id=a2_user)
    )
    db_session.flush()
    roles = hworld.client.put(
        f"{API}/users/{a2_user}/roles",
        headers=hworld.admin,
        json={"roles": [RoleName.STUDENT, RoleName.PARENT]},
    )
    assert roles.status_code == 200, roles.text
    a2 = hworld.student("A2")

    assignment_id = hworld.published(allow_resubmission=True)
    child = hworld.submission_id(assignment_id, hworld.student("A1"), "A1's work")
    assert hworld.get(f"/assignment-submissions/{child}", a2).status_code == 200
    assert hworld.resubmit(child, a2, "Not mine").status_code == 404
    revisions = db_session.scalars(
        select(AssignmentSubmissionRevision.content).where(
            AssignmentSubmissionRevision.submission_id == uuid.UUID(child)
        )
    ).all()
    assert list(revisions) == ["A1's work"]
