"""Examinations: exams, their subject components, marks, correction history,
result publications and the report cards frozen at publication.

Exam model
----------
An exam belongs to one school, academic year and **class**. ``section_id`` set ->
the exam is for that section only; ``NULL`` -> every section of the class. Each
subject in the exam is one component (:class:`ExamSubject`, unique per exam) with its
own maximum and optional passing marks.

Lifecycle (``status``)::

    draft -> scheduled -> marks_entry -> published
      ^          |             |  ^          |
      +----------+             |  +----------+  (reopen: withdraws the publication)
    draft / scheduled / marks_entry -> cancelled (terminal)

Results are published as **snapshots**: publishing freezes every student's result into
``exam_report_cards`` under a numbered ``exam_publications`` row. Reopening withdraws
that publication (with a reason); republishing creates the next version. Later
corrections can therefore never silently change a published report card.

Integrity rules enforced by PostgreSQL (not only by services)
--------------------------------------------------------------
* every reference is a composite foreign key carrying ``school_id``;
* a mark repeats its component's ``max_marks`` through a composite foreign key, so
  ``marks_obtained <= max_marks`` is a CHECK and a component's maximum cannot change
  once marks reference it;
* a mark's ``(enrollment_id, school_id, student_id, year, class, section)`` must equal an
  enrollment's, and ``(exam_id, school_id, year, class)`` the exam's;
* triggers (migration 0007) enforce the lifecycle transitions, freeze components outside
  ``draft``/``scheduled``, accept marks only in ``marks_entry`` and only for the exam's
  section, and keep history, publications and report cards append-only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExamStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    MARKS_ENTRY = "marks_entry"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class ExamType(StrEnum):
    UNIT_TEST = "unit_test"
    PERIODIC = "periodic"
    MIDTERM = "midterm"
    TERM = "term"
    FINAL = "final"
    PRACTICAL = "practical"
    OTHER = "other"


class MarkStatus(StrEnum):
    PRESENT = "present"  # marks_obtained is required
    ABSENT = "absent"  # no marks; counts as 0 out of the maximum
    EXEMPT = "exempt"  # no marks; left out of the totals entirely


class PublicationStatus(StrEnum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class ExamResult(StrEnum):
    PASS = "pass"  # noqa: S105 - result label, not a secret
    FAIL = "fail"
    INCOMPLETE = "incomplete"
    NOT_APPLICABLE = "not_applicable"


# Marks: up to 9999.99 with two decimals (stored exactly; never floating point).
MARKS = Numeric(6, 2)
TOTAL = Numeric(8, 2)
PERCENT = Numeric(5, 2)


def _school_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"), index=True)


def _user_fk(*, nullable: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=nullable)


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


def _fk(columns: list[str], table: str, targets: list[str], name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        columns, [f"{table}.{c}" for c in targets], name=name, ondelete="RESTRICT"
    )


ENROLLMENT_PLACEMENT = [
    "enrollment_id",
    "school_id",
    "student_id",
    "academic_year_id",
    "class_id",
    "section_id",
]
ENROLLMENT_TARGET = ["id", "school_id", "student_id", "academic_year_id", "class_id", "section_id"]
EXAM_PLACEMENT = ["exam_id", "school_id", "academic_year_id", "class_id"]
EXAM_TARGET = ["id", "school_id", "academic_year_id", "class_id"]


class Exam(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "exams"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(ExamStatus)})", name="status_valid"),
        CheckConstraint(f"exam_type IN ({_values(ExamType)})", name="exam_type_valid"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("start_date <= end_date", name="dates_ordered"),
        CheckConstraint(
            "pass_percentage IS NULL OR pass_percentage BETWEEN 0 AND 100",
            name="pass_percentage_range",
        ),
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL) "
            "AND (cancelled_at IS NULL) = (cancelled_by_user_id IS NULL) "
            "AND (cancelled_at IS NULL) = (cancellation_reason IS NULL)",
            name="cancellation_complete",
        ),
        CheckConstraint(
            "cancellation_reason IS NULL OR length(trim(cancellation_reason)) > 0",
            name="cancellation_reason_not_blank",
        ),
        # Composite FK targets for components, marks, publications and report cards.
        UniqueConstraint("id", "school_id", name="uq_exams_id_school_id"),
        UniqueConstraint(
            "id", "school_id", "academic_year_id", "class_id", name="uq_exams_placement_key"
        ),
        _fk(
            ["academic_year_id", "school_id"],
            "academic_years",
            ["id", "school_id"],
            "fk_exams_year_same_school",
        ),
        _fk(
            ["class_id", "school_id"], "classes", ["id", "school_id"], "fk_exams_class_same_school"
        ),
        # MATCH SIMPLE: a class-wide exam (NULL section) skips these two checks by design.
        _fk(
            ["section_id", "school_id"],
            "sections",
            ["id", "school_id"],
            "fk_exams_section_same_school",
        ),
        _fk(
            ["section_id", "class_id"], "sections", ["id", "class_id"], "fk_exams_section_in_class"
        ),
        # One live exam of a given name per (year, class, section or whole class).
        Index(
            "uq_exams_name_per_placement",
            "academic_year_id",
            "class_id",
            "section_id",
            "name",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("status <> 'cancelled'"),
        ),
        Index("ix_exams_placement", "school_id", "academic_year_id", "class_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID | None] = mapped_column()
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(String(1000))
    exam_type: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(20), default=ExamStatus.DRAFT.value, server_default=ExamStatus.DRAFT.value
    )
    # Optional overall pass threshold; NULL -> only component passing marks decide.
    pass_percentage: Mapped[Decimal | None] = mapped_column(PERCENT)
    # Optional grading scale: [{"grade": "A1", "min_percentage": "91.00"}, ...].
    grade_bands: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_by_user_id: Mapped[uuid.UUID] = _user_fk()
    cancelled_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))


class ExamSubject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One subject (assessment component) of an exam."""

    __tablename__ = "exam_subjects"
    __table_args__ = (
        CheckConstraint("max_marks > 0 AND max_marks <= 1000", name="max_marks_range"),
        CheckConstraint(
            "pass_marks IS NULL OR (pass_marks >= 0 AND pass_marks <= max_marks)",
            name="pass_marks_range",
        ),
        CheckConstraint(
            "start_time IS NULL OR end_time IS NULL OR start_time < end_time",
            name="times_ordered",
        ),
        UniqueConstraint("exam_id", "subject_id"),
        # Composite FK target: a mark repeats its component's exam, school and maximum.
        UniqueConstraint(
            "id", "exam_id", "school_id", "max_marks", name="uq_exam_subjects_marks_key"
        ),
        _fk(
            ["exam_id", "school_id"],
            "exams",
            ["id", "school_id"],
            "fk_exam_subjects_exam_same_school",
        ),
        _fk(
            ["subject_id", "school_id"],
            "subjects",
            ["id", "school_id"],
            "fk_exam_subjects_subject_same_school",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    exam_id: Mapped[uuid.UUID] = mapped_column()
    subject_id: Mapped[uuid.UUID] = mapped_column()
    max_marks: Mapped[Decimal] = mapped_column(MARKS)
    pass_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    assessment_date: Mapped[date | None] = mapped_column(Date)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    created_by_user_id: Mapped[uuid.UUID] = _user_fk()


class ExamMark(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One student's result in one exam component.

    ``corrected_*`` describe the latest correction; every correction is appended to
    :class:`ExamMarkChange`.
    """

    __tablename__ = "exam_marks"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(MarkStatus)})", name="status_valid"),
        CheckConstraint(
            "(status = 'present') = (marks_obtained IS NOT NULL)", name="marks_match_status"
        ),
        CheckConstraint(
            "marks_obtained IS NULL OR (marks_obtained >= 0 AND marks_obtained <= max_marks)",
            name="marks_range",
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
        UniqueConstraint("exam_subject_id", "student_id"),
        # Composite FK target for the correction history.
        UniqueConstraint("id", "school_id", name="uq_exam_marks_id_school_id"),
        _fk(
            ["exam_subject_id", "exam_id", "school_id", "max_marks"],
            "exam_subjects",
            ["id", "exam_id", "school_id", "max_marks"],
            "fk_exam_marks_component",
        ),
        _fk(EXAM_PLACEMENT, "exams", EXAM_TARGET, "fk_exam_marks_exam_placement"),
        _fk(
            ENROLLMENT_PLACEMENT,
            "student_enrollments",
            ENROLLMENT_TARGET,
            "fk_exam_marks_enrollment_placement",
        ),
        Index("ix_exam_marks_student_id", "student_id"),
        Index("ix_exam_marks_exam_id", "exam_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    exam_id: Mapped[uuid.UUID] = mapped_column()
    exam_subject_id: Mapped[uuid.UUID] = mapped_column()
    max_marks: Mapped[Decimal] = mapped_column(MARKS)
    student_id: Mapped[uuid.UUID] = mapped_column()
    enrollment_id: Mapped[uuid.UUID] = mapped_column()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(10))
    marks_obtained: Mapped[Decimal | None] = mapped_column(MARKS)
    remarks: Mapped[str | None] = mapped_column(String(500))
    entered_by_user_id: Mapped[uuid.UUID] = _user_fk()
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    corrected_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_reason: Mapped[str | None] = mapped_column(String(500))


class ExamMarkChange(UUIDPrimaryKeyMixin, Base):
    """Append-only: one row per correction of a mark, numbered 1, 2, 3... per mark."""

    __tablename__ = "exam_mark_changes"
    __table_args__ = (
        CheckConstraint(
            f"previous_status IN ({_values(MarkStatus)})", name="previous_status_valid"
        ),
        CheckConstraint(f"new_status IN ({_values(MarkStatus)})", name="new_status_valid"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_not_blank"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        UniqueConstraint("mark_id", "sequence"),
        _fk(
            ["mark_id", "school_id"],
            "exam_marks",
            ["id", "school_id"],
            "fk_exam_mark_changes_mark_same_school",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    mark_id: Mapped[uuid.UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer)
    previous_status: Mapped[str] = mapped_column(String(10))
    new_status: Mapped[str] = mapped_column(String(10))
    previous_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    new_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    previous_remarks: Mapped[str | None] = mapped_column(String(500))
    new_remarks: Mapped[str | None] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(String(500))
    changed_by_user_id: Mapped[uuid.UUID] = _user_fk()
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamPublication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One publication of an exam's results (version 1, 2, ...).

    ``settings`` freezes the exam configuration used for the calculation (components,
    maxima, passing marks, pass percentage, grade bands, names). At most one
    publication per exam is ``active``; withdrawn ones remain as history.
    """

    __tablename__ = "exam_publications"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(PublicationStatus)})", name="status_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL) "
            "AND (withdrawn_at IS NULL) = (withdrawn_by_user_id IS NULL) "
            "AND (withdrawn_at IS NULL) = (withdrawal_reason IS NULL)",
            name="withdrawal_complete",
        ),
        CheckConstraint(
            "withdrawal_reason IS NULL OR length(trim(withdrawal_reason)) > 0",
            name="withdrawal_reason_not_blank",
        ),
        UniqueConstraint("exam_id", "version"),
        UniqueConstraint("id", "exam_id", "school_id", name="uq_exam_publications_key"),
        _fk(
            ["exam_id", "school_id"],
            "exams",
            ["id", "school_id"],
            "fk_exam_publications_exam_same_school",
        ),
        Index(
            "uq_exam_publications_one_active",
            "exam_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    exam_id: Mapped[uuid.UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20),
        default=PublicationStatus.ACTIVE.value,
        server_default=PublicationStatus.ACTIVE.value,
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_by_user_id: Mapped[uuid.UUID] = _user_fk()
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawn_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawal_reason: Mapped[str | None] = mapped_column(String(500))


class ExamReportCard(UUIDPrimaryKeyMixin, Base):
    """A student's result frozen at publication (immutable).

    ``subjects`` holds one line per component (decimal values stored as strings).
    """

    __tablename__ = "exam_report_cards"
    __table_args__ = (
        CheckConstraint(f"result IN ({_values(ExamResult)})", name="result_valid"),
        CheckConstraint("total_obtained >= 0 AND total_obtained <= total_max", name="totals_range"),
        CheckConstraint(
            "percentage IS NULL OR percentage BETWEEN 0 AND 100", name="percentage_range"
        ),
        UniqueConstraint("publication_id", "student_id"),
        _fk(
            ["publication_id", "exam_id", "school_id"],
            "exam_publications",
            ["id", "exam_id", "school_id"],
            "fk_exam_report_cards_publication",
        ),
        _fk(EXAM_PLACEMENT, "exams", EXAM_TARGET, "fk_exam_report_cards_exam_placement"),
        _fk(
            ENROLLMENT_PLACEMENT,
            "student_enrollments",
            ENROLLMENT_TARGET,
            "fk_exam_report_cards_enrollment_placement",
        ),
        Index("ix_exam_report_cards_student_id", "student_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    publication_id: Mapped[uuid.UUID] = mapped_column()
    exam_id: Mapped[uuid.UUID] = mapped_column()
    student_id: Mapped[uuid.UUID] = mapped_column()
    enrollment_id: Mapped[uuid.UUID] = mapped_column()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    total_obtained: Mapped[Decimal] = mapped_column(TOTAL)
    total_max: Mapped[Decimal] = mapped_column(TOTAL)
    percentage: Mapped[Decimal | None] = mapped_column(PERCENT)
    grade: Mapped[str | None] = mapped_column(String(10))
    result: Mapped[str] = mapped_column(String(20))
    subjects: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
