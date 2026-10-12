"""Submissions, resubmissions, deadlines, reviews and personal visibility over HTTP."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.assignments import AssignmentSubmission, AssignmentSubmissionRevision
from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import HeadersFactory
from tests.integration.conftest import API, HomeworkWorld, iso_in

pytestmark = pytest.mark.db


def _audits(db_session: Session, action: str) -> list[AuditLog]:
    return list(db_session.scalars(select(AuditLog).where(AuditLog.action == action)))


def _count(db_session: Session, model: type) -> int:
    return int(db_session.scalar(select(func.count()).select_from(model)) or 0)


# ===================================================================== submitting
def test_student_submits_own_work(hworld: HomeworkWorld, db_session: Session) -> None:
    assignment_id = hworld.published()
    a1 = hworld.student("A1")
    response = hworld.submit(assignment_id, a1, "  x = 4  ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["student_id"] == hworld.sid("a1")
    assert body["enrollment_id"] == hworld.a.e["a1"]
    assert (body["status"], body["revision_count"], body["is_late"], body["content"]) == (
        "submitted",
        1,
        False,
        "x = 4",
    )
    duplicate = hworld.submit(assignment_id, a1)
    assert duplicate.status_code == 409
    audit = _audits(db_session, "assignment_submission.created")[0].details
    assert (audit["revision"], audit["is_late"], audit["content_length"]) == (1, False, 5)
    assert "x = 4" not in str(audit)


@pytest.mark.parametrize(
    "body",
    [
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 20_001},
        {"content": "ok", "student_id": "00000000-0000-4000-8000-000000000001"},
        {},
    ],
)
def test_submission_validation(hworld: HomeworkWorld, body: dict[str, object]) -> None:
    assignment_id = hworld.published()
    response = hworld.client.post(
        f"{API}/assignments/{assignment_id}/submissions", headers=hworld.student("A1"), json=body
    )
    assert response.status_code == 422


def test_only_students_submit(hworld: HomeworkWorld, auth_headers: HeadersFactory) -> None:
    assignment_id = hworld.published()
    for headers in (
        hworld.admin,
        hworld.a.f,
        hworld.parent_of("a1"),
        auth_headers(RoleName.MANAGEMENT),
    ):
        assert hworld.submit(assignment_id, headers).status_code == 403


def test_submission_state_gating(hworld: HomeworkWorld) -> None:
    draft = hworld.create_id()
    assert hworld.submit(draft, hworld.student("A1")).status_code == 404  # drafts are invisible
    closed = hworld.published()
    assert hworld.action(closed, "close").status_code == 200
    refused = hworld.submit(closed, hworld.student("A1"))
    assert refused.status_code == 409
    assert "not open" in refused.json()["error"]["message"]


def test_eligibility_follows_the_current_enrollment(hworld: HomeworkWorld) -> None:
    """A student who moved section after publication keeps their submission (readable)
    but can no longer hand in work for the old section; the new section's open work is
    theirs to submit."""
    old_section = hworld.published()
    new_section = hworld.published(sec="section_a_b", title="B work")
    student_id, enrollment_id, headers = hworld.new_student("MOVER")
    kept = hworld.submission_id(old_section, headers)
    other = hworld.published(title="Second")
    moved = hworld.a.w.enroll(
        student_id, sec="section_a_b", date=iso_in(0)[:10], supersedes_enrollment_id=enrollment_id
    )
    assert moved.status_code == 201, moved.text
    late_for_old = hworld.submit(other, headers)
    assert late_for_old.status_code == 422
    assert late_for_old.json()["error"]["message"] == (
        "You are not enrolled in this assignment's section."
    )
    assert hworld.resubmit(kept, headers).status_code == 422
    assert hworld.get(f"/assignment-submissions/{kept}", headers).status_code == 200
    assert hworld.submit(new_section, headers).status_code == 201


def test_withdrawn_enrollment_cannot_submit(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published()
    _, enrollment_id, headers = hworld.new_student("LEAVER")
    response = hworld.client.patch(
        f"{API}/enrollments/{enrollment_id}",
        headers=hworld.admin,
        json={"status": "withdrawn", "end_date": "2026-09-01"},
    )
    assert response.status_code == 200, response.text
    assert hworld.submit(assignment_id, headers).status_code in (404, 422)


# ====================================================================== deadlines
def test_late_work_is_refused_unless_allowed(hworld: HomeworkWorld) -> None:
    strict = hworld.published()
    lenient = hworld.published(allow_late=True, title="Lenient")
    for assignment_id in (strict, lenient):
        assert hworld.patch(assignment_id, {"due_at": iso_in(-0.01)}).status_code == 200
    refused = hworld.submit(strict, hworld.student("A1"))
    assert refused.status_code == 409
    assert "deadline has passed" in refused.json()["error"]["message"]
    late = hworld.submit(lenient, hworld.student("A1"))
    assert late.status_code == 201
    assert late.json()["is_late"] is True
    roster = hworld.items(f"/assignments/{lenient}/students")
    assert next(s for s in roster if s["student_id"] == hworld.sid("a1"))["is_late"] is True


def test_lateness_is_fixed_when_work_is_handed_in(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published(allow_late=True)
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    assert hworld.patch(assignment_id, {"due_at": iso_in(-0.01)}).status_code == 200
    second = hworld.resubmit(submission, hworld.student("A1"))
    assert second.status_code == 201
    assert second.json()["is_late"] is True
    revisions = hworld.get(
        f"/assignment-submissions/{submission}/revisions", hworld.student("A1")
    ).json()
    assert [(r["revision"], r["is_late"]) for r in revisions] == [(1, False), (2, True)]


# =================================================================== resubmission
def test_resubmission_keeps_history(hworld: HomeworkWorld, db_session: Session) -> None:
    assignment_id = hworld.published()
    a1 = hworld.student("A1")
    submission = hworld.submission_id(assignment_id, a1, "First draft")
    for text in ("Second draft", "Final answer"):
        response = hworld.resubmit(submission, a1, text)
        assert response.status_code == 201, response.text
    current = hworld.get(f"/assignment-submissions/{submission}", a1).json()
    assert (current["revision_count"], current["content"]) == (3, "Final answer")
    revisions = hworld.get(f"/assignment-submissions/{submission}/revisions", a1).json()
    assert [r["content"] for r in revisions] == ["First draft", "Second draft", "Final answer"]
    assert _count(db_session, AssignmentSubmissionRevision) == 3
    assert len(_audits(db_session, "assignment_submission.resubmitted")) == 2


def test_resubmission_rules(hworld: HomeworkWorld) -> None:
    a1, a2 = hworld.student("A1"), hworld.student("A2")
    no_redo = hworld.published(allow_resubmission=False)
    submission = hworld.submission_id(no_redo, a1)
    assert hworld.resubmit(submission, a1).status_code == 409
    open_one = hworld.published(title="Open")
    mine = hworld.submission_id(open_one, a1)
    assert hworld.resubmit(mine, a2).status_code == 404  # someone else's work
    assert hworld.review(mine, feedback="Good start").status_code == 200
    reviewed = hworld.resubmit(mine, a1)
    assert reviewed.status_code == 409
    assert "reviewed" in reviewed.json()["error"]["message"]


# ======================================================================== reviews
def test_review_and_grade_history(hworld: HomeworkWorld, db_session: Session) -> None:
    assignment_id = hworld.published(max_marks=20)
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    first = hworld.review(submission, hworld.a.f, marks_awarded=15.5, feedback="Well argued")
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["status"], body["marks_awarded"], body["reviewed_revision"]) == (
        "reviewed",
        15.5,
        1,
    )
    no_reason = hworld.review(submission, hworld.a.f, marks_awarded=17)
    assert no_reason.status_code == 422
    assert no_reason.json()["error"]["details"][0]["loc"] == ["body", "reason"]
    unchanged = hworld.review(submission, hworld.a.f, marks_awarded=15.5)
    assert unchanged.status_code == 200
    changed = hworld.review(submission, hworld.a.f, marks_awarded=17, reason="Re-marked Q4")
    assert changed.status_code == 200
    cleared = hworld.review(submission, feedback=None, reason="Feedback was for another student")
    assert cleared.status_code == 200
    assert (cleared.json()["feedback"], cleared.json()["marks_awarded"]) == (None, 17)
    history = hworld.get(f"/assignment-submissions/{submission}/review-history").json()
    assert [(h["sequence"], h["previous_marks"], h["new_marks"], h["reason"]) for h in history] == [
        (1, None, 15.5, None),
        (2, 15.5, 17, "Re-marked Q4"),
        (3, 17, 17, "Feedback was for another student"),
    ]
    assert (history[2]["previous_feedback"], history[2]["new_feedback"]) == ("Well argued", None)
    assert len(_audits(db_session, "assignment_submission.reviewed")) == 1
    changes = _audits(db_session, "assignment_submission.review_changed")
    assert len(changes) == 2
    assert (changes[0].details["previous_marks"], changes[0].details["new_marks"]) == (
        "15.50",
        "17.00",
    )
    assert "Well argued" not in str([c.details for c in changes])


@pytest.mark.parametrize("marks", [20.01, -1, 10.555, "ten"])
def test_invalid_grades_are_rejected(hworld: HomeworkWorld, marks: object) -> None:
    assignment_id = hworld.published(max_marks=20)
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    response = hworld.review(submission, marks_awarded=marks)
    assert response.status_code == 422
    if marks == 20.01:
        assert response.json()["error"]["message"] == "Must not exceed the maximum marks (20.00)."
    assert hworld.get(f"/assignment-submissions/{submission}").json()["status"] == "submitted"


def test_ungraded_assignments_take_feedback_only(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published()
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    refused = hworld.review(submission, marks_awarded=5)
    assert refused.status_code == 422
    assert "not graded" in refused.json()["error"]["message"]
    assert hworld.review(submission).status_code == 422  # nothing to record
    assert hworld.review(submission, feedback="Nice").status_code == 200


def test_review_after_closing(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published(max_marks=10)
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    assert hworld.action(assignment_id, "close").status_code == 200
    assert hworld.review(submission, marks_awarded=8).status_code == 200


def test_who_reviews(hworld: HomeworkWorld, auth_headers: HeadersFactory) -> None:
    assignment_id = hworld.published(max_marks=10)
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    assert hworld.review(submission, hworld.a.g, marks_awarded=1).status_code == 404
    assert hworld.review(submission, hworld.a.h, marks_awarded=1).status_code == 404
    assert (
        hworld.review(submission, auth_headers(RoleName.MANAGEMENT), marks_awarded=1).status_code
        == 403
    )
    for headers in (hworld.student("A1"), hworld.parent_of("a1")):
        assert hworld.review(submission, headers, marks_awarded=10).status_code == 403
    assert hworld.review(submission, hworld.a.f, marks_awarded=9).status_code == 200


# ===================================================================== visibility
def test_personal_visibility(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published(max_marks=10)
    mine = hworld.submission_id(assignment_id, hworld.student("A1"), "A1's essay")
    theirs = hworld.submission_id(assignment_id, hworld.student("A2"), "A2's essay")
    assert hworld.review(mine, marks_awarded=7, feedback="Good").status_code == 200
    a1 = hworld.student("A1")
    assert [s["id"] for s in hworld.items("/assignment-submissions", a1)] == [mine]
    assert hworld.get(f"/assignment-submissions/{theirs}", a1).status_code == 404
    assert hworld.items("/assignment-submissions", a1, student_id=hworld.sid("a2")) == []
    parent = hworld.parent_of("a1")
    seen = hworld.get(f"/assignment-submissions/{mine}", parent).json()
    # Parents see status, marks and feedback, but not the submitted work itself.
    assert (seen["status"], seen["marks_awarded"], seen["feedback"], seen["content"]) == (
        "reviewed",
        7,
        "Good",
        None,
    )
    assert hworld.get(f"/assignment-submissions/{mine}/revisions", parent).status_code == 403
    assert hworld.get(f"/assignment-submissions/{mine}/review-history", parent).status_code == 200
    assert hworld.get(f"/assignment-submissions/{theirs}", parent).status_code == 404
    assert hworld.items("/assignment-submissions", parent) == [seen]
    # Faculty in scope read the work; others do not reach it.
    assert (
        hworld.get(f"/assignment-submissions/{theirs}", hworld.a.f).json()["content"]
        == "A2's essay"
    )
    assert hworld.get(f"/assignment-submissions/{theirs}", hworld.a.g).status_code == 404


def test_access_ends_when_relationships_end(hworld: HomeworkWorld) -> None:
    assignment_id = hworld.published()
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    parent = hworld.parent_of("a1")
    assert hworld.get(f"/assignment-submissions/{submission}", parent).status_code == 200
    links = hworld.get(f"/students/{hworld.sid('a1')}/parents").json()
    response = hworld.client.patch(
        f"{API}/students/{hworld.sid('a1')}/parents/{links[0]['parent_id']}",
        headers=hworld.admin,
        json={"is_active": False},
    )
    assert response.status_code == 200
    assert hworld.get(f"/assignment-submissions/{submission}", parent).status_code == 404
    assert hworld.items("/assignments", parent) == []


def test_assignment_grades_never_touch_exam_results(
    hworld: HomeworkWorld, db_session: Session
) -> None:
    assignment_id = hworld.published(max_marks=10)
    submission = hworld.submission_id(assignment_id, hworld.student("A1"))
    assert hworld.review(submission, marks_awarded=10).status_code == 200
    for table in ("exam_marks", "exam_report_cards", "exam_publications"):
        assert db_session.execute(select(func.count()).select_from(_table(table))).scalar() == 0
    assert _count(db_session, AssignmentSubmission) == 1


def _table(name: str) -> object:
    from sqlalchemy import table

    return table(name)
