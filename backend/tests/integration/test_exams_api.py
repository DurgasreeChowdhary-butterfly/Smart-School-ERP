"""Exam configuration, subjects and lifecycle over HTTP (real PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_B, HeadersFactory
from tests.integration.conftest import API, ExamWorld

pytestmark = pytest.mark.db


def _audits(db_session: Session, action: str) -> list[AuditLog]:
    return list(db_session.scalars(select(AuditLog).where(AuditLog.action == action)))


# ===================================================================== creation
def test_create_class_wide_and_section_exam(eworld: ExamWorld, db_session: Session) -> None:
    response = eworld.exam(
        description="Covers chapters 1-4",
        pass_percentage=40,
        grade_bands=[
            {"grade": "B", "min_percentage": 0},
            {"grade": "A", "min_percentage": 75},
        ],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["section_id"] is None
    assert body["pass_percentage"] == 40
    # Stored highest threshold first.
    assert [b["grade"] for b in body["grade_bands"]] == ["A", "B"]
    assert body["grade_bands"][0]["min_percentage"] == 75

    section_exam = eworld.exam(sec="section_a", name="Unit Test 1", exam_type="UNIT_TEST")
    assert section_exam.status_code == 201, section_exam.text
    assert section_exam.json()["section_id"] == eworld.ids["section_a"]
    assert section_exam.json()["exam_type"] == "unit_test"

    audit = _audits(db_session, "exam.created")
    assert len(audit) == 2
    assert audit[0].details["school_id"]
    assert audit[0].resource_type == "exam"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"start": "2026-09-10", "end": "2026-09-01"}, "end_date"),
        ({"start": "2026-05-01", "end": "2026-06-05"}, "start_date"),
        ({"start": "2027-05-30", "end": "2027-06-02"}, "start_date"),
    ],
)
def test_exam_dates_validated(eworld: ExamWorld, override: dict[str, Any], field: str) -> None:
    response = eworld.exam(**override)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "body",
    [
        {"pass_percentage": 101},
        {"pass_percentage": 40.555},
        {"grade_bands": [{"grade": "A", "min_percentage": 50}]},  # no band at 0
        {
            "grade_bands": [
                {"grade": "A", "min_percentage": 0},
                {"grade": "A", "min_percentage": 50},
            ]
        },
        {"grade_bands": [{"grade": "A", "min_percentage": 0}, {"grade": "B", "min_percentage": 0}]},
        {"grade_bands": []},
        {"exam_type": "olympiad"},
        {"name": "   "},
        {"unexpected": True},
    ],
)
def test_exam_body_validation(eworld: ExamWorld, body: dict[str, Any]) -> None:
    assert eworld.exam(**body).status_code == 422


def test_exam_placement_must_be_in_school_and_consistent(eworld: ExamWorld) -> None:
    ids = eworld.ids
    for override, message in (
        ({"year": "year_b"}, "Academic year not found."),
        ({"cls": "class_b"}, "Class not found."),
        ({"sec": "section_b"}, "Section not found."),
        ({"sec": "section2_a"}, "Section does not belong to the class."),
    ):
        response = eworld.exam(**override)
        assert response.status_code == 422, override
        assert response.json()["error"]["message"] == message
    # Inactive structure is refused.
    assert (
        eworld.client.patch(
            f"{API}/sections/{ids['section_a_b']}", headers=eworld.admin, json={"is_active": False}
        ).status_code
        == 200
    )
    assert eworld.exam(sec="section_a_b").json()["error"]["message"] == "Section is inactive."


def test_duplicate_exam_names(eworld: ExamWorld) -> None:
    eworld.exam_id()
    duplicate = eworld.exam()
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["details"][0]["loc"] == ["body", "name"]
    # The same name for one section, another class or year is a different exam.
    assert eworld.exam(sec="section_a").status_code == 201
    assert eworld.exam(sec="section_a").status_code == 409
    assert eworld.exam(cls="class2_a").status_code == 201
    assert eworld.exam(year="year_a_prev", start="2025-09-01", end="2025-09-05").status_code == 201
    # A cancelled exam frees its name.
    exam_id = eworld.exam_id(name="Retest")
    assert eworld.action(exam_id, "cancel", body={"reason": "Clash"}).status_code == 200
    assert eworld.exam(name="Retest").status_code == 201


def test_only_school_staff_configure_exams(eworld: ExamWorld, auth_headers: HeadersFactory) -> None:
    management = auth_headers(RoleName.MANAGEMENT)
    assert eworld.exam(management).status_code == 403  # no marks.manage
    faculty = eworld.exam(eworld.a.f)
    assert faculty.status_code == 403
    assert faculty.json()["error"]["message"] == "Exams are configured by school staff."
    parent = eworld.parent_of("a1")
    assert eworld.exam(parent).status_code == 403
    assert eworld.exam(eworld.student_headers("A1")).status_code == 403
    root = auth_headers(RoleName.SUPER_ADMIN)
    assert eworld.exam(root).status_code == 201
    unselected = auth_headers(RoleName.SUPER_ADMIN, select_school=False)
    assert eworld.exam(unselected).status_code == 400


# ======================================================================= reads
def test_list_and_filter_exams(eworld: ExamWorld, auth_headers: HeadersFactory) -> None:
    first = eworld.exam_id()
    second = eworld.exam_id(sec="section_a", name="UT", exam_type="unit_test")
    other_class = eworld.exam_id(cls="class2_a")
    assert {e["id"] for e in eworld.items("/exams")} == {first, second, other_class}
    assert {e["id"] for e in eworld.items("/exams", class_id=eworld.ids["class_a"])} == {
        first,
        second,
    }
    assert {e["id"] for e in eworld.items("/exams", exam_type="unit_test")} == {second}
    assert {e["id"] for e in eworld.items("/exams", status="draft")} == {
        first,
        second,
        other_class,
    }
    page = eworld.get("/exams", page=1, page_size=2).json()
    assert (page["total"], len(page["items"]), page["pages"]) == (3, 2, 2)
    management = auth_headers(RoleName.MANAGEMENT)
    assert len(eworld.items("/exams", management)) == 3
    assert eworld.get(f"/exams/{first}", management).status_code == 200


# =================================================================== updates
def test_update_exam_by_status(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, components = eworld.setup(("math_a",), open_entry=False)
    response = eworld.client.patch(
        f"{API}/exams/{exam_id}",
        headers=eworld.admin,
        json={"name": "Midterm 2026", "description": None, "end_date": "2026-09-12"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Midterm 2026"
    changes = _audits(db_session, "exam.updated")[0].details["changes"]
    assert {"field": "name", "from": "Midterm", "to": "Midterm 2026"} in changes

    # Dates may not strand a subject's assessment date.
    path = f"{API}/exams/{exam_id}/subjects/{components['math_a']}"
    assert (
        eworld.client.patch(
            path, headers=eworld.admin, json={"assessment_date": "2026-09-12"}
        ).status_code
        == 200
    )
    stranded = eworld.client.patch(
        f"{API}/exams/{exam_id}", headers=eworld.admin, json={"end_date": "2026-09-11"}
    )
    assert stranded.status_code == 422
    for body in ({}, {"name": None}, {"class_id": eworld.ids["class2_a"]}, {"status": "published"}):
        assert (
            eworld.client.patch(
                f"{API}/exams/{exam_id}", headers=eworld.admin, json=body
            ).status_code
            == 422
        ), body

    eworld.open(exam_id)
    blocked = eworld.client.patch(
        f"{API}/exams/{exam_id}", headers=eworld.admin, json={"name": "Renamed"}
    )
    assert blocked.status_code == 409
    grading = eworld.client.patch(
        f"{API}/exams/{exam_id}",
        headers=eworld.admin,
        json={"pass_percentage": 33, "grade_bands": [{"grade": "P", "min_percentage": 0}]},
    )
    assert grading.status_code == 200, grading.text
    assert grading.json()["pass_percentage"] == 33

    # No-op update: nothing audited.
    before = len(_audits(db_session, "exam.updated"))
    assert (
        eworld.client.patch(
            f"{API}/exams/{exam_id}", headers=eworld.admin, json={"pass_percentage": 33}
        ).status_code
        == 200
    )
    assert len(_audits(db_session, "exam.updated")) == before


# ================================================================== components
def test_components_configuration(eworld: ExamWorld, db_session: Session) -> None:
    exam_id = eworld.exam_id()
    created = eworld.component(exam_id, max_marks=80, pass_marks=28.5, assessment_date="2026-09-02")
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["max_marks"], body["pass_marks"], body["assessment_date"]) == (
        80,
        28.5,
        "2026-09-02",
    )
    assert eworld.component(exam_id).status_code == 409  # one entry per subject
    assert eworld.component(exam_id, "sci_a", max_marks=50, pass_marks=None).status_code == 201

    for n, override in enumerate(
        (
            {"max_marks": 0},
            {"max_marks": -5},
            {"max_marks": 1001},
            {"max_marks": 50.123},
            {"max_marks": 50, "pass_marks": 51},
            {"pass_marks": -1},
            {"assessment_date": "2026-08-31"},
            {"assessment_date": "2026-09-11"},
            {"start_time": "10:00", "end_time": "09:00"},
        )
    ):
        response = eworld.component(eworld.exam_id(name=f"Invalid {n}"), **override)
        assert response.status_code == 422, (override, response.text)

    other_school = eworld.component(exam_id, "math_b")
    assert other_school.status_code == 422
    assert other_school.json()["error"]["message"] == "Subject not found."

    items = eworld.items(f"/exams/{exam_id}/subjects")
    assert len(items) == 2
    path = f"{API}/exams/{exam_id}/subjects/{body['id']}"
    assert (
        eworld.client.patch(path, headers=eworld.admin, json={"pass_marks": 81}).status_code == 422
    )
    updated = eworld.client.patch(
        path, headers=eworld.admin, json={"max_marks": 90, "pass_marks": None}
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["max_marks"], updated.json()["pass_marks"]) == (90, None)
    assert eworld.client.delete(path, headers=eworld.admin).status_code == 204
    assert eworld.get(f"/exams/{exam_id}/subjects/{body['id']}").status_code == 404
    assert [
        len(_audits(db_session, f"exam_subject.{a}")) for a in ("created", "updated", "deleted")
    ] == [
        2,
        1,
        1,
    ]
    deleted = _audits(db_session, "exam_subject.deleted")[0].details
    assert deleted["qualifying_marks"] is None
    assert deleted["max_marks"] == "90.00"


def test_other_schools_subjects_are_indistinguishable_from_unknown(eworld: ExamWorld) -> None:
    """An inactive subject of another school must not reveal itself as "inactive"."""
    response = eworld.client.patch(
        f"{API}/subjects/{eworld.ids['math_b']}",
        headers=eworld.a.w.admin_b,
        json={"is_active": False},
    )
    assert response.status_code == 200, response.text
    exam_id = eworld.exam_id()
    for subject in ("math_b", "sci_b"):
        refused = eworld.component(exam_id, subject)
        assert refused.status_code == 422
        assert refused.json()["error"]["message"] == "Subject not found."


def test_subject_removal_is_draft_only(eworld: ExamWorld, db_session: Session) -> None:
    exam_id = eworld.exam_id()
    math = eworld.component_id(exam_id, "math_a")
    sci = eworld.component_id(exam_id, "sci_a")
    assert eworld.action(exam_id, "schedule").status_code == 200
    blocked = eworld.client.delete(f"{API}/exams/{exam_id}/subjects/{math}", headers=eworld.admin)
    assert blocked.status_code == 409
    assert "return a scheduled exam to draft first" in blocked.json()["error"]["message"]
    assert len(eworld.items(f"/exams/{exam_id}/subjects")) == 2
    assert eworld.action(exam_id, "return-to-draft").status_code == 200
    for component in (math, sci):
        response = eworld.client.delete(
            f"{API}/exams/{exam_id}/subjects/{component}", headers=eworld.admin
        )
        assert response.status_code == 204
    # Without subjects the exam cannot be scheduled (and so never reaches marks entry).
    assert eworld.action(exam_id, "schedule").status_code == 422
    assert len(_audits(db_session, "exam_subject.deleted")) == 2
    faculty = eworld.client.delete(
        f"{API}/exams/{exam_id}/subjects/{eworld.component_id(exam_id)}", headers=eworld.a.f
    )
    assert faculty.status_code == 403


def test_subject_with_marks_is_never_removed(eworld: ExamWorld, db_session: Session) -> None:
    """Marks cannot exist on a draft exam (triggers). Disabling that guard inside this
    rolled-back transaction proves the service check and ON DELETE RESTRICT still hold."""
    exam_id, comps = eworld.setup(("math_a",))
    eworld.mark_id(exam_id, comps["math_a"], "a1")
    db_session.execute(text("ALTER TABLE exams DISABLE TRIGGER trg_exams_lifecycle"))
    db_session.execute(text("UPDATE exams SET status = 'draft' WHERE id = :e"), {"e": exam_id})
    db_session.execute(text("ALTER TABLE exams ENABLE TRIGGER trg_exams_lifecycle"))
    path = f"{API}/exams/{exam_id}/subjects/{comps['math_a']}"
    refused = eworld.client.delete(path, headers=eworld.admin)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == (
        "Marks reference this subject; it cannot be removed."
    )
    with pytest.raises(IntegrityError) as exc_info, db_session.begin_nested():
        db_session.execute(text("DELETE FROM exam_subjects WHERE id = :c"), {"c": comps["math_a"]})
    assert exc_info.value.orig.diag.constraint_name == "fk_exam_marks_component"  # type: ignore[union-attr]
    assert _audits(db_session, "exam_subject.deleted") == []


def test_components_frozen_after_scheduling_ends(eworld: ExamWorld) -> None:
    exam_id, components = eworld.setup(("math_a",))
    path = f"{API}/exams/{exam_id}/subjects/{components['math_a']}"
    assert eworld.component(exam_id, "sci_a").status_code == 409
    assert (
        eworld.client.patch(path, headers=eworld.admin, json={"max_marks": 50}).status_code == 409
    )
    assert eworld.client.delete(path, headers=eworld.admin).status_code == 409


# ================================================================== lifecycle
def test_lifecycle_transitions(eworld: ExamWorld, db_session: Session) -> None:
    exam_id = eworld.exam_id()
    scheduled = eworld.action(exam_id, "schedule")
    assert scheduled.status_code == 422  # needs a subject
    assert scheduled.json()["error"]["message"] == "Add at least one subject before scheduling."
    eworld.component_id(exam_id)
    for verb, expected_status, expected in (
        ("open-marks-entry", 409, "draft"),
        ("schedule", 200, "scheduled"),
        ("schedule", 409, "scheduled"),
        ("return-to-draft", 200, "draft"),
        ("schedule", 200, "scheduled"),
        ("open-marks-entry", 200, "marks_entry"),
        ("open-marks-entry", 409, "marks_entry"),
        ("return-to-draft", 409, "marks_entry"),
    ):
        response = eworld.action(exam_id, verb)
        assert response.status_code == expected_status, (verb, response.text)
        assert eworld.get(f"/exams/{exam_id}").json()["status"] == expected
    assert eworld.action(exam_id, "reopen", body={"reason": "x"}).status_code == 409
    assert [
        len(_audits(db_session, f"exam.{a}"))
        for a in ("scheduled", "returned_to_draft", "marks_entry_opened")
    ] == [2, 1, 1]


def test_cancel_requires_reason_and_is_terminal(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, components = eworld.setup(("math_a",))
    eworld.mark_id(exam_id, components["math_a"], "a1")
    assert eworld.action(exam_id, "cancel").status_code == 422
    assert eworld.action(exam_id, "cancel", body={"reason": "  "}).status_code == 422
    cancelled = eworld.action(exam_id, "cancel", body={"reason": "Exam postponed"})
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert (body["status"], body["cancellation_reason"]) == ("cancelled", "Exam postponed")
    assert body["cancelled_by_user_id"]
    assert body["cancelled_at"]
    # Marks are kept but nothing else can happen.
    assert len(eworld.items(f"/exams/{exam_id}/subjects/{components['math_a']}/marks")) == 1
    for verb in ("schedule", "open-marks-entry", "return-to-draft", "publish"):
        assert eworld.action(exam_id, verb).status_code == 409, verb
    assert eworld.action(exam_id, "cancel", body={"reason": "again"}).status_code == 409
    assert (
        eworld.client.patch(
            f"{API}/exams/{exam_id}", headers=eworld.admin, json={"pass_percentage": 10}
        ).status_code
        == 409
    )
    assert eworld.mark(exam_id, components["math_a"], "a2").status_code == 409
    audit = _audits(db_session, "exam.cancelled")[0].details
    assert (audit["reason"], audit["previous_status"]) == ("Exam postponed", "marks_entry")


def test_management_and_faculty_cannot_run_lifecycle(
    eworld: ExamWorld, auth_headers: HeadersFactory
) -> None:
    exam_id, _ = eworld.setup(("math_a",), open_entry=False)
    management = auth_headers(RoleName.MANAGEMENT)
    for headers, expected in ((management, 403), (eworld.a.f, 403)):
        assert eworld.action(exam_id, "open-marks-entry", headers).status_code == expected
    assert (
        eworld.action(
            exam_id, "schedule", auth_headers(RoleName.SCHOOL_ADMIN, SCHOOL_B)
        ).status_code
        == 404
    )
