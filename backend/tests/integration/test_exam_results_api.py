"""Result calculation, publication, reopening and report cards over HTTP."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_B, HeadersFactory
from tests.integration.conftest import API, ExamWorld

pytestmark = pytest.mark.db

BANDS = [
    {"grade": "A", "min_percentage": 75},
    {"grade": "B", "min_percentage": 50},
    {"grade": "C", "min_percentage": 0},
]


def _audits(db_session: Session, action: str) -> list[AuditLog]:
    return list(db_session.scalars(select(AuditLog).where(AuditLog.action == action)))


def _graded_exam(eworld: ExamWorld, **exam: Any) -> tuple[str, dict[str, str]]:
    """Math (max 100, pass 35) and science (max 50, pass 20), all marks entered:

    * a1: 80 + 40      = 120 / 150 = 80.00 % -> pass, A
    * a2: 30 + absent  =  30 / 150 = 20.00 % -> fail (math below 35, science absent), C
    * ab: exempt + 45  =  45 /  50 = 90.00 % -> pass, A
    """
    exam_id = eworld.exam_id(grade_bands=BANDS, **exam)
    comps = {
        "math_a": eworld.component_id(exam_id, "math_a"),
        "sci_a": eworld.component_id(exam_id, "sci_a", max_marks=50, pass_marks=20),
    }
    eworld.open(exam_id)
    for subject, entries in (
        (
            "math_a",
            [
                {"student_id": "a1", "marks_obtained": 80},
                {"student_id": "a2", "marks_obtained": 30},
                {"student_id": "ab", "status": "exempt"},
            ],
        ),
        (
            "sci_a",
            [
                {"student_id": "a1", "marks_obtained": 40},
                {"student_id": "a2", "status": "absent"},
                {"student_id": "ab", "marks_obtained": 45},
            ],
        ),
    ):
        assert eworld.bulk(exam_id, comps[subject], entries).status_code == 201
    return exam_id, comps


def _by_student(eworld: ExamWorld, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    keys = {eworld.sid(k): k for k in ("a1", "a2", "ab")}
    return {keys[item["student"]["id"]]: item for item in items}


# ======================================================================= preview
def test_live_results(eworld: ExamWorld) -> None:
    exam_id, _ = _graded_exam(eworld)
    results = _by_student(eworld, eworld.items(f"/exams/{exam_id}/results"))
    summary = {
        key: (r["total_obtained"], r["total_max"], r["percentage"], r["result"], r["grade"])
        for key, r in results.items()
    }
    assert summary == {
        "a1": (120, 150, 80, "pass", "A"),
        "a2": (30, 150, 20, "fail", "C"),
        "ab": (45, 50, 90, "pass", "A"),
    }
    a2 = {line["subject_code"]: line for line in results["a2"]["subjects"]}
    assert (a2["MATH"]["result"], a2["MATH"]["marks_obtained"], a2["MATH"]["grade"]) == (
        "fail",
        30,
        "C",
    )
    assert (a2["SCI"]["status"], a2["SCI"]["result"], a2["SCI"]["marks_obtained"]) == (
        "absent",
        "absent",
        None,
    )
    assert results["ab"]["section_id"] == eworld.ids["section_a_b"]
    assert eworld.get(f"/exams/{exam_id}/results", page_size=2).json()["total"] == 3


def test_live_results_show_missing_marks_as_pending(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup()
    eworld.mark_id(exam_id, comps["math_a"], "a1", 0)
    results = _by_student(eworld, eworld.items(f"/exams/{exam_id}/results"))
    a1 = results["a1"]
    assert (a1["result"], a1["percentage"], a1["total_obtained"]) == ("incomplete", None, 0)
    lines = {line["subject_code"]: line["status"] for line in a1["subjects"]}
    assert lines == {"MATH": "present", "SCI": "pending"}  # zero is not missing
    assert results["a2"]["result"] == "incomplete"
    assert results["a2"]["total_max"] == 0


def test_overall_pass_percentage(eworld: ExamWorld) -> None:
    exam_id, _ = _graded_exam(eworld, pass_percentage=85)
    results = _by_student(eworld, eworld.items(f"/exams/{exam_id}/results"))
    assert {k: r["result"] for k, r in results.items()} == {
        "a1": "fail",
        "a2": "fail",
        "ab": "pass",
    }


def test_results_are_for_school_staff(eworld: ExamWorld, auth_headers: HeadersFactory) -> None:
    exam_id, _ = _graded_exam(eworld)
    path = f"/exams/{exam_id}/results"
    assert eworld.get(path, auth_headers(RoleName.MANAGEMENT)).status_code == 200
    assert eworld.get(path, eworld.a.f).status_code == 403
    assert eworld.get(path, eworld.parent_of("a1")).status_code == 404
    assert eworld.get(path, eworld.student_headers("A1")).status_code == 404
    assert eworld.get(f"/exams/{exam_id}/publications", eworld.a.f).status_code == 403


# =================================================================== publication
def test_publish_refuses_missing_marks_unless_allowed(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup()
    eworld.mark_id(exam_id, comps["math_a"], "a1", 70)
    refused = eworld.publish(exam_id)
    assert refused.status_code == 422
    assert "3 student(s) have no entry for 5 exam subject(s)" in refused.json()["error"]["message"]
    assert eworld.get(f"/exams/{exam_id}").json()["status"] == "marks_entry"
    published = eworld.publish(exam_id, allow_incomplete=True)
    assert published.status_code == 201, published.text
    assert (published.json()["report_cards"], published.json()["incomplete"]) == (3, 3)
    cards = eworld.items(f"/exams/{exam_id}/report-cards")
    assert {card["result"] for card in cards} == {"incomplete"}


def test_publish_needs_eligible_students(eworld: ExamWorld) -> None:
    # Nobody was enrolled yet on the exam date.
    exam_id, _ = eworld.setup(("math_a",), start="2026-06-02", end="2026-06-05")
    response = eworld.publish(exam_id)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "No students are eligible for this exam."


def test_publish_freezes_report_cards(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, comps = _graded_exam(eworld, description="Term 1")
    response = eworld.publish(exam_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["version"], body["status"], body["report_cards"], body["incomplete"]) == (
        1,
        "active",
        3,
        0,
    )
    assert eworld.get(f"/exams/{exam_id}").json()["status"] == "published"
    cards = _by_student(eworld, eworld.items(f"/exams/{exam_id}/report-cards"))
    a1 = cards["a1"]
    assert (a1["total_obtained"], a1["percentage"], a1["result"], a1["grade"]) == (
        120,
        80,
        "pass",
        "A",
    )
    assert (a1["school_name"], a1["academic_year_name"], a1["class_name"], a1["section_name"]) == (
        "School A",
        "2026-27",
        "class",
        "A",
    )
    assert cards["ab"]["section_name"] == "B"
    assert a1["exam"]["name"] == "Midterm"
    assert a1["exam"]["grade_bands"][0] == {"grade": "A", "min_percentage": 75}
    assert a1["student"]["admission_number"] == "A1"
    assert (a1["publication_version"], a1["publication_status"]) == (1, "active")
    assert [line["subject_name"] for line in a1["subjects"]] == ["math", "sci"]

    # Published results are read-only.
    mark_id = eworld.items(f"/exams/{exam_id}/subjects/{comps['math_a']}/marks")[0]["id"]
    for response in (
        eworld.mark(exam_id, comps["math_a"], "a1"),
        eworld.correct(mark_id, marks_obtained=1, reason="x"),
        eworld.publish(exam_id),
        eworld.action(exam_id, "cancel", body={"reason": "x"}),
        eworld.client.patch(
            f"{API}/exams/{exam_id}", headers=eworld.admin, json={"pass_percentage": 1}
        ),
        eworld.component(exam_id, "sci_a"),
    ):
        assert response.status_code == 409, response.text
    audit = _audits(db_session, "exam.published")[0].details
    assert (audit["version"], audit["report_cards"], audit["incomplete"]) == (1, 3, 0)


def test_reopen_correct_and_republish_keeps_history(eworld: ExamWorld, db_session: Session) -> None:
    exam_id, comps = _graded_exam(eworld)
    assert eworld.publish(exam_id).status_code == 201
    parent = eworld.parent_of("a2")
    v1 = eworld.items("/report-cards", parent)
    assert [c["percentage"] for c in v1] == [20]

    assert eworld.action(exam_id, "reopen").status_code == 422  # reason required
    reopened = eworld.action(exam_id, "reopen", body={"reason": "Science script found"})
    assert reopened.status_code == 200, reopened.text
    assert (reopened.json()["status"], reopened.json()["withdrawal_reason"]) == (
        "withdrawn",
        "Science script found",
    )
    assert eworld.get(f"/exams/{exam_id}").json()["status"] == "marks_entry"
    # Withdrawn results disappear for parents and students...
    assert eworld.items("/report-cards", parent) == []
    assert eworld.get(f"/report-cards/{v1[0]['id']}", parent).status_code == 404
    # ...but staff keep the withdrawn version.
    staff_v1 = eworld.get(f"/report-cards/{v1[0]['id']}").json()
    assert staff_v1["publication_status"] == "withdrawn"
    assert eworld.items(f"/exams/{exam_id}/report-cards") == []

    sci = eworld.items(
        f"/exams/{exam_id}/subjects/{comps['sci_a']}/marks", student_id=eworld.sid("a2")
    )[0]
    corrected = eworld.correct(
        sci["id"], status="present", marks_obtained=30, reason="Script found"
    )
    assert corrected.status_code == 200, corrected.text
    again = eworld.publish(exam_id)
    assert again.status_code == 201
    assert again.json()["version"] == 2

    v2 = eworld.items("/report-cards", parent)
    assert [(c["publication_version"], c["percentage"], c["result"]) for c in v2] == [
        (2, 40, "fail")  # 60 / 150; math is still below its passing marks
    ]
    # The first publication is unchanged: corrections never alter a published snapshot.
    old = eworld.get(f"/report-cards/{v1[0]['id']}").json()
    assert (old["percentage"], old["publication_version"]) == (20, 1)
    assert {line["subject_code"]: line["status"] for line in old["subjects"]}["SCI"] == "absent"
    versions = eworld.get(f"/exams/{exam_id}/publications").json()
    assert [(p["version"], p["status"], p["report_cards"]) for p in versions] == [
        (1, "withdrawn", 3),
        (2, "active", 3),
    ]
    assert len(eworld.items(f"/exams/{exam_id}/report-cards", version=1)) == 3
    reopen_audit = _audits(db_session, "exam.reopened")[0].details
    assert (reopen_audit["version"], reopen_audit["reason"]) == (1, "Science script found")


def test_who_publishes(eworld: ExamWorld, auth_headers: HeadersFactory) -> None:
    exam_id, _ = _graded_exam(eworld)
    assert eworld.publish(exam_id, eworld.a.f).status_code == 403
    assert eworld.publish(exam_id, eworld.parent_of("a1")).status_code == 403
    assert eworld.publish(exam_id, auth_headers(RoleName.SCHOOL_ADMIN, SCHOOL_B)).status_code == 404
    management = auth_headers(RoleName.MANAGEMENT)
    assert eworld.publish(exam_id, management).status_code == 201
    assert eworld.action(exam_id, "reopen", management, {"reason": "Recheck"}).status_code == 200
    assert eworld.action(exam_id, "reopen", management, {"reason": "Again"}).status_code == 409
    root = auth_headers(RoleName.SUPER_ADMIN)
    assert eworld.publish(exam_id, root).status_code == 201


# ===================================================================== visibility
def test_parent_and_student_report_card_visibility(eworld: ExamWorld) -> None:
    exam_id, _ = _graded_exam(eworld)
    parent = eworld.parent_of("a1")
    student = eworld.student_headers("A1")
    other_student = eworld.student_headers("A2")
    # Nothing is visible before publication.
    for headers in (parent, student):
        assert eworld.items("/report-cards", headers) == []
    assert eworld.publish(exam_id).status_code == 201
    staff_cards = _by_student(eworld, eworld.items(f"/exams/{exam_id}/report-cards"))
    for headers in (parent, student):
        mine = eworld.items("/report-cards", headers)
        assert [c["student"]["id"] for c in mine] == [eworld.sid("a1")]
        assert eworld.get(f"/report-cards/{staff_cards['a1']['id']}", headers).status_code == 200
        for other in ("a2", "ab"):
            assert (
                eworld.get(f"/report-cards/{staff_cards[other]['id']}", headers).status_code == 404
            )
            assert eworld.items("/report-cards", headers, student_id=eworld.sid(other)) == []
        # Staff-only views stay closed.
        assert eworld.get(f"/exams/{exam_id}/report-cards", headers).status_code == 404
        assert eworld.get(f"/exams/{exam_id}/publications", headers).status_code == 404
    assert [c["student"]["id"] for c in eworld.items("/report-cards", other_student)] == [
        eworld.sid("a2")
    ]
    assert eworld.items("/report-cards", eworld.a.f) == []
    assert eworld.get(f"/report-cards/{staff_cards['a1']['id']}", eworld.a.f).status_code == 404


def test_report_card_filters(eworld: ExamWorld, auth_headers: HeadersFactory) -> None:
    exam_id, _ = _graded_exam(eworld)
    assert eworld.publish(exam_id).status_code == 201
    assert len(eworld.items("/report-cards")) == 3
    assert len(eworld.items("/report-cards", student_id=eworld.sid("a1"))) == 1
    assert len(eworld.items("/report-cards", exam_id=exam_id)) == 3
    assert len(eworld.items("/report-cards", academic_year_id=eworld.ids["year_a_prev"])) == 0
    assert len(eworld.items("/report-cards", auth_headers(RoleName.MANAGEMENT))) == 3
    assert eworld.items("/report-cards", auth_headers(RoleName.SCHOOL_ADMIN, SCHOOL_B)) == []
