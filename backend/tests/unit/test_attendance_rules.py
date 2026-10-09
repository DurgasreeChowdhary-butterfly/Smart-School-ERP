"""Attendance rules that need no database: percentage formula and input normalisation."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.attendance import SessionType
from app.schemas.attendance import (
    AttendanceRecordCreate,
    AttendanceRecordUpdate,
    AttendanceSessionCreate,
)
from app.services.attendance import build_summary

STUDENT = uuid.uuid4()


def _counts(present: int = 0, absent: int = 0, late: int = 0, excused: int = 0) -> dict[str, int]:
    return {"present": present, "absent": absent, "late": late, "excused": excused}


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (_counts(present=1, absent=2), 33.33),
        (_counts(present=2, late=1, absent=1, excused=1), 75.0),
        (_counts(late=3), 100.0),
        (_counts(absent=3, excused=5), 0.0),
        (_counts(excused=2), None),
        (_counts(), None),
    ],
)
def test_percentage_rule(counts: dict[str, int], expected: float | None) -> None:
    summary = build_summary(STUDENT, counts, session_type=SessionType.DAILY)
    assert summary.attendance_percentage == expected
    assert summary.total_sessions == sum(counts.values())
    assert summary.attended == counts["present"] + counts["late"]
    assert summary.counted_sessions == summary.total_sessions - counts["excused"]
    assert summary.session_type == "daily"


@pytest.mark.parametrize(("raw", "stored"), [("PRESENT", "present"), (" late ", "late")])
def test_status_is_normalised(raw: str, stored: str) -> None:
    assert AttendanceRecordCreate(student_id=STUDENT, status=raw).status == stored  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", ["sick", "", 1, None])
def test_unknown_status_rejected(raw: object) -> None:
    with pytest.raises(ValidationError):
        AttendanceRecordCreate(student_id=STUDENT, status=raw)  # type: ignore[arg-type]


def test_record_update_needs_something_to_change() -> None:
    with pytest.raises(ValidationError):
        AttendanceRecordUpdate(reason="why")
    with pytest.raises(ValidationError):
        AttendanceRecordUpdate(status=None)
    assert AttendanceRecordUpdate(remarks=None).changes() == {"remarks": None}


def test_session_times_must_be_ordered() -> None:
    base = {
        "academic_year_id": uuid.uuid4(),
        "class_id": uuid.uuid4(),
        "section_id": uuid.uuid4(),
        "session_date": "2026-07-10",
    }
    with pytest.raises(ValidationError):
        AttendanceSessionCreate(**base, start_time="10:00", end_time="10:00")  # type: ignore[arg-type]
    assert AttendanceSessionCreate(**base).period == 1  # type: ignore[arg-type]
