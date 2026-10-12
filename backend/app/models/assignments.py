"""Assignments (homework/coursework), student submissions, their revisions and the
history of reviews (feedback and grades).

Model
-----
An assignment belongs to one school, academic year, class, **section** and **subject**:
exactly the placement a Phase 5 faculty assignment covers. Lifecycle::

    draft --publish--> published --close--> closed

Students submit text work (file attachments are not supported: the platform has no
secure file storage yet). Each student has at most one submission per assignment; every
(re)submission is an append-only *revision* that snapshots the due time, so lateness is
fixed when the work is handed in. Reviews (feedback, and marks when the assignment has
``max_marks``) are kept on the submission, and every change to a review is appended to
``assignment_review_changes``.

Integrity rules enforced by PostgreSQL (not only by services)
--------------------------------------------------------------
* every reference is a composite foreign key carrying ``school_id``; a submission repeats
  its assignment's (school, year, class, section) and points at an enrollment with the
  same values, so it can only exist for a student placed in that section;
* a submission carries its assignment's ``max_marks`` through a composite foreign key, so
  ``marks_awarded <= max_marks`` is a CHECK;
* triggers (migration 0008) enforce the lifecycle, keep placement and grading
  configuration immutable after publication, accept revisions only while the assignment
  is published, and keep revisions and review history append-only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AssignmentLifecycle(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"


class SubmissionStatus(StrEnum):
    SUBMITTED = "submitted"  # waiting for review
    REVIEWED = "reviewed"  # feedback and/or marks recorded


MARKS = Numeric(6, 2)
MAX_CONTENT = 20_000


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


PLACEMENT = ["school_id", "academic_year_id", "class_id", "section_id"]


class Assignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(AssignmentLifecycle)})", name="status_valid"),
        CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
        CheckConstraint("length(trim(instructions)) > 0", name="instructions_not_blank"),
        CheckConstraint(
            "max_marks IS NULL OR (max_marks > 0 AND max_marks <= 1000)", name="max_marks_range"
        ),
        CheckConstraint(
            "(status = 'draft') = (published_at IS NULL) "
            "AND (published_at IS NULL) = (published_by_user_id IS NULL)",
            name="publication_complete",
        ),
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL) "
            "AND (closed_at IS NULL) = (closed_by_user_id IS NULL)",
            name="closure_complete",
        ),
        # Composite FK targets for submissions.
        UniqueConstraint("id", *PLACEMENT, name="uq_assignments_placement_key"),
        UniqueConstraint("id", "school_id", "max_marks", name="uq_assignments_marks_key"),
        _fk(
            ["academic_year_id", "school_id"],
            "academic_years",
            ["id", "school_id"],
            "fk_assignments_year_same_school",
        ),
        _fk(
            ["class_id", "school_id"],
            "classes",
            ["id", "school_id"],
            "fk_assignments_class_same_school",
        ),
        _fk(
            ["section_id", "school_id"],
            "sections",
            ["id", "school_id"],
            "fk_assignments_section_same_school",
        ),
        _fk(
            ["section_id", "class_id"],
            "sections",
            ["id", "class_id"],
            "fk_assignments_section_in_class",
        ),
        _fk(
            ["subject_id", "school_id"],
            "subjects",
            ["id", "school_id"],
            "fk_assignments_subject_same_school",
        ),
        Index(
            "ix_assignments_placement", "school_id", "academic_year_id", "section_id", "subject_id"
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    subject_id: Mapped[uuid.UUID] = mapped_column()
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text)
    # School-local calendar date the work is set; due_at is an absolute instant.
    assigned_on: Mapped[date] = mapped_column(Date)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    allow_late: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    allow_resubmission: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # NULL -> feedback only (ungraded assignment).
    max_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    status: Mapped[str] = mapped_column(
        String(20),
        default=AssignmentLifecycle.DRAFT.value,
        server_default=AssignmentLifecycle.DRAFT.value,
    )
    created_by_user_id: Mapped[uuid.UUID] = _user_fk()
    published_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssignmentSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A student's work for an assignment (one per student). The content lives in its
    revisions; the review (feedback and marks) lives here."""

    __tablename__ = "assignment_submissions"
    __table_args__ = (
        CheckConstraint("revision_count >= 1", name="revision_count_positive"),
        CheckConstraint(
            "marks_awarded IS NULL OR (max_marks IS NOT NULL AND marks_awarded >= 0 "
            "AND marks_awarded <= max_marks)",
            name="marks_range",
        ),
        CheckConstraint(
            "(reviewed_at IS NULL) = (reviewed_by_user_id IS NULL) "
            "AND (reviewed_at IS NULL) = (reviewed_revision IS NULL)",
            name="review_complete",
        ),
        CheckConstraint(
            "reviewed_at IS NOT NULL OR (marks_awarded IS NULL AND feedback IS NULL)",
            name="review_fields_need_review",
        ),
        CheckConstraint(
            "reviewed_revision IS NULL OR reviewed_revision BETWEEN 1 AND revision_count",
            name="reviewed_revision_exists",
        ),
        CheckConstraint(
            "feedback IS NULL OR length(trim(feedback)) > 0", name="feedback_not_blank"
        ),
        UniqueConstraint("assignment_id", "student_id"),
        UniqueConstraint("id", "school_id", name="uq_assignment_submissions_id_school_id"),
        _fk(
            ["assignment_id", *PLACEMENT],
            "assignments",
            ["id", *PLACEMENT],
            "fk_assignment_submissions_assignment_placement",
        ),
        # MATCH SIMPLE: for an ungraded assignment (NULL maximum) the CHECK above forbids marks.
        _fk(
            ["assignment_id", "school_id", "max_marks"],
            "assignments",
            ["id", "school_id", "max_marks"],
            "fk_assignment_submissions_max_marks",
        ),
        _fk(
            [
                "enrollment_id",
                "school_id",
                "student_id",
                "academic_year_id",
                "class_id",
                "section_id",
            ],
            "student_enrollments",
            ["id", "school_id", "student_id", "academic_year_id", "class_id", "section_id"],
            "fk_assignment_submissions_enrollment_placement",
        ),
        Index("ix_assignment_submissions_student_id", "student_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    assignment_id: Mapped[uuid.UUID] = mapped_column()
    student_id: Mapped[uuid.UUID] = mapped_column()
    enrollment_id: Mapped[uuid.UUID] = mapped_column()
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    max_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    revision_count: Mapped[int] = mapped_column(Integer, default=1)
    first_submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_late: Mapped[bool] = mapped_column(Boolean)  # the latest revision's lateness
    status: Mapped[str] = mapped_column(
        String(20),
        Computed(
            "CASE WHEN reviewed_at IS NULL THEN 'submitted' ELSE 'reviewed' END", persisted=True
        ),
    )
    marks_awarded: Mapped[Decimal | None] = mapped_column(MARKS)
    feedback: Mapped[str | None] = mapped_column(Text)
    reviewed_revision: Mapped[int | None] = mapped_column(Integer)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = _user_fk(nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssignmentSubmissionRevision(UUIDPrimaryKeyMixin, Base):
    """Append-only: one row per (re)submission, numbered 1, 2, 3... per submission."""

    __tablename__ = "assignment_submission_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            f"length(trim(content)) > 0 AND length(content) <= {MAX_CONTENT}", name="content_valid"
        ),
        UniqueConstraint("submission_id", "revision"),
        _fk(
            ["submission_id", "school_id"],
            "assignment_submissions",
            ["id", "school_id"],
            "fk_assignment_submission_revisions_submission_same_school",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    submission_id: Mapped[uuid.UUID] = mapped_column()
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    submitted_by_user_id: Mapped[uuid.UUID] = _user_fk()
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The assignment's due time when this revision was handed in (lateness is fixed then).
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_late: Mapped[bool] = mapped_column(
        Boolean, Computed("submitted_at > due_at", persisted=True)
    )


class AssignmentReviewChange(UUIDPrimaryKeyMixin, Base):
    """Append-only: one row per review or review change, numbered per submission.

    The first review needs no reason; every later change does.
    """

    __tablename__ = "assignment_review_changes"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("sequence = 1 OR reason IS NOT NULL", name="reason_required_for_change"),
        CheckConstraint("reason IS NULL OR length(trim(reason)) > 0", name="reason_not_blank"),
        UniqueConstraint("submission_id", "sequence"),
        _fk(
            ["submission_id", "school_id"],
            "assignment_submissions",
            ["id", "school_id"],
            "fk_assignment_review_changes_submission_same_school",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    submission_id: Mapped[uuid.UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    previous_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    new_marks: Mapped[Decimal | None] = mapped_column(MARKS)
    previous_feedback: Mapped[str | None] = mapped_column(Text)
    new_feedback: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(500))
    changed_by_user_id: Mapped[uuid.UUID] = _user_fk()
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
