"""Attendance summaries: counts, the single percentage rule, filters, exclusions and
scope-limited aggregation."""

from __future__ import annotations

import pytest

from tests.integration.conftest import AttendanceWorld

pytestmark = pytest.mark.db


@pytest.fixture
def marked(aworld: AttendanceWorld) -> AttendanceWorld:
    """a1: present, present (sci), late (daily), absent, excused; a2: present."""
    a = aworld
    a.record_id(a.session_id(), "a1", "present")  # 07-10 math
    a.record_id(a.session_id(subject="sci_a", date="2026-07-11"), "a1", "present")
    daily = a.session_id(subject=None, date="2026-07-12")
    a.record_id(daily, "a1", "late")
    a.record_id(a.session_id(date="2026-07-13"), "a1", "absent")
    a.record_id(a.session_id(date="2026-07-14"), "a1", "excused")
    a.record_id(a.session_id(date="2026-07-15"), "a2", "present")
    a.session_id(date="2026-07-16")  # a1 never marked here: not counted
    return a


def _summary(
    a: AttendanceWorld, student: str = "a1", headers: object = None, **params: object
) -> dict[str, object]:
    response = a.get(f"/students/{a.s[student]}/summary", headers, **params)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_counts_and_percentage(marked: AttendanceWorld) -> None:
    body = _summary(marked)
    assert {k: body[k] for k in ("total_sessions", "present", "absent", "late", "excused")} == {
        "total_sessions": 5,
        "present": 2,
        "absent": 1,
        "late": 1,
        "excused": 1,
    }
    # (present + late) / (total - excused) = 3 / 4
    assert (body["attended"], body["counted_sessions"], body["attendance_percentage"]) == (
        3,
        4,
        75.0,
    )


def test_filters(marked: AttendanceWorld) -> None:
    ids = marked.w.ids
    sci = _summary(marked, subject_id=ids["sci_a"])
    assert (sci["total_sessions"], sci["attendance_percentage"], sci["subject_id"]) == (
        1,
        100.0,
        ids["sci_a"],
    )
    daily = _summary(marked, session_type="daily")
    assert (daily["total_sessions"], daily["late"], daily["session_type"]) == (1, 1, "daily")
    window = _summary(marked, from_date="2026-07-12", to_date="2026-07-13")
    assert (window["total_sessions"], window["attended"], window["attendance_percentage"]) == (
        2,
        1,
        50.0,
    )
    year = _summary(marked, academic_year_id=ids["year_a"])
    assert year["total_sessions"] == 5
    previous = _summary(marked, academic_year_id=ids["year_a_prev"])
    assert (previous["total_sessions"], previous["attendance_percentage"]) == (0, None)


def test_inactive_sessions_are_excluded(marked: AttendanceWorld) -> None:
    absent_session = marked.ids("/sessions", session_date="2026-07-13")
    marked.patch(f"/sessions/{absent_session.pop()}", {"status": "inactive"})
    body = _summary(marked)
    assert (body["total_sessions"], body["absent"], body["attendance_percentage"]) == (
        4,
        0,
        100.0,
    )


def test_only_excused_gives_no_percentage(aworld: AttendanceWorld) -> None:
    aworld.record_id(aworld.session_id(), "a1", "excused")
    body = _summary(aworld)
    assert (body["total_sessions"], body["counted_sessions"], body["attendance_percentage"]) == (
        1,
        0,
        None,
    )


def test_faculty_summary_counts_only_their_sessions(marked: AttendanceWorld) -> None:
    """F (section A maths) sees the three maths sessions and the daily roll call only."""
    body = _summary(marked, headers=marked.f)
    assert {k: body[k] for k in ("total_sessions", "present", "absent", "late", "excused")} == {
        "total_sessions": 4,
        "present": 1,
        "absent": 1,
        "late": 1,
        "excused": 1,
    }
    assert body["attendance_percentage"] == pytest.approx(66.67)
    assert marked.get(f"/students/{marked.s['ab']}/summary", marked.f).status_code == 404


def test_summary_rejects_unknown_or_foreign_students(marked: AttendanceWorld) -> None:
    import uuid

    assert marked.get(f"/students/{uuid.uuid4()}/summary").status_code == 404
    assert marked.get(f"/students/{marked.s['b']}/summary").status_code == 404
