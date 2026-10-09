"""IDOR, cross-school and filter attacks against exams, marks and report cards.

Every identifier an attacker can control (exam, component, mark, report card, student,
class, section, subject, academic year, school header) is swapped for one outside the
caller's scope. The answer must be 404 (or an empty page for filters), exactly as for an
unknown ID, and nothing may be written.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.exams import ExamMark
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import API, ExamWorld

pytestmark = pytest.mark.db


def _school_b_published(eworld: ExamWorld) -> dict[str, str]:
    """A published school-B exam with one mark and report card."""
    b = eworld.a.w.admin_b
    exam_id = eworld.exam_id(b, year="year_b", cls="class_b")
    component = eworld.component_id(exam_id, "math_b", headers=b)
    for verb in ("schedule", "open-marks-entry"):
        assert eworld.action(exam_id, verb, b).status_code == 200
    mark = eworld.mark(exam_id, component, "b", 50, b)
    assert mark.status_code == 201, mark.text
    assert eworld.publish(exam_id, b).status_code == 201
    card = eworld.items(f"/exams/{exam_id}/report-cards", b)[0]["id"]
    return {"exam": exam_id, "component": component, "mark": mark.json()["id"], "card": card}


def _marks(db_session: Session) -> int:
    return int(db_session.scalar(select(func.count()).select_from(ExamMark)) or 0)


def test_school_a_cannot_reach_school_b_by_id(eworld: ExamWorld, db_session: Session) -> None:
    b = _school_b_published(eworld)
    exam, comp = b["exam"], b["component"]
    before = _marks(db_session)
    requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"/exams/{exam}", None),
        ("PATCH", f"/exams/{exam}", {"name": "Hijacked"}),
        ("POST", f"/exams/{exam}/schedule", None),
        ("POST", f"/exams/{exam}/cancel", {"reason": "x"}),
        ("POST", f"/exams/{exam}/publish", None),
        ("POST", f"/exams/{exam}/reopen", {"reason": "x"}),
        ("GET", f"/exams/{exam}/results", None),
        ("GET", f"/exams/{exam}/publications", None),
        ("GET", f"/exams/{exam}/report-cards", None),
        ("GET", f"/exams/{exam}/subjects", None),
        ("POST", f"/exams/{exam}/subjects", {"subject_id": eworld.ids["math_a"], "max_marks": 5}),
        ("GET", f"/exams/{exam}/subjects/{comp}", None),
        ("PATCH", f"/exams/{exam}/subjects/{comp}", {"max_marks": 5}),
        ("DELETE", f"/exams/{exam}/subjects/{comp}", None),
        ("GET", f"/exams/{exam}/subjects/{comp}/students", None),
        ("GET", f"/exams/{exam}/subjects/{comp}/marks", None),
        (
            "POST",
            f"/exams/{exam}/subjects/{comp}/marks",
            {"student_id": eworld.sid("b"), "marks_obtained": 99},
        ),
        (
            "POST",
            f"/exams/{exam}/subjects/{comp}/marks/bulk",
            {"marks": [{"student_id": eworld.sid("b"), "marks_obtained": 99}]},
        ),
        ("GET", f"/exam-marks/{b['mark']}", None),
        ("PATCH", f"/exam-marks/{b['mark']}", {"marks_obtained": 99, "reason": "x"}),
        ("GET", f"/exam-marks/{b['mark']}/history", None),
        ("GET", f"/report-cards/{b['card']}", None),
    ]
    for method, path, body in requests:
        response = eworld.client.request(method, f"{API}{path}", headers=eworld.admin, json=body)
        assert response.status_code == 404, (method, path, response.text)
    assert _marks(db_session) == before
    # Filters with school B identifiers only narrow (to nothing).
    for path, params in (
        ("/exams", {"class_id": eworld.ids["class_b"]}),
        ("/exams", {"academic_year_id": eworld.ids["year_b"]}),
        ("/report-cards", {"student_id": eworld.sid("b")}),
        ("/report-cards", {"exam_id": exam}),
        ("/report-cards", {"academic_year_id": eworld.ids["year_b"]}),
    ):
        assert eworld.items(path, **params) == [], (path, params)


def test_school_selection_cannot_be_forged(eworld: ExamWorld, auth_headers: HeadersFactory) -> None:
    b = _school_b_published(eworld)
    forged = {**eworld.admin, "X-School-ID": str(SCHOOL_B)}
    assert eworld.get(f"/exams/{b['exam']}", forged).status_code == 404
    assert eworld.get("/exams", forged).status_code == 404
    parent = {**eworld.parent_of("a1"), "X-School-ID": str(SCHOOL_B)}
    assert eworld.get(f"/report-cards/{b['card']}", parent).status_code == 404
    # SUPER_ADMIN reaches exactly the selected school.
    root_a = auth_headers(RoleName.SUPER_ADMIN, SCHOOL_A)
    root_b = auth_headers(RoleName.SUPER_ADMIN, SCHOOL_B)
    assert eworld.get(f"/exams/{b['exam']}", root_a).status_code == 404
    assert eworld.get(f"/exams/{b['exam']}", root_b).status_code == 200
    assert eworld.get(f"/report-cards/{b['card']}", root_b).status_code == 200
    assert eworld.get(f"/report-cards/{b['card']}", root_a).status_code == 404
    unselected = auth_headers(RoleName.SUPER_ADMIN, select_school=False)
    assert eworld.get("/report-cards", unselected).status_code == 400


def test_components_and_marks_are_bound_to_their_exam(eworld: ExamWorld) -> None:
    first, first_comps = eworld.setup(("math_a",))
    second, second_comps = eworld.setup(("math_a",), name="Final")
    swapped = f"/exams/{first}/subjects/{second_comps['math_a']}"
    assert eworld.get(swapped).status_code == 404
    assert eworld.get(f"{swapped}/marks").status_code == 404
    assert eworld.get(f"{swapped}/students").status_code == 404
    assert eworld.mark(first, second_comps["math_a"], "a1").status_code == 404
    assert eworld.mark(second, first_comps["math_a"], "a1").status_code == 404
    random_id = str(uuid.uuid4())
    for path in (
        f"/exams/{random_id}",
        f"/exams/{first}/subjects/{random_id}",
        f"/exam-marks/{random_id}",
        f"/report-cards/{random_id}",
    ):
        assert eworld.get(path).status_code == 404, path


def test_faculty_cannot_widen_their_scope(eworld: ExamWorld) -> None:
    a = eworld.a
    class_wide, _ = eworld.setup(("math_a",))
    section_b, section_b_comps = eworld.setup(("math_a",), sec="section_a_b", name="UT-B")
    other_class, _ = eworld.setup(("math_a",), cls="class2_a", name="Other")
    # F teaches section A: sees the class-wide exam, not section B's nor class 2's.
    assert {e["id"] for e in eworld.items("/exams", a.f)} == {class_wide}
    for exam_id in (section_b, other_class):
        assert eworld.get(f"/exams/{exam_id}", a.f).status_code == 404
    for params in (
        {"section_id": eworld.ids["section_a_b"]},
        {"class_id": eworld.ids["class2_a"]},
        {"status": "marks_entry"},
    ):
        assert {e["id"] for e in eworld.items("/exams", a.f, **params)} <= {class_wide}
    assert eworld.mark(section_b, section_b_comps["math_a"], "ab", 10, a.f).status_code == 404
    # H teaches section B and sees it.
    assert {e["id"] for e in eworld.items("/exams", a.h)} == {class_wide, section_b}
    # Faculty never configure exams, even ones they can see.
    assert eworld.action(class_wide, "cancel", a.f, {"reason": "x"}).status_code == 403
    assert eworld.component(class_wide, "sci_a", a.f).status_code in (403, 409)


def test_parent_cannot_reach_another_childs_results(eworld: ExamWorld) -> None:
    exam_id, comps = eworld.setup()
    for subject in comps:
        eworld.bulk(
            exam_id,
            comps[subject],
            [{"student_id": k, "marks_obtained": 50} for k in ("a1", "a2", "ab")],
        )
    assert eworld.publish(exam_id).status_code == 201
    cards = {c["student"]["id"]: c["id"] for c in eworld.items(f"/exams/{exam_id}/report-cards")}
    parent = eworld.parent_of("a1")
    for key in ("a2", "ab"):
        assert eworld.get(f"/report-cards/{cards[eworld.sid(key)]}", parent).status_code == 404
    for params in (
        {"student_id": eworld.sid("a2")},
        {"exam_id": exam_id, "student_id": eworld.sid("ab")},
    ):
        assert eworld.items("/report-cards", parent, **params) == []
    # A deactivated link removes access immediately.
    links = eworld.get(f"/students/{eworld.sid('a1')}/parents").json()
    parent_id = links[0]["parent_id"]
    assert (
        eworld.client.patch(
            f"{API}/students/{eworld.sid('a1')}/parents/{parent_id}",
            headers=eworld.admin,
            json={"is_active": False},
        ).status_code
        == 200
    )
    assert eworld.items("/report-cards", parent) == []
    assert eworld.get(f"/report-cards/{cards[eworld.sid('a1')]}", parent).status_code == 404
