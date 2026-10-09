"""Faculty and faculty assignments (what each faculty member teaches).

Identity model
--------------
``users`` remains the single authentication identity. ``faculty.user_id`` is
required and unique and points at a FACULTY user of the same school; email,
mobile and the password hash live on ``users`` only. Faculty sign in with the
existing Phase 2 password login (email or mobile + password).

Scope model
-----------
A faculty assignment is the tuple *(faculty, academic year, class, section,
subject)*. It is the **only** source of a faculty member's resource scope (see
:mod:`app.security.faculty_scope`).

Integrity rules enforced by PostgreSQL (not only by services):

* every reference carries ``school_id`` and targets ``(id, school_id)`` of the
  referenced table, so an assignment can never mix schools;
* ``(section_id, class_id)`` targets ``sections(id, class_id)``: the section must
  belong to the assignment's class;
* at most one *active* assignment per (faculty, year, class, section, subject)
  (partial unique index); inactive rows are retained as history;
* employee IDs are unique per school and stored upper-case.

``is_active`` is derived from ``status`` by PostgreSQL (generated column), as on
``users``, so the two can never disagree. Nothing here is hard-deleted.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FacultyStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class AssignmentStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


def _school_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"), index=True)


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


class Faculty(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "faculty"
    __table_args__ = (
        CheckConstraint("employee_id = upper(employee_id)", name="employee_id_upper"),
        CheckConstraint("length(trim(employee_id)) > 0", name="employee_id_not_blank"),
        CheckConstraint("length(trim(first_name)) > 0", name="first_name_not_blank"),
        CheckConstraint("length(trim(last_name)) > 0", name="last_name_not_blank"),
        CheckConstraint(f"status IN ({_values(FacultyStatus)})", name="status_valid"),
        UniqueConstraint("school_id", "employee_id"),
        UniqueConstraint("user_id"),
        # Composite FK target: assignments can only reference faculty of their school.
        UniqueConstraint("id", "school_id", name="uq_faculty_id_school_id"),
        ForeignKeyConstraint(
            ["user_id", "school_id"],
            ["users.id", "users.school_id"],
            name="fk_faculty_user_same_school",
            ondelete="RESTRICT",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    user_id: Mapped[uuid.UUID] = mapped_column()
    employee_id: Mapped[str] = mapped_column(String(30))
    first_name: Mapped[str] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    designation: Mapped[str | None] = mapped_column(String(100))
    department: Mapped[str | None] = mapped_column(String(100))
    joining_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(20), default=FacultyStatus.ACTIVE.value, server_default=FacultyStatus.ACTIVE.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, Computed("status = 'active'", persisted=True))

    @property
    def full_name(self) -> str:
        parts = (self.first_name, self.middle_name, self.last_name)
        return " ".join(part for part in parts if part)


class FacultyAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Faculty member + academic year + class + section + subject.

    The placement columns (faculty, year, class, section, subject) are immutable;
    a change of what someone teaches ends one assignment and creates another.
    """

    __tablename__ = "faculty_assignments"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(AssignmentStatus)})", name="status_valid"),
        CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to",
            name="effective_dates_ordered",
        ),
        ForeignKeyConstraint(
            ["faculty_id", "school_id"],
            ["faculty.id", "faculty.school_id"],
            name="fk_faculty_assignments_faculty_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["academic_year_id", "school_id"],
            ["academic_years.id", "academic_years.school_id"],
            name="fk_faculty_assignments_year_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_faculty_assignments_class_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "school_id"],
            ["sections.id", "sections.school_id"],
            name="fk_faculty_assignments_section_same_school",
            ondelete="RESTRICT",
        ),
        # The section must belong to the assignment's class.
        ForeignKeyConstraint(
            ["section_id", "class_id"],
            ["sections.id", "sections.class_id"],
            name="fk_faculty_assignments_section_in_class",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subject_id", "school_id"],
            ["subjects.id", "subjects.school_id"],
            name="fk_faculty_assignments_subject_same_school",
            ondelete="RESTRICT",
        ),
        # No duplicate *active* assignment; inactive history may repeat.
        Index(
            "uq_faculty_assignments_one_active",
            "faculty_id",
            "academic_year_id",
            "class_id",
            "section_id",
            "subject_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        # Scope lookups: "which faculty teach this section?"
        Index(
            "ix_faculty_assignments_placement",
            "school_id",
            "academic_year_id",
            "class_id",
            "section_id",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    faculty_id: Mapped[uuid.UUID] = mapped_column(index=True)
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    subject_id: Mapped[uuid.UUID] = mapped_column()
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(20),
        default=AssignmentStatus.ACTIVE.value,
        server_default=AssignmentStatus.ACTIVE.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, Computed("status = 'active'", persisted=True))
