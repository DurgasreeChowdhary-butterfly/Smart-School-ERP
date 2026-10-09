"""Attendance: sessions (one roll call), records (one student's status in it) and the
append-only history of changes to records.

Session model
-------------
A session is one roll call for one **section** on one school-local **date**:

* ``subject_id IS NULL``  -> a *daily* (general/class) roll call;
* ``subject_id`` set      -> a *subject* (period) roll call.

``session_type`` is derived from ``subject_id`` by PostgreSQL (generated column), so
the two can never disagree. ``period`` (1-20, default 1) numbers the roll calls of the
same kind on the same day, so double periods or a morning/afternoon roll call are
legitimate rather than duplicates.

What is a duplicate (at most one *active* session each; inactive history may repeat):

* daily:   (academic year, section, date, period)            where subject_id IS NULL
* subject: (academic year, section, subject, date, period)   where subject_id IS NOT NULL

Integrity rules enforced by PostgreSQL (not only by services)
--------------------------------------------------------------
* every structural reference is a composite foreign key carrying ``school_id``, and
  ``(section_id, class_id)`` pins the section to the session's class;
* a record's ``(session_id, school_id, academic_year_id, class_id, section_id)`` must
  equal its session's, and its ``(enrollment_id, school_id, student_id,
  academic_year_id, class_id, section_id)`` must equal an enrollment's. So a record can
  only exist for a student who holds an enrollment in exactly the session's school,
  year, class and section, and it is pinned to *that* enrollment: moving the student
  later never moves their attendance;
* one record per (session, student);
* ``attendance_record_changes`` rejects UPDATE (and DELETE, unless a deliberate purge
  is requested; see migration 0006), and a status change must carry a reason.

Nothing here is hard-deleted by the application.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AttendanceStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    LATE = "late"
    EXCUSED = "excused"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class SessionType(StrEnum):
    DAILY = "daily"
    SUBJECT = "subject"


def _school_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"), index=True)


def _user_fk(*, nullable: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


SESSION_TYPE_SQL = "CASE WHEN subject_id IS NULL THEN 'daily' ELSE 'subject' END"


class AttendanceSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attendance_sessions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(SessionStatus)})", name="status_valid"),
        CheckConstraint("period BETWEEN 1 AND 20", name="period_range"),
        CheckConstraint(
            "start_time IS NULL OR end_time IS NULL OR start_time < end_time",
            name="times_ordered",
        ),
        # Composite FK target for records: a record repeats its session's placement.
        UniqueConstraint(
            "id",
            "school_id",
            "academic_year_id",
            "class_id",
            "section_id",
            name="uq_attendance_sessions_placement_key",
        ),
        ForeignKeyConstraint(
            ["academic_year_id", "school_id"],
            ["academic_years.id", "academic_years.school_id"],
            name="fk_attendance_sessions_year_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_attendance_sessions_class_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "school_id"],
            ["sections.id", "sections.school_id"],
            name="fk_attendance_sessions_section_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "class_id"],
            ["sections.id", "sections.class_id"],
            name="fk_attendance_sessions_section_in_class",
            ondelete="RESTRICT",
        ),
        # MATCH SIMPLE: a daily session (NULL subject) skips this check by design.
        ForeignKeyConstraint(
            ["subject_id", "school_id"],
            ["subjects.id", "subjects.school_id"],
            name="fk_attendance_sessions_subject_same_school",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_attendance_sessions_daily_active",
            "academic_year_id",
            "section_id",
            "session_date",
            "period",
            unique=True,
            postgresql_where=text("subject_id IS NULL AND status = 'active'"),
        ),
        Index(
            "uq_attendance_sessions_subject_active",
            "academic_year_id",
            "section_id",
            "subject_id",
            "session_date",
            "period",
            unique=True,
            postgresql_where=text("subject_id IS NOT NULL AND status = 'active'"),
        ),
        Index("ix_attendance_sessions_section_date", "school_id", "section_id", "session_date"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    subject_id: Mapped[uuid.UUID | None] = mapped_column()
    session_date: Mapped[date] = mapped_column(Date)
    period: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    session_type: Mapped[str] = mapped_column(
        String(10), Computed(SESSION_TYPE_SQL, persisted=True)
    )
    status: Mapped[str] = mapped_column(
        String(20), default=SessionStatus.ACTIVE.value, server_default=SessionStatus.ACTIVE.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, Computed("status = 'active'", persisted=True))
    created_by_user_id: Mapped[uuid.UUID] = _user_fk()


class AttendanceRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One student's status in one session.

    ``original_status`` is the status first recorded and never changes; every later
    change is appended to :class:`AttendanceRecordChange`. ``corrected_*`` describe the
    latest status correction.
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(AttendanceStatus)})", name="status_valid"),
        CheckConstraint(
            f"original_status IN ({_values(AttendanceStatus)})", name="original_status_valid"
        ),
        CheckConstraint(
            "(corrected_at IS NULL) = (corrected_by_user_id IS NULL) "
            "AND (corrected_at IS NULL) = (correction_reason IS NULL)",
            name="correction_complete",
        ),
        CheckConstraint(
            "correction_reason IS NULL OR length(trim(correction_reason)) > 0",
            name="correction_reason_not_blank",
        ),
        UniqueConstraint("session_id", "student_id"),
        # Composite FK target for the change history.
        UniqueConstraint("id", "school_id", name="uq_attendance_records_id_school_id"),
        ForeignKeyConstraint(
            ["session_id", "school_id", "academic_year_id", "class_id", "section_id"],
            [
                "attendance_sessions.id",
                "attendance_sessions.school_id",
                "attendance_sessions.academic_year_id",
                "attendance_sessions.class_id",
                "attendance_sessions.section_id",
            ],
            name="fk_attendance_records_session_placement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "enrollment_id",
                "school_id",
                "student_id",
                "academic_year_id",
                "class_id",
                "section_id",
            ],
            [
                "student_enrollments.id",
                "student_enrollments.school_id",
                "student_enrollments.student_id",
                "student_enrollments.academic_year_id",
                "student_enrollments.class_id",
                "student_enrollments.section_id",
            ],
            name="fk_attendance_records_enrollment_placement",
            ondelete="RESTRICT",
        ),
        Index("ix_attendance_records_student_id", "student_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    session_id: Mapped[uuid.UUID] = mapped_column()
    enrollment_id: Mapped[uuid.UUID] = mapped_column()
    student_id: Mapped[uuid.UUID] = mapped_column()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(10))
    original_status: Mapped[str] = mapped_column(String(10))
    remarks: Mapped[str | None] = mapped_column(String(500))
    marked_by_user_id: Mapped[uuid.UUID] = _user_fk()
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    corrected_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_reason: Mapped[str | None] = mapped_column(String(500))


class AttendanceRecordChange(UUIDPrimaryKeyMixin, Base):
    """Append-only: one row per change to a record (status and/or remarks), numbered
    1, 2, 3... per record by ``sequence``."""

    __tablename__ = "attendance_record_changes"
    __table_args__ = (
        CheckConstraint(
            f"previous_status IN ({_values(AttendanceStatus)})", name="previous_status_valid"
        ),
        CheckConstraint(f"new_status IN ({_values(AttendanceStatus)})", name="new_status_valid"),
        CheckConstraint(
            "previous_status = new_status OR reason IS NOT NULL",
            name="reason_required_for_status_change",
        ),
        CheckConstraint("reason IS NULL OR length(trim(reason)) > 0", name="reason_not_blank"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        # One change per position: concurrent writers cannot both append change N.
        UniqueConstraint("record_id", "sequence"),
        ForeignKeyConstraint(
            ["record_id", "school_id"],
            ["attendance_records.id", "attendance_records.school_id"],
            name="fk_attendance_record_changes_record_same_school",
            ondelete="RESTRICT",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    record_id: Mapped[uuid.UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer)
    previous_status: Mapped[str] = mapped_column(String(10))
    new_status: Mapped[str] = mapped_column(String(10))
    previous_remarks: Mapped[str | None] = mapped_column(String(500))
    new_remarks: Mapped[str | None] = mapped_column(String(500))
    reason: Mapped[str | None] = mapped_column(String(500))
    changed_by_user_id: Mapped[uuid.UUID] = _user_fk()
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
