"""Request/response models for attendance."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.models.attendance import AttendanceStatus, SessionStatus
from app.schemas.fields import PatchInput, StrictInput, Text500

MAX_BULK_RECORDS = 200


def _normalise_status(value: Any) -> Any:
    # "PRESENT", " Present " and "present" are the same status; anything else is rejected.
    return value.strip().lower() if isinstance(value, str) else value


Status = Annotated[AttendanceStatus, BeforeValidator(_normalise_status)]


def _ordered(start: time | None, end: time | None) -> None:
    if start is not None and end is not None and start >= end:
        raise ValueError("start_time must be before end_time.")


# ========================================================================= sessions
class AttendanceSessionCreate(StrictInput):
    """Omit ``subject_id`` for a daily (general) roll call; set it for a subject roll call."""

    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID | None = None
    session_date: date = Field(
        description="School-local calendar date; within the academic year and not in the "
        "future (in the school's time zone)."
    )
    period: int = Field(
        default=1, ge=1, le=20, description="Roll-call number of this kind on this day."
    )
    start_time: time | None = None
    end_time: time | None = None

    @model_validator(mode="after")
    def _times(self) -> AttendanceSessionCreate:
        _ordered(self.start_time, self.end_time)
        return self


class AttendanceSessionUpdate(PatchInput):
    """Placement, date, subject and period are immutable. ``status`` (de)activates."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"start_time", "end_time"})

    start_time: time | None = None
    end_time: time | None = None
    status: SessionStatus | None = None


class AttendanceSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID | None
    session_type: str
    session_date: date
    period: int
    start_time: time | None
    end_time: time | None
    status: str
    is_active: bool
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ========================================================================== records
class AttendanceRecordCreate(StrictInput):
    student_id: uuid.UUID
    status: Status = Field(description="present, absent, late or excused (case-insensitive).")
    remarks: Text500 | None = None


class AttendanceBulkCreate(StrictInput):
    """All-or-nothing: if any entry is invalid, nothing is written."""

    records: list[AttendanceRecordCreate] = Field(min_length=1, max_length=MAX_BULK_RECORDS)


class AttendanceRecordUpdate(PatchInput):
    """A status change is a *correction* and requires ``reason``. Every change is kept
    in the record's history."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"remarks"})

    status: Status | None = None
    remarks: Text500 | None = None
    reason: Text500 | None = Field(
        default=None, description="Why the record is changed. Required to change status."
    )

    @model_validator(mode="after")
    def _something_to_change(self) -> AttendanceRecordUpdate:
        if not self.model_fields_set - {"reason"}:
            raise ValueError("Provide status and/or remarks to change.")
        return self


class AttendanceRecordRead(BaseModel):
    id: uuid.UUID
    school_id: uuid.UUID
    session_id: uuid.UUID
    student_id: uuid.UUID
    enrollment_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID | None
    session_type: str
    session_date: date
    period: int
    session_status: str
    status: str
    original_status: str
    remarks: str | None
    marked_by_user_id: uuid.UUID
    marked_at: datetime
    corrected_by_user_id: uuid.UUID | None
    corrected_at: datetime | None
    correction_reason: str | None
    created_at: datetime
    updated_at: datetime


class AttendanceBulkResult(BaseModel):
    session_id: uuid.UUID
    created: int
    items: list[AttendanceRecordRead]


class AttendanceRecordChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    record_id: uuid.UUID
    sequence: int
    previous_status: str
    new_status: str
    previous_remarks: str | None
    new_remarks: str | None
    reason: str | None
    changed_by_user_id: uuid.UUID
    changed_at: datetime


class UnmarkedStudent(BaseModel):
    student_id: uuid.UUID
    enrollment_id: uuid.UUID
    admission_number: str
    first_name: str
    last_name: str


# ========================================================================== summary
class AttendanceSummary(BaseModel):
    """``attendance_percentage = (present + late) / (total_sessions - excused) * 100``,
    rounded to 2 decimals; ``null`` when no session counts. Only *marked* records in
    *active* sessions are counted: an unmarked session is neither present nor absent."""

    student_id: uuid.UUID
    academic_year_id: uuid.UUID | None
    from_date: date | None
    to_date: date | None
    subject_id: uuid.UUID | None
    session_type: str | None
    total_sessions: int
    present: int
    absent: int
    late: int
    excused: int
    attended: int = Field(description="present + late")
    counted_sessions: int = Field(description="total_sessions - excused")
    attendance_percentage: float | None
