"""Marks entry, bulk entry, corrections and faculty authorization (real PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.exams import ExamMark, ExamMarkChange
from app.security.permissions import RoleName
from tests.conftest import HeadersFactory
from tests.integration.conftest import API, ExamWorld

pytestmark = pytest.mark.db


def _count(db_session: Session, model: Any) -> int:
    return int(db_session.scalar(select(func.count()).select_from(model)) or 0)


def _audits(db_session: Session, action: str) -> list[AuditLog]:
    return list(db_session.scalars(select(AuditLog).where(AuditLog.action == action)))


def _student(eworld: ExamWorld, adm: str, **enroll: Any) -> tuple[str, str]:
    """A new student enrolled (default: year_a / class_a / section_a on 2026-06-10)."""
    student_id = eworld.a.w.student(adm).json()["id"]
    response = eworld.a.w.enroll(student_id, **enroll)
    assert response.status_code == 201, response.text
    return student_id, response.json()["id"]


# ===================================================================== single entry
def test_enter_single_marks(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, comps = eworld.setup()
    response = eworld.mark(exam_id, comps["math_a"], "a1", 45.5, remarks="Good effort")
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["marks_obtained"], body["max_marks"]) == ("present", 45.5, 100)
    assert body["enrollment_id"] == eworld.a.e["a1"]
    assert body["section_id"] == eworld.ids["section_a"]
    assert body["corrected_at"] is None
    absent = eworld.mark(exam_id, comps["math_a"], "a2", None, status="ABSENT")
    assert absent.status_code == 201, absent.text
    assert (absent.json()["status"], absent.json()["marks_obtained"]) == ("absent", None)
    # A class-wide exam reaches every section of the class.
    assert eworld.mark(exam_id, comps["math_a"], "ab", 0).status_code == 201
    exempt = eworld.mark(exam_id, comps["sci_a"], "ab", None, status="exempt")
    assert exempt.status_code == 201
    audit = _audits(db_session, "exam_mark.created")
    assert len(audit) == 4
    assert audit[0].details["marks_obtained"] == "45.50"
    assert "Good effort" not in str([a.details for a in audit])


@pytest.mark.parametrize(
    ("marks", "extra"),
    [
        (100.01, {}),
        (-1, {}),
        (50.123, {}),
        ("abc", {}),
        (None, {}),  # present without marks
        (10, {"status": "absent"}),
        (10, {"status": "exempt"}),
        (10, {"status": "late"}),
        (10, {"remarks": "x" * 501}),
        (10, {"entered_by_user_id": "00000000-0000-4000-8000-000000000001"}),
    ],
)
def test_invalid_marks_rejected(
    eworld: ExamWorld, db_session: Session, marks: Any, extra: dict[str, Any]
) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    assert eworld.mark(exam_id, comps["math_a"], "a1", marks, **extra).status_code == 422
    assert _count(db_session, ExamMark) == 0


def test_maximum_is_per_component(eworld: ExamWorld) -> None:
    exam_id = eworld.exam_id()
    small = eworld.component_id(exam_id, "math_a", max_marks=20, pass_marks=7)
    eworld.open(exam_id)
    over = eworld.mark(exam_id, small, "a1", 20.5)
    assert over.status_code == 422
    assert over.json()["error"]["message"] == "Must not exceed the maximum marks (20.00)."
    assert eworld.mark(exam_id, small, "a1", 20).status_code == 201


def test_eligibility(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    for student in ("c2", "prev", "b", "00000000-0000-4000-8000-000000000999"):
        response = eworld.mark(exam_id, comps["math_a"], student)
        assert response.status_code == 422, student
        assert (
            response.json()["error"]["message"] == "Student is not eligible for this exam subject."
        )
    section_exam, section_comps = eworld.setup(("math_a",), sec="section_a", name="UT1")
    assert eworld.mark(section_exam, section_comps["math_a"], "ab").status_code == 422
    assert eworld.mark(section_exam, section_comps["math_a"], "a1").status_code == 201


def test_eligibility_uses_the_assessment_date(eworld: ExamWorld) -> None:
    """A late joiner sits only the components assessed after they joined; a student who
    moved section is placed by the enrollment covering that date."""
    exam_id = eworld.exam_id()
    early = eworld.component_id(exam_id, "math_a", assessment_date="2026-09-01")
    late = eworld.component_id(exam_id, "sci_a", assessment_date="2026-09-08")
    eworld.open(exam_id)
    joiner, _ = _student(eworld, "LATE1", date="2026-09-05")
    assert eworld.mark(exam_id, early, joiner).status_code == 422
    assert eworld.mark(exam_id, late, joiner).status_code == 201

    mover, old = _student(eworld, "MOVE1")
    moved = eworld.a.w.enroll(
        mover, sec="section_a_b", date="2026-09-04", supersedes_enrollment_id=old
    )
    assert moved.status_code == 201, moved.text
    first = eworld.mark(exam_id, early, mover)
    second = eworld.mark(exam_id, late, mover)
    assert (first.json()["section_id"], first.json()["enrollment_id"]) == (
        eworld.ids["section_a"],
        old,
    )
    assert (second.json()["section_id"], second.json()["enrollment_id"]) == (
        eworld.ids["section_a_b"],
        moved.json()["id"],
    )


def test_duplicate_entry_and_state_gating(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup(("math_a",), open_entry=False)
    draft = eworld.mark(exam_id, comps["math_a"], "a1")
    assert draft.status_code == 409
    assert "open for marks entry" in draft.json()["error"]["message"]
    assert eworld.action(exam_id, "schedule").status_code == 200
    assert eworld.mark(exam_id, comps["math_a"], "a1").status_code == 409
    assert eworld.action(exam_id, "open-marks-entry").status_code == 200
    assert eworld.mark(exam_id, comps["math_a"], "a1").status_code == 201
    duplicate = eworld.mark(exam_id, comps["math_a"], "a1", 60)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["details"][0]["loc"] == ["body", "student_id"]
    other_exam, other = eworld.setup(("math_a",), name="Final")
    assert eworld.mark(exam_id, other["math_a"], "a1").status_code == 404  # wrong exam
    assert eworld.mark(other_exam, other["math_a"], "a1").status_code == 201


# ======================================================================= bulk entry
def test_bulk_entry(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    response = eworld.bulk(
        exam_id,
        comps["math_a"],
        [
            {"student_id": "a1", "marks_obtained": 88},
            {"student_id": "a2", "status": "absent"},
            {"student_id": "ab", "marks_obtained": "34.75"},
        ],
    )
    assert response.status_code == 201, response.text
    assert response.json()["created"] == 3
    assert {i["marks_obtained"] for i in response.json()["items"]} == {88, None, 34.75}
    audit = _audits(db_session, "exam_mark.bulk_created")[0].details
    assert audit["marks"] == 3
    assert audit["status_counts"] == {"present": 2, "absent": 1, "exempt": 0}


@pytest.mark.parametrize(
    ("entries", "status", "index"),
    [
        (
            [{"student_id": "a1", "marks_obtained": 1}, {"student_id": "a1", "marks_obtained": 2}],
            422,
            1,
        ),
        (
            [{"student_id": "a1", "marks_obtained": 1}, {"student_id": "c2", "marks_obtained": 2}],
            422,
            1,
        ),
        (
            [
                {"student_id": "a1", "marks_obtained": 1},
                {"student_id": "a2", "marks_obtained": 101},
            ],
            422,
            1,
        ),
        (
            [{"student_id": "b", "marks_obtained": 1}, {"student_id": "a2", "marks_obtained": 2}],
            422,
            0,
        ),
    ],
)
def test_bulk_is_all_or_nothing(
    eworld: ExamWorld,
    db_session: Session,
    entries: list[dict[str, Any]],
    status: int,
    index: int,
) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    response = eworld.bulk(exam_id, comps["math_a"], entries)
    assert response.status_code == status, response.text
    assert response.json()["error"]["details"][0]["loc"][2] == index
    assert _count(db_session, ExamMark) == 0


def test_bulk_refuses_already_entered_students(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    eworld.mark_id(exam_id, comps["math_a"], "a2")
    response = eworld.bulk(
        exam_id,
        comps["math_a"],
        [{"student_id": "a1", "marks_obtained": 10}, {"student_id": "a2", "marks_obtained": 20}],
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["loc"] == ["body", "marks", 1, "student_id"]
    assert _count(db_session, ExamMark) == 1  # a1 was not written


def test_bulk_size_limits(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    path = f"{eworld.subject_path(exam_id, comps['math_a'])}/marks/bulk"
    assert eworld.client.post(path, headers=eworld.admin, json={"marks": []}).status_code == 422
    too_many = [{"student_id": eworld.sid("a1"), "marks_obtained": 1}] * 201
    assert (
        eworld.client.post(path, headers=eworld.admin, json={"marks": too_many}).status_code == 422
    )


# ===================================================================== eligibility list
def test_eligible_students_list(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    path = f"/exams/{exam_id}/subjects/{comps['math_a']}/students"
    everyone = eworld.items(path)
    assert {s["student_id"] for s in everyone} == {eworld.sid(k) for k in ("a1", "a2", "ab")}
    eworld.mark_id(exam_id, comps["math_a"], "a1", 70)
    entered = next(s for s in eworld.items(path) if s["student_id"] == eworld.sid("a1"))
    assert (entered["status"], entered["marks_obtained"]) == ("present", 70)
    assert {s["student_id"] for s in eworld.items(path, unmarked=True)} == {
        eworld.sid("a2"),
        eworld.sid("ab"),
    }
    assert eworld.get(path, unmarked=True).json()["total"] == 2


# ====================================================================== corrections
def test_correction_history(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    mark_id = eworld.mark_id(exam_id, comps["math_a"], "a1", 45)
    assert eworld.correct(mark_id, marks_obtained=50).status_code == 422  # reason required
    assert eworld.correct(mark_id, reason="nothing").status_code == 422
    assert eworld.correct(mark_id, marks_obtained=50, reason="   ").status_code == 422

    unchanged = eworld.correct(mark_id, marks_obtained=45, reason="Re-check")
    assert unchanged.status_code == 200
    assert unchanged.json()["corrected_at"] is None
    assert eworld.get(f"/exam-marks/{mark_id}/history").json() == []

    first = eworld.correct(mark_id, marks_obtained=54.5, reason="Totalling error")
    assert first.status_code == 200, first.text
    assert (first.json()["marks_obtained"], first.json()["correction_reason"]) == (
        54.5,
        "Totalling error",
    )
    assert eworld.correct(mark_id, status="absent", reason="Was not present").status_code == 200
    assert eworld.correct(mark_id, status="present", reason="Found script").status_code == 422
    assert eworld.correct(mark_id, marks_obtained=10, reason="x").status_code == 422
    assert (
        eworld.correct(mark_id, status="present", marks_obtained=100.5, reason="x").status_code
        == 422
    )
    final = eworld.correct(
        mark_id, status="present", marks_obtained=61, remarks="Rechecked", reason="Found script"
    )
    assert final.status_code == 200, final.text
    assert eworld.correct(mark_id, remarks=None, reason="Remove note").status_code == 200

    history = eworld.get(f"/exam-marks/{mark_id}/history").json()
    assert [
        (h["sequence"], h["previous_status"], h["previous_marks"], h["new_status"], h["new_marks"])
        for h in history
    ] == [
        (1, "present", 45, "present", 54.5),
        (2, "present", 54.5, "absent", None),
        (3, "absent", None, "present", 61),
        (4, "present", 61, "present", 61),
    ]
    assert [h["reason"] for h in history] == [
        "Totalling error",
        "Was not present",
        "Found script",
        "Remove note",
    ]
    assert (history[3]["previous_remarks"], history[3]["new_remarks"]) == ("Rechecked", None)
    assert all(h["changed_by_user_id"] and h["changed_at"] for h in history)
    assert _count(db_session, ExamMarkChange) == 4
    audit = _audits(db_session, "exam_mark.corrected")
    assert len(audit) == 4
    assert (audit[0].details["previous_marks"], audit[0].details["new_marks"]) == ("45.00", "54.50")
    assert audit[0].details["reason"] == "Totalling error"
    assert "Rechecked" not in str([a.details for a in audit])  # remarks recorded by name only
    assert audit[2].details["remarks_changed"] is True


def test_corrections_only_during_marks_entry(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    mark_id = eworld.mark_id(exam_id, comps["math_a"], "a1", 45)
    assert eworld.action(exam_id, "cancel", body={"reason": "Void"}).status_code == 200
    blocked = eworld.correct(mark_id, marks_obtained=50, reason="Late fix")
    assert blocked.status_code == 409


# ================================================================ faculty authorization
def test_faculty_enter_marks_only_for_their_subject_and_section(
    eworld: ExamWorld,
) -> None:
    a = eworld.a
    exam_id, comps = eworld.setup()
    assert eworld.mark(exam_id, comps["math_a"], "a1", 70, a.f).status_code == 201
    # F teaches math in section A only: section B students are out of reach.
    assert eworld.mark(exam_id, comps["math_a"], "ab", 70, a.f).status_code == 422
    sci = eworld.mark(exam_id, comps["sci_a"], "a1", 70, a.f)
    assert sci.status_code == 403
    assert eworld.mark(exam_id, comps["math_a"], "ab", 70, a.h).status_code == 201
    assert eworld.mark(exam_id, comps["math_a"], "a2", 70, a.h).status_code == 422
    assert eworld.mark(exam_id, comps["sci_a"], "a1", 70, a.g).status_code == 201
    bulk = eworld.bulk(
        exam_id,
        comps["math_a"],
        [{"student_id": "a2", "marks_obtained": 1}, {"student_id": "ab", "marks_obtained": 2}],
        a.f,
    )
    assert bulk.status_code == 422
    assert bulk.json()["error"]["details"][0]["loc"] == ["body", "marks", 1, "student_id"]


def test_faculty_read_scope(eworld: ExamWorld) -> None:
    a = eworld.a
    exam_id, comps = eworld.setup()
    math_a1 = eworld.mark_id(exam_id, comps["math_a"], "a1")
    math_ab = eworld.mark_id(exam_id, comps["math_a"], "ab")
    sci_a1 = eworld.mark_id(exam_id, comps["sci_a"], "a1")
    marks = f"/exams/{exam_id}/subjects/{comps['math_a']}/marks"
    assert {m["id"] for m in eworld.items(marks, a.f)} == {math_a1}
    assert {m["id"] for m in eworld.items(marks, a.h)} == {math_ab}
    assert eworld.items(f"/exams/{exam_id}/subjects/{comps['sci_a']}/marks", a.f) == []
    assert eworld.get(f"/exam-marks/{math_a1}", a.f).status_code == 200
    for mark_id in (math_ab, sci_a1):
        assert eworld.get(f"/exam-marks/{mark_id}", a.f).status_code == 404
        assert eworld.get(f"/exam-marks/{mark_id}/history", a.f).status_code == 404
        assert eworld.correct(mark_id, a.f, marks_obtained=1, reason="x").status_code == 404
    students = f"/exams/{exam_id}/subjects/{comps['math_a']}/students"
    assert {s["student_id"] for s in eworld.items(students, a.f)} == {
        eworld.sid("a1"),
        eworld.sid("a2"),
    }
    assert eworld.items(f"/exams/{exam_id}/subjects/{comps['sci_a']}/students", a.f) == []
    # Faculty correct marks in their scope.
    assert eworld.correct(math_a1, a.f, marks_obtained=66, reason="Recount").status_code == 200


def test_faculty_assignment_must_be_effective_on_the_assessment_date(
    eworld: ExamWorld,
) -> None:
    w = eworld.a.w
    exam_id, comps = eworld.setup(("math_a",))
    late = w.faculty(email="late@school.test").json()["id"]
    # Effective today (2026-10) but not on the assessment date (2026-09-01).
    assert w.assign(late, effective_from="2026-10-01").status_code == 201
    late_headers = w.faculty_headers("late@school.test")
    assert eworld.mark(exam_id, comps["math_a"], "a1", 10, late_headers).status_code == 403
    # Effective on the assessment date but expired since: authority must also be current.
    expired = w.faculty(email="expired@school.test").json()["id"]
    assert w.assign(expired, effective_to="2026-09-30").status_code == 201
    expired_headers = w.faculty_headers("expired@school.test")
    assert eworld.mark(exam_id, comps["math_a"], "a1", 10, expired_headers).status_code == 404
    # Covering both the assessment date and today.
    covering = w.faculty(email="covering@school.test").json()["id"]
    assert w.assign(covering, effective_from="2026-08-01").status_code == 201
    covering_headers = w.faculty_headers("covering@school.test")
    assert eworld.mark(exam_id, comps["math_a"], "a1", 10, covering_headers).status_code == 201


def test_deactivated_assignment_revokes_marks_access(eworld: ExamWorld) -> None:
    a = eworld.a
    exam_id, comps = eworld.setup(("math_a",))
    mark_id = eworld.mark_id(exam_id, comps["math_a"], "a1")
    assignment = eworld.items("/faculty-assignments", faculty_id=a.faculty_ids["f"])[0]["id"]
    assert (
        a.client.patch(
            f"{API}/faculty-assignments/{assignment}", headers=a.admin, json={"status": "inactive"}
        ).status_code
        == 200
    )
    assert eworld.mark(exam_id, comps["math_a"], "a2", 10, a.f).status_code == 404  # exam hidden
    assert eworld.get(f"/exam-marks/{mark_id}", a.f).status_code == 404


def test_management_reads_but_cannot_enter_marks(
    eworld: ExamWorld, auth_headers: HeadersFactory
) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    mark_id = eworld.mark_id(exam_id, comps["math_a"], "a1")
    management = auth_headers(RoleName.MANAGEMENT)
    assert eworld.mark(exam_id, comps["math_a"], "a2", 10, management).status_code == 403
    assert eworld.correct(mark_id, management, marks_obtained=1, reason="x").status_code == 403
    assert eworld.get(f"/exam-marks/{mark_id}", management).status_code == 200
    assert eworld.get(f"/exam-marks/{mark_id}/history", management).json() == []


def test_parents_and_students_cannot_enter_or_read_marks(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup(("math_a",))
    mark_id = eworld.mark_id(exam_id, comps["math_a"], "a1")
    for headers in (eworld.parent_of("a1"), eworld.student_headers("A1")):
        assert eworld.mark(exam_id, comps["math_a"], "a1", 99, headers).status_code == 403
        assert eworld.correct(mark_id, headers, marks_obtained=99, reason="x").status_code == 403
        assert eworld.get(f"/exam-marks/{mark_id}", headers).status_code == 404
        assert eworld.get(f"/exams/{exam_id}", headers).status_code == 404
        assert eworld.items("/exams", headers) == []
        marks = eworld.get(f"/exams/{exam_id}/subjects/{comps['math_a']}/marks", headers)
        assert marks.status_code == 404
