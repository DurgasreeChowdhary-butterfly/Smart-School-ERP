"""Attendance records: marking, statuses, eligibility on the session date, atomic bulk
marking, unmarked students, corrections with history, and historical correctness."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceRecord, AttendanceRecordChange
from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import HeadersFactory
from tests.integration.conftest import SESSION_DATE, AttendanceWorld

pytestmark = pytest.mark.db
R = RoleName


def _record_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(AttendanceRecord)) or 0)


def _details(response: Any) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = response.json()["error"]["details"]
    return details


# ========================================================================== marking
def test_mark_one_student(aworld: AttendanceWorld, db_session: Session) -> None:
    sid = aworld.session_id()
    response = aworld.mark(sid, "a1", "late", remarks="Bus delayed")
    assert response.status_code == 201, response.text
    body = response.json()
    ids = aworld.w.ids
    assert (body["student_id"], body["enrollment_id"]) == (aworld.s["a1"], aworld.e["a1"])
    assert (body["section_id"], body["class_id"], body["academic_year_id"]) == (
        ids["section_a"],
        ids["class_a"],
        ids["year_a"],
    )
    assert (body["subject_id"], body["session_date"], body["session_type"]) == (
        ids["math_a"],
        SESSION_DATE,
        "subject",
    )
    assert (body["status"], body["original_status"], body["remarks"]) == (
        "late",
        "late",
        "Bus delayed",
    )
    assert body["marked_at"] is not None
    assert body["corrected_at"] is None
    assert aworld.get(f"/records/{body['id']}").json() == body
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "attendance_record.created")
    ).one()
    assert event.details["student_id"] == aworld.s["a1"]
    assert event.details["status"] == "late"
    assert "Bus delayed" not in str(event.details)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("present", "present"),
        ("absent", "absent"),
        ("late", "late"),
        ("excused", "excused"),
        ("PRESENT", "present"),
        (" Excused ", "excused"),
    ],
)
def test_statuses(aworld: AttendanceWorld, given: str, stored: str) -> None:
    response = aworld.mark(aworld.session_id(), "a1", given)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == stored


@pytest.mark.parametrize("status", ["sick", "", "P", "presentt", None])
def test_invalid_status_rejected(aworld: AttendanceWorld, status: object) -> None:
    assert aworld.mark(aworld.session_id(), "a1", status).status_code == 422  # type: ignore[arg-type]


def test_duplicate_student_in_session_rejected(aworld: AttendanceWorld) -> None:
    sid = aworld.session_id()
    assert aworld.mark(sid, "a1").status_code == 201
    duplicate = aworld.mark(sid, "a1", "absent")
    assert duplicate.status_code == 409
    assert _details(duplicate)[0]["loc"] == ["body", "student_id"]


@pytest.mark.parametrize("student", ["ab", "c2", "prev", "b", str(uuid.uuid4())])
def test_ineligible_students_rejected(aworld: AttendanceWorld, student: str) -> None:
    """Other section, other class, previous year only, other school, unknown: one answer."""
    response = aworld.mark(aworld.session_id(), student)
    assert response.status_code == 422
    assert _details(response)[0] == {
        "loc": ["body", "student_id"],
        "message": "Student is not enrolled in this section on the session date.",
        "type": None,
    }


# ===================================================== eligibility on the session date
def test_late_joiner_only_from_enrollment_date(aworld: AttendanceWorld) -> None:
    w = aworld.w
    late = w.student("LATE").json()["id"]
    assert w.enroll(late, date="2026-08-01").status_code == 201
    assert aworld.mark(aworld.session_id(), late).status_code == 422  # 2026-07-10
    assert aworld.mark(aworld.session_id(date="2026-08-01"), late).status_code == 201


def test_moved_student_follows_enrollment_history(aworld: AttendanceWorld) -> None:
    """a1 moves from section A to section B on 2026-07-15 (the move date is in B)."""
    moved = aworld.w.enroll(
        aworld.s["a1"],
        sec="section_a_b",
        date="2026-07-15",
        supersedes_enrollment_id=aworld.e["a1"],
    )
    assert moved.status_code == 201, moved.text
    before = aworld.mark(aworld.session_id(), "a1")  # 07-10, section A
    assert before.status_code == 201
    assert before.json()["enrollment_id"] == aworld.e["a1"]
    assert aworld.mark(aworld.session_id(date="2026-07-15"), "a1").status_code == 422
    in_b = aworld.mark(aworld.session_id(sec="section_a_b", date="2026-07-15"), "a1")
    assert in_b.status_code == 201
    assert in_b.json()["enrollment_id"] == moved.json()["id"]
    assert aworld.mark(aworld.session_id(sec="section_a_b"), "a1").status_code == 422  # 07-10


def test_withdrawn_student_eligible_through_last_day(aworld: AttendanceWorld) -> None:
    withdrawn = aworld.w.client.patch(
        f"/api/v1/enrollments/{aworld.e['a2']}",
        headers=aworld.admin,
        json={"status": "withdrawn", "end_date": SESSION_DATE},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert aworld.mark(aworld.session_id(), "a2").status_code == 201
    assert aworld.mark(aworld.session_id(date="2026-07-11"), "a2").status_code == 422


# ============================================================================= bulk
def test_bulk_marks_whole_section(aworld: AttendanceWorld, db_session: Session) -> None:
    sid = aworld.session_id()
    response = aworld.bulk(sid, {"a1": "present", "a2": "ABSENT"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["session_id"], body["created"]) == (sid, 2)
    assert {(i["student_id"], i["status"]) for i in body["items"]} == {
        (aworld.s["a1"], "present"),
        (aworld.s["a2"], "absent"),
    }
    listed = aworld.get(f"/sessions/{sid}/records").json()
    assert listed["total"] == 2
    assert aworld.ids(f"/sessions/{sid}/records", status="absent") == {
        i["id"] for i in body["items"] if i["status"] == "absent"
    }
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "attendance_record.bulk_created")
    ).one()
    assert event.resource_id == sid
    assert event.details["records"] == 2
    assert event.details["status_counts"] == {"present": 1, "absent": 1, "late": 0, "excused": 0}


@pytest.mark.parametrize("intruder", ["ab", "c2", "prev", "b"])
def test_bulk_is_all_or_nothing(
    aworld: AttendanceWorld, db_session: Session, intruder: str
) -> None:
    sid = aworld.session_id()
    response = aworld.bulk(sid, {"a1": "present", intruder: "present", "a2": "late"})
    assert response.status_code == 422
    assert [d["loc"] for d in _details(response)] == [["body", "records", 1, "student_id"]]
    assert _record_count(db_session) == 0


def test_bulk_duplicate_in_request_rejected(aworld: AttendanceWorld, db_session: Session) -> None:
    sid = aworld.session_id()
    records = [{"student_id": aworld.s["a1"], "status": s} for s in ("present", "absent")]
    response = aworld.client.post(
        f"/api/v1/attendance/sessions/{sid}/records/bulk",
        headers=aworld.admin,
        json={"records": records},
    )
    assert response.status_code == 422
    assert [d["loc"] for d in _details(response)] == [["body", "records", 1, "student_id"]]
    assert _record_count(db_session) == 0


def test_bulk_with_already_marked_student_is_conflict(
    aworld: AttendanceWorld, db_session: Session
) -> None:
    sid = aworld.session_id()
    assert aworld.mark(sid, "a2").status_code == 201
    response = aworld.bulk(sid, {"a1": "present", "a2": "absent"})
    assert response.status_code == 409
    assert [d["loc"] for d in _details(response)] == [["body", "records", 1, "student_id"]]
    assert _record_count(db_session) == 1


@pytest.mark.parametrize("size", [0, 201])
def test_bulk_size_limits(aworld: AttendanceWorld, size: int) -> None:
    records = [{"student_id": str(uuid.uuid4()), "status": "present"} for _ in range(size)]
    response = aworld.client.post(
        f"/api/v1/attendance/sessions/{aworld.session_id()}/records/bulk",
        headers=aworld.admin,
        json={"records": records},
    )
    assert response.status_code == 422


def test_unmarked_students(aworld: AttendanceWorld) -> None:
    """Missing records are never presumed present or absent: they are listed instead."""
    sid = aworld.session_id()
    unmarked = aworld.get(f"/sessions/{sid}/unmarked-students").json()
    assert {i["student_id"] for i in unmarked["items"]} == {aworld.s["a1"], aworld.s["a2"]}
    assert unmarked["items"][0]["enrollment_id"] in {aworld.e["a1"], aworld.e["a2"]}
    aworld.mark(sid, "a1")
    after = aworld.get(f"/sessions/{sid}/unmarked-students").json()
    assert (after["total"], after["items"][0]["admission_number"]) == (1, "A2")
    assert aworld.get(f"/sessions/{uuid.uuid4()}/unmarked-students").status_code == 404


def test_inactive_session_accepts_no_changes(aworld: AttendanceWorld) -> None:
    sid = aworld.session_id()
    record = aworld.record_id(sid, "a1")
    aworld.patch(f"/sessions/{sid}", {"status": "inactive"})
    assert aworld.mark(sid, "a2").status_code == 422
    assert aworld.bulk(sid, {"a2": "present"}).status_code == 422
    assert aworld.patch(f"/records/{record}", {"remarks": "x"}).status_code == 422
    # The records stay readable (history) by staff.
    assert aworld.get(f"/records/{record}").json()["session_status"] == "inactive"


# ====================================================================== corrections
def test_status_change_requires_reason(aworld: AttendanceWorld, db_session: Session) -> None:
    record = aworld.record_id(aworld.session_id(), "a1")
    response = aworld.patch(f"/records/{record}", {"status": "absent"})
    assert response.status_code == 422
    assert _details(response)[0]["loc"] == ["body", "reason"]
    assert (
        aworld.patch(f"/records/{record}", {"status": "absent", "reason": "  "}).status_code == 422
    )
    assert aworld.patch(f"/records/{record}", {"reason": "nothing else"}).status_code == 422
    assert db_session.scalar(select(func.count()).select_from(AttendanceRecordChange)) == 0


def test_correction_preserves_original_and_history(
    aworld: AttendanceWorld, db_session: Session, auth_headers: HeadersFactory
) -> None:
    record = aworld.record_id(aworld.session_id(), "a1", "absent")
    first = aworld.patch(
        f"/records/{record}", {"status": "late", "reason": "Arrived at 9:20, register error"}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["status"], body["original_status"]) == ("late", "absent")
    assert body["correction_reason"] == "Arrived at 9:20, register error"
    assert body["corrected_by_user_id"] is not None
    assert body["corrected_at"] is not None
    second = aworld.patch(f"/records/{record}", {"status": "excused", "reason": "Medical note"})
    assert (second.json()["status"], second.json()["original_status"]) == ("excused", "absent")
    assert second.json()["correction_reason"] == "Medical note"

    history = aworld.get(f"/records/{record}/history").json()
    assert [(h["previous_status"], h["new_status"], h["reason"]) for h in history] == [
        ("absent", "late", "Arrived at 9:20, register error"),
        ("late", "excused", "Medical note"),
    ]
    assert history[0]["changed_by_user_id"] == body["corrected_by_user_id"]
    assert history[0]["changed_at"] <= history[1]["changed_at"]

    events = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "attendance_record.corrected")
        .order_by(AuditLog.created_at)
    ).all()
    assert [(e.details["previous_status"], e.details["new_status"]) for e in events] == [
        ("absent", "late"),
        ("late", "excused"),
    ]
    assert events[0].details["student_id"] == aworld.s["a1"]
    assert events[0].details["reason"] == "Arrived at 9:20, register error"
    assert events[0].details["change_id"] == history[0]["id"]
    # Management (school-wide reader) may audit corrections too.
    assert aworld.get(f"/records/{record}/history", auth_headers(R.MANAGEMENT)).status_code == 200


def test_remarks_change_is_an_update_not_a_correction(
    aworld: AttendanceWorld, db_session: Session
) -> None:
    record = aworld.record_id(aworld.session_id(), "a1")
    response = aworld.patch(f"/records/{record}", {"remarks": "Left at noon (fever)"})
    assert response.status_code == 200
    assert (response.json()["remarks"], response.json()["corrected_at"]) == (
        "Left at noon (fever)",
        None,
    )
    history = aworld.get(f"/records/{record}/history").json()
    assert [(h["previous_remarks"], h["new_remarks"], h["reason"]) for h in history] == [
        (None, "Left at noon (fever)", None)
    ]
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "attendance_record.updated")
    ).one()
    assert event.details["remarks_changed"] is True
    assert "fever" not in str(event.details)  # remarks are recorded by name only
    cleared = aworld.patch(f"/records/{record}", {"remarks": None})
    assert cleared.json()["remarks"] is None


def test_noop_change_is_not_recorded(aworld: AttendanceWorld, db_session: Session) -> None:
    record = aworld.record_id(aworld.session_id(), "a1")
    response = aworld.patch(f"/records/{record}", {"status": "PRESENT", "reason": "same"})
    assert response.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(AttendanceRecordChange)) == 0
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action.like("attendance_record.%"), AuditLog.resource_id == record)
        )
        == 1  # only "created"
    )


def test_history_cannot_be_rewritten(aworld: AttendanceWorld, db_session: Session) -> None:
    record = aworld.record_id(aworld.session_id(), "a1")
    aworld.patch(f"/records/{record}", {"status": "absent", "reason": "Register error"})
    for sql in (
        "UPDATE attendance_record_changes SET new_status = 'present'",
        "DELETE FROM attendance_record_changes",
    ):
        with pytest.raises(IntegrityError, match="append-only"), db_session.begin_nested():
            db_session.execute(text(sql))
    assert db_session.scalar(select(func.count()).select_from(AttendanceRecordChange)) == 1


def test_records_cannot_be_moved_to_another_student(
    aworld: AttendanceWorld,
) -> None:
    record = aworld.record_id(aworld.session_id(), "a1")
    for body in ({"student_id": aworld.s["a2"]}, {"session_id": str(uuid.uuid4())}):
        assert aworld.patch(f"/records/{record}", body).status_code == 422


def test_parents_and_students_cannot_read_history(aworld: AttendanceWorld) -> None:
    record = aworld.record_id(aworld.session_id(), "a1")
    for headers in (aworld.w.student_headers("A1"), aworld.parent_of("a1")):
        assert aworld.get(f"/records/{record}", headers).status_code == 200
        assert aworld.get(f"/records/{record}/history", headers).status_code == 403


# ============================================================== historical correctness
def test_past_attendance_stays_with_the_original_placement(aworld: AttendanceWorld) -> None:
    """Moving a student later never moves their past attendance."""
    sid = aworld.session_id()
    record = aworld.record_id(sid, "a1")
    moved = aworld.w.enroll(
        aworld.s["a1"],
        sec="section_a_b",
        date="2026-09-01",
        supersedes_enrollment_id=aworld.e["a1"],
    )
    assert moved.status_code == 201
    after = aworld.get(f"/records/{record}").json()
    ids = aworld.w.ids
    assert (after["section_id"], after["enrollment_id"]) == (ids["section_a"], aworld.e["a1"])
    assert aworld.ids("/records", section_id=ids["section_a"]) == {record}
    assert aworld.ids("/records", section_id=ids["section_a_b"]) == set()
    # Faculty F (section A maths) still sees the history of their own session ...
    assert aworld.ids("/records", aworld.f) == {record}
    # ... and H (section B maths) does not inherit it.
    assert aworld.ids("/records", aworld.h) == set()
    # Corrections still work on the historical record.
    corrected = aworld.patch(f"/records/{record}", {"status": "absent", "reason": "Late entry"})
    assert corrected.status_code == 200
    assert corrected.json()["section_id"] == ids["section_a"]


def test_record_history_queries(aworld: AttendanceWorld) -> None:
    math = aworld.session_id()
    sci = aworld.session_id(subject="sci_a", date="2026-07-13")
    daily = aworld.session_id(subject=None, date="2026-07-14")
    r1 = aworld.record_id(math, "a1")
    r2 = aworld.record_id(sci, "a1", "absent")
    r3 = aworld.record_id(daily, "a1", "late")
    r4 = aworld.record_id(math, "a2")
    ids = aworld.w.ids
    assert aworld.ids("/records", student_id=aworld.s["a1"]) == {r1, r2, r3}
    assert aworld.ids("/records", subject_id=ids["sci_a"]) == {r2}
    assert aworld.ids("/records", session_type="daily") == {r3}
    assert aworld.ids("/records", session_date=SESSION_DATE) == {r1, r4}
    assert aworld.ids("/records", from_date="2026-07-13", to_date="2026-07-13") == {r2}
    assert aworld.ids("/records", status="late") == {r3}
    assert aworld.ids("/records", academic_year_id=ids["year_a"], class_id=ids["class_a"]) == {
        r1,
        r2,
        r3,
        r4,
    }
    page = aworld.get("/records", page_size=3).json()
    assert (page["total"], len(page["items"]), page["items"][0]["session_date"]) == (
        4,
        3,
        "2026-07-14",
    )
    assert aworld.get(f"/records/{uuid.uuid4()}").status_code == 404
