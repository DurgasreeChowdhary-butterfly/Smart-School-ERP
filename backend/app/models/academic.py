"""Academic structure: academic years, classes (grades), sections and subjects.

Hierarchy::

    School ── AcademicYear
           ├─ SchoolClass ── Section
           └─ Subject

Integrity rules enforced by PostgreSQL (not only by services), so they hold
under concurrent requests:

* academic years of one school never overlap (exclusion constraint over a
  ``daterange``) and at most one is current (partial unique index);
* a section's class belongs to the same school as the section (composite FK
  ``(class_id, school_id) -> classes(id, school_id)``);
* codes are unique within their scope and stored upper-case.

Foreign keys use ``ON DELETE RESTRICT``: nothing here is hard-deleted.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _school_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"), index=True)


class AcademicYear(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "academic_years"
    __table_args__ = (
        CheckConstraint("start_date < end_date", name="dates_ordered"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        # A current year must be active.
        CheckConstraint("NOT is_current OR is_active", name="current_is_active"),
        UniqueConstraint("school_id", "name"),
        # Composite FK target: (academic_year_id, school_id) references (Phase 4).
        UniqueConstraint("id", "school_id", name="uq_academic_years_id_school_id"),
        Index(
            "uq_academic_years_one_current_per_school",
            "school_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        # Inclusive ranges: a year ending on 31 May and the next starting on 1 June are
        # fine; sharing any single day is an overlap. Requires the btree_gist extension.
        ExcludeConstraint(
            ("school_id", "="),
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            name="ex_academic_years_no_overlap",
            using="gist",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    name: Mapped[str] = mapped_column(String(50))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class SchoolClass(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A grade level such as "Class 10" (``class`` is a Python keyword)."""

    __tablename__ = "classes"
    __table_args__ = (
        CheckConstraint("code = upper(code)", name="code_uppercase"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("display_order >= 0", name="display_order_non_negative"),
        UniqueConstraint("school_id", "code"),
        # Target of the composite FK that pins sections to their class's school.
        UniqueConstraint("id", "school_id", name="uq_classes_id_school_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    name: Mapped[str] = mapped_column(String(100))
    code: Mapped[str] = mapped_column(String(30))
    display_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Section(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sections"
    __table_args__ = (
        CheckConstraint("code = upper(code)", name="code_uppercase"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="capacity_positive"),
        UniqueConstraint("class_id", "code"),
        # Composite FK targets: same-school and section-belongs-to-class references.
        UniqueConstraint("id", "school_id", name="uq_sections_id_school_id"),
        UniqueConstraint("id", "class_id", name="uq_sections_id_class_id"),
        ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_sections_class_same_school",
            ondelete="RESTRICT",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    class_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(50))
    code: Mapped[str] = mapped_column(String(30))
    capacity: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Subject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subjects"
    __table_args__ = (
        CheckConstraint("code = upper(code)", name="code_uppercase"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        UniqueConstraint("school_id", "code"),
        UniqueConstraint("id", "school_id", name="uq_subjects_id_school_id"),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    name: Mapped[str] = mapped_column(String(150))
    code: Mapped[str] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(String(1000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
