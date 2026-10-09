"""Attendance authorization and IDOR: faculty assignment scope (section, subject, year,
school, effective window, daily roll calls), parent -> children, student -> self, school
isolation, and filter/ID manipulation on every list and detail endpoint."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import AttendanceWorld

pytestmark = pytest.mark.db
R = RoleName


@dataclass
class Scene:
    a: AttendanceWorld
    math: str  # year_a / section_a / math, created by F
    sci: str  # year_a / section_a / sci (G's subject)
    daily: str  # year_a / section_a daily roll call (staff)
    math_b: str  # year_a / section_a_b / math (H's section)
    daily_b: str  # year_a / section_a_b daily roll call
    school_b: str  # a session in school B
    rec: dict[str, str]  # record ids


@pytest.fixture
def scene(aworld: AttendanceWorld) -> Scene:
    a = aworld
    math = a.session_id(a.f)
    sci = a.session_id(a.g, subject="sci_a")
    daily = a.session_id(subject=None)
    math_b = a.session_id(a.h, sec="section_a_b")
    daily_b = a.session_id(subject=None, sec="section_a_b")
    school_b = a.session_id(
        a.w.admin_b, year="year_b", cls="class_b", sec="section_b", subject="math_b"
    )
    rec = {
        "math_a1": a.record_id(math, "a1"),
        "math_a2": a.record_id(math, "a2", "absent"),
        "sci_a1": a.record_id(sci, "a1", "late"),
        "daily_a1": a.record_id(daily, "a1"),
        "daily_a2": a.record_id(daily, "a2", "excused"),
        "math_b_ab": a.record_id(math_b, "ab"),
        "daily_b_ab": a.record_id(daily_b, "ab", "absent"),
    }
    b_rec = a.client.post(
        f"/api/v1/attendance/sessions/{school_b}/records",
        headers=a.w.admin_b,
        json={"student_id": a.s["b"], "status": "present"},
    )
    assert b_rec.status_code == 201
    rec["school_b"] = b_rec.json()["id"]
    return Scene(a, math, sci, daily, math_b, daily_b, school_b, rec)


# ===================================================================== faculty: create
def test_faculty_creates_session_for_own_assignment(aworld: AttendanceWorld) -> None:
    response = aworld.session(aworld.f, date="2026-07-20")
    assert response.status_code == 201, response.text
    assert response.json()["created_by_user_id"] != response.json()["school_id"]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"sec": "section_a_b"}, "unassigned section"),
        ({"subject": "sci_a"}, "another subject"),
        ({"cls": "class2_a", "sec": "section2_a"}, "another class"),
        ({"cls": "class2_a"}, "own section under another class: judged on the exact placement"),
        ({"year": "year_a_prev", "date": "2026-03-10"}, "another academic year"),
        ({"year": "year_b", "cls": "class_b", "sec": "section_b", "subject": "math_b"}, "school B"),
        ({"subject": None}, "daily roll call"),
    ],
)
def test_faculty_cannot_create_outside_assignment(
    aworld: AttendanceWorld, override: dict[str, Any], reason: str
) -> None:
    response = aworld.session(aworld.f, **override)
    assert response.status_code == 403, reason


def test_faculty_refusal_does_not_reveal_existence(aworld: AttendanceWorld) -> None:
    """Unknown and existing-but-unassigned sections get the same answer."""
    real = aworld.session(aworld.f, sec="section_a_b")
    aworld.w.ids["ghost"] = str(uuid.uuid4())
    ghost = aworld.session(aworld.f, sec="ghost")
    assert (real.status_code, real.json()["error"]["message"]) == (
        ghost.status_code,
        ghost.json()["error"]["message"],
    )


def test_assignment_effective_window_limits_dates(aworld: AttendanceWorld) -> None:
    w = aworld.w
    late_teacher = w.faculty(email="window@school.test").json()
    w.assign(late_teacher["id"], subject="sci_a", sec="section_a_b", effective_from="2026-08-01")
    headers = w.faculty_headers("window@school.test")
    assert aworld.session(headers, subject="sci_a", sec="section_a_b").status_code == 403
    allowed = aworld.session(headers, subject="sci_a", sec="section_a_b", date="2026-08-03")
    assert allowed.status_code == 201
    # Sessions before the window (e.g. the predecessor's) stay invisible.
    earlier = aworld.session_id(subject="sci_a", sec="section_a_b")
    assert aworld.get(f"/sessions/{earlier}", headers).status_code == 404


def test_assignment_window_end_is_enforced_for_any_stored_session(
    aworld: AttendanceWorld, db_session: Session
) -> None:
    """Defence in depth: even a session stored past the assignment's end date (only
    possible by bypassing the service, which refuses future dates) stays invisible."""
    w = aworld.w
    today = datetime.now(UTC).date()
    teacher = w.faculty(email="ending@school.test").json()
    w.assign(
        teacher["id"],
        subject="sci_a",
        sec="section_a_b",
        effective_to=str(today + timedelta(days=3)),
    )
    headers = w.faculty_headers("ending@school.test")
    inside = aworld.session_id(subject="sci_a", sec="section_a_b", date=str(today))
    beyond = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO attendance_sessions (id, school_id, academic_year_id, class_id, "
            "section_id, subject_id, session_date, created_by_user_id) SELECT :id, :s, :y, :c, "
            ":sec, :sub, :d, user_id FROM faculty WHERE id = :f"
        ),
        {
            "id": beyond,
            "s": SCHOOL_A,
            "y": w.ids["year_a"],
            "c": w.ids["class_a"],
            "sec": w.ids["section_a_b"],
            "sub": w.ids["sci_a"],
            "d": today + timedelta(days=10),
            "f": teacher["id"],
        },
    )
    assert aworld.ids("/sessions", headers) == {inside}
    assert aworld.get(f"/sessions/{beyond}", headers).status_code == 404


# ======================================================================= faculty: read
def test_faculty_session_visibility(scene: Scene) -> None:
    a = scene.a
    # F: own subject session + daily roll call of a section they teach. Nothing else.
    assert a.ids("/sessions", a.f) == {scene.math, scene.daily}
    assert a.ids("/sessions", a.g) == {scene.sci, scene.daily}
    assert a.ids("/sessions", a.h) == {scene.math_b, scene.daily_b}
    for sid in (scene.sci, scene.math_b, scene.daily_b, scene.school_b):
        assert a.get(f"/sessions/{sid}", a.f).status_code == 404
        assert a.get(f"/sessions/{sid}/records", a.f).status_code == 404
        assert a.get(f"/sessions/{sid}/unmarked-students", a.f).status_code == 404


@pytest.mark.parametrize(
    ("param", "key"),
    [
        ("section_id", "section_a_b"),
        ("subject_id", "sci_a"),
        ("class_id", "class2_a"),
        ("academic_year_id", "year_a_prev"),
        ("section_id", "section_b"),
    ],
)
def test_faculty_filters_cannot_widen(scene: Scene, param: str, key: str) -> None:
    """GET ...?section_id=<another section> must reveal nothing."""
    a = scene.a
    value = a.w.ids[key]
    assert a.ids("/sessions", a.f, **{param: value}) == set()
    assert a.ids("/records", a.f, **{param: value}) == set()


def test_faculty_record_visibility(scene: Scene) -> None:
    a, rec = scene.a, scene.rec
    assert a.ids("/records", a.f) == {
        rec["math_a1"],
        rec["math_a2"],
        rec["daily_a1"],
        rec["daily_a2"],
    }
    # Swapping the student filter to a student they cannot reach yields nothing.
    assert a.ids("/records", a.f, student_id=a.s["ab"]) == set()
    assert a.ids("/records", a.f, student_id=a.s["a1"]) == {rec["math_a1"], rec["daily_a1"]}
    for key in ("sci_a1", "math_b_ab", "daily_b_ab", "school_b"):
        assert a.get(f"/records/{rec[key]}", a.f).status_code == 404, key
        assert a.get(f"/records/{rec[key]}/history", a.f).status_code == 404, key


# ====================================================================== faculty: write
def test_faculty_writes_only_in_own_subject_sessions(scene: Scene) -> None:
    a, rec = scene.a, scene.rec
    ok = a.patch(f"/records/{rec['math_a1']}", {"status": "late", "reason": "Arrived 9:10"}, a.f)
    assert ok.status_code == 200
    assert a.patch(f"/sessions/{scene.math}", {"end_time": "10:00"}, a.f).status_code == 200
    # Visible but not theirs to change: the daily roll call is staff-only.
    # Authorisation is decided before record state, so nothing about the roll call leaks.
    assert a.mark(scene.daily, "a2", headers=a.f).status_code == 403
    fresh_daily = a.session_id(subject=None, date="2026-07-21")
    assert a.mark(fresh_daily, "a1", headers=a.f).status_code == 403
    assert a.bulk(fresh_daily, {"a1": "present"}, a.f).status_code == 403
    assert a.patch(f"/records/{rec['daily_a1']}", {"remarks": "x"}, a.f).status_code == 403
    assert a.patch(f"/sessions/{scene.daily}", {"status": "inactive"}, a.f).status_code == 403
    # Invisible sessions/records: 404 for every write.
    assert a.mark(scene.sci, "a2", headers=a.f).status_code == 404
    assert a.bulk(scene.math_b, {"ab": "present"}, a.f).status_code == 404
    assert a.patch(f"/records/{rec['sci_a1']}", {"remarks": "x"}, a.f).status_code == 404
    assert a.patch(f"/records/{rec['school_b']}", {"remarks": "x"}, a.f).status_code == 404
    assert a.patch(f"/sessions/{scene.sci}", {"status": "inactive"}, a.f).status_code == 404


def test_faculty_cannot_mark_students_of_other_sections_in_own_session(scene: Scene) -> None:
    a = scene.a
    sid = a.session_id(a.f, date="2026-07-22")
    for student in ("ab", "c2", "prev", "b"):
        assert a.mark(sid, student, headers=a.f).status_code == 422, student
    response = a.bulk(sid, {"a1": "present", "ab": "present"}, a.f)
    assert response.status_code == 422


def test_ended_assignment_revokes_attendance_access(scene: Scene) -> None:
    a = scene.a
    assignments = a.client.get(
        "/api/v1/faculty-assignments",
        headers=a.admin,
        params={"faculty_id": a.faculty_ids["f"]},
    ).json()["items"]
    a.client.patch(
        f"/api/v1/faculty-assignments/{assignments[0]['id']}",
        headers=a.admin,
        json={"status": "inactive"},
    )
    assert a.ids("/sessions", a.f) == set()
    assert a.ids("/records", a.f) == set()
    assert a.mark(scene.math, "a1", headers=a.f).status_code == 404


# ============================================================================= parents
def test_parent_sees_only_their_children(scene: Scene) -> None:
    a, rec = scene.a, scene.rec
    parent = a.parent_of("a1")
    mine = {rec["math_a1"], rec["sci_a1"], rec["daily_a1"]}
    assert a.ids("/records", parent) == mine
    assert a.ids("/records", parent, student_id=a.s["a2"]) == set()
    assert a.ids("/records", parent, section_id=a.w.ids["section_a_b"]) == set()
    assert a.get(f"/records/{rec['math_a1']}", parent).status_code == 200
    for key in ("math_a2", "daily_a2", "math_b_ab", "school_b"):
        assert a.get(f"/records/{rec[key]}", parent).status_code == 404, key
    assert a.get(f"/students/{a.s['a1']}/summary", parent).status_code == 200
    for student in ("a2", "ab", "b"):
        assert a.get(f"/students/{a.s[student]}/summary", parent).status_code == 404
    assert a.get(f"/sessions/{scene.math}/records", parent).status_code == 404


def test_parent_of_two_children_sees_both(scene: Scene) -> None:
    a, rec = scene.a, scene.rec
    parent = a.parent_of("a1", "a2")
    assert a.ids("/records", parent) == {
        rec["math_a1"],
        rec["sci_a1"],
        rec["daily_a1"],
        rec["math_a2"],
        rec["daily_a2"],
    }
    for student in ("a1", "a2"):
        assert a.get(f"/students/{a.s[student]}/summary", parent).status_code == 200


def test_cancelled_sessions_hidden_from_parents_and_students(scene: Scene) -> None:
    a = scene.a
    parent = a.parent_of("a1")
    a.patch(f"/sessions/{scene.sci}", {"status": "inactive"})
    assert scene.rec["sci_a1"] not in a.ids("/records", parent)
    assert scene.rec["sci_a1"] not in a.ids("/records", a.w.student_headers("A1"))
    assert scene.rec["sci_a1"] in a.ids("/records")  # staff keep the history


# ============================================================================ students
def test_student_sees_only_themselves(scene: Scene) -> None:
    a, rec = scene.a, scene.rec
    me = a.w.student_headers("A1")
    assert a.ids("/records", me) == {rec["math_a1"], rec["sci_a1"], rec["daily_a1"]}
    assert a.ids("/records", me, student_id=a.s["a2"]) == set()
    assert a.get(f"/records/{rec['math_a2']}", me).status_code == 404
    assert a.get(f"/students/{a.s['a1']}/summary", me).status_code == 200
    assert a.get(f"/students/{a.s['a2']}/summary", me).status_code == 404


@pytest.mark.parametrize("who", ["parent", "student"])
def test_parents_and_students_cannot_write(scene: Scene, who: str) -> None:
    a, rec = scene.a, scene.rec
    headers = a.parent_of("a1") if who == "parent" else a.w.student_headers("A1")
    assert a.mark(scene.math, "a1", headers=headers).status_code == 403
    assert a.bulk(scene.math, {"a1": "present"}, headers).status_code == 403
    body = {"status": "present", "reason": "I was there"}
    assert a.patch(f"/records/{rec['math_a2']}", body, headers).status_code == 403
    assert a.patch(f"/records/{rec['math_a1']}", body, headers).status_code == 403


# ===================================================================== school isolation
def test_school_b_admin_cannot_reach_school_a(scene: Scene) -> None:
    a, rec = scene.a, scene.rec
    b = a.w.admin_b
    assert a.ids("/records", b) == {rec["school_b"]}
    assert a.get(f"/records/{rec['math_a1']}", b).status_code == 404
    assert a.get(f"/records/{rec['math_a1']}/history", b).status_code == 404
    assert a.patch(f"/records/{rec['math_a1']}", {"remarks": "x"}, b).status_code == 404
    assert a.mark(scene.math, a.s["b"], headers=b).status_code == 404
    assert a.get(f"/sessions/{scene.math}/records", b).status_code == 404
    assert a.get(f"/students/{a.s['a1']}/summary", b).status_code == 404
    assert a.get("/records", {**b, "X-School-ID": str(SCHOOL_A)}).status_code == 404
    # Marking a school-B student into a school-A session is not possible either.
    assert a.mark(scene.daily, a.s["b"]).status_code == 422


def test_management_reads_school_but_never_writes(
    scene: Scene, auth_headers: HeadersFactory
) -> None:
    a = scene.a
    management = auth_headers(R.MANAGEMENT)
    assert len(a.ids("/records", management, page_size=100)) == 7
    assert a.get(f"/students/{a.s['a1']}/summary", management).status_code == 200
    assert a.mark(scene.daily, "a2", headers=management).status_code == 403
    body = {"status": "absent", "reason": "x"}
    assert a.patch(f"/records/{scene.rec['math_a1']}", body, management).status_code == 403


def test_super_admin_uses_school_selection(scene: Scene, auth_headers: HeadersFactory) -> None:
    a = scene.a
    root_a = auth_headers(R.SUPER_ADMIN, SCHOOL_A)
    root_b = auth_headers(R.SUPER_ADMIN, SCHOOL_B)
    assert scene.rec["math_a1"] in a.ids("/records", root_a, page_size=100)
    assert a.ids("/records", root_b) == {scene.rec["school_b"]}
    assert a.get(f"/records/{scene.rec['math_a1']}", root_b).status_code == 404
