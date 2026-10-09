"""Students, parents, their relationships and enrollments.

Identity model
--------------
``users`` remains the single authentication identity (credentials, sessions,
tokens, roles). Domain records link to it:

* ``parents.user_id`` (required, unique): every parent is a PARENT user; the
  mobile number / email used for OTP sign-in live on ``users`` only.
* ``students.user_id`` (optional, unique): set when the student has a login. The
  password hash lives on ``users.password_hash`` only. Students sign in with their
  school code + admission number, which resolves to this user.

Same-school integrity is enforced by PostgreSQL with composite foreign keys:
every cross-table reference carries ``school_id`` and targets ``(id, school_id)``
of the referenced table, so rows of different schools can never be linked.
Nothing here is hard-deleted (``ON DELETE RESTRICT``; deactivate instead).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class StudentStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    TRANSFERRED = "transferred"
    GRADUATED = "graduated"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"


class RelationshipType(StrEnum):
    MOTHER = "mother"
    FATHER = "father"
    GUARDIAN = "guardian"
    OTHER = "other"


class EnrollmentStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    WITHDRAWN = "withdrawn"
    # Replaced by a later enrollment in the same academic year (class/section change).
    SUPERSEDED = "superseded"


def _school_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("schools.id", ondelete="RESTRICT"), index=True)


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


class Student(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "students"
    __table_args__ = (
        CheckConstraint(
            "admission_number = upper(admission_number)", name="admission_number_upper"
        ),
        CheckConstraint("length(trim(first_name)) > 0", name="first_name_not_blank"),
        CheckConstraint("length(trim(last_name)) > 0", name="last_name_not_blank"),
        CheckConstraint(f"status IN ({_values(StudentStatus)})", name="status_valid"),
        CheckConstraint(f"gender IN ({_values(Gender)})", name="gender_valid"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{7,14}$'", name="phone_e164"),
        # The admission number is the canonical, school-scoped student identifier.
        UniqueConstraint("school_id", "admission_number"),
        UniqueConstraint("id", "school_id", name="uq_students_id_school_id"),
        UniqueConstraint("user_id"),
        ForeignKeyConstraint(
            ["user_id", "school_id"],
            ["users.id", "users.school_id"],
            name="fk_students_user_same_school",
            ondelete="RESTRICT",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    user_id: Mapped[uuid.UUID | None] = mapped_column()
    admission_number: Mapped[str] = mapped_column(String(30))
    first_name: Mapped[str] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(10))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(16))
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(100))
    admission_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(20), default=StudentStatus.ACTIVE.value, server_default=StudentStatus.ACTIVE.value
    )

    @property
    def full_name(self) -> str:
        parts = (self.first_name, self.middle_name, self.last_name)
        return " ".join(part for part in parts if part)

    @property
    def has_login(self) -> bool:
        return self.user_id is not None


class Parent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "parents"
    __table_args__ = (
        CheckConstraint("length(trim(first_name)) > 0", name="first_name_not_blank"),
        CheckConstraint("length(trim(last_name)) > 0", name="last_name_not_blank"),
        UniqueConstraint("id", "school_id", name="uq_parents_id_school_id"),
        UniqueConstraint("user_id"),
        ForeignKeyConstraint(
            ["user_id", "school_id"],
            ["users.id", "users.school_id"],
            name="fk_parents_user_same_school",
            ondelete="RESTRICT",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    user_id: Mapped[uuid.UUID] = mapped_column()
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    occupation: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class StudentParent(Base):
    """Link between a student and a parent/guardian (deactivated, never deleted)."""

    __tablename__ = "student_parents"
    __table_args__ = (
        CheckConstraint(
            f"relationship_type IN ({_values(RelationshipType)})", name="relationship_type_valid"
        ),
        ForeignKeyConstraint(
            ["student_id", "school_id"],
            ["students.id", "students.school_id"],
            name="fk_student_parents_student_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["parent_id", "school_id"],
            ["parents.id", "parents.school_id"],
            name="fk_student_parents_parent_same_school",
            ondelete="RESTRICT",
        ),
        # At most one active primary contact per student.
        Index(
            "uq_student_parents_one_primary",
            "student_id",
            unique=True,
            postgresql_where=text("is_primary AND is_active"),
        ),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    parent_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    school_id: Mapped[uuid.UUID] = _school_fk()
    relationship_type: Mapped[str] = mapped_column(String(20))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_guardian: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StudentEnrollment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One student's placement in a class/section for an academic year.

    History is append-only: class/section/year never change on an existing row.
    A mid-year move closes the old row (``superseded``) and creates a new one.
    """

    __tablename__ = "student_enrollments"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(EnrollmentStatus)})", name="status_valid"),
        CheckConstraint("NOT is_current OR status = 'active'", name="current_is_active"),
        CheckConstraint("end_date IS NULL OR end_date >= enrollment_date", name="dates_ordered"),
        # Composite FK target (migration 0006): an attendance record is pinned to the
        # enrollment its student held in exactly the session's year/class/section.
        UniqueConstraint(
            "id",
            "school_id",
            "student_id",
            "academic_year_id",
            "class_id",
            "section_id",
            name="uq_student_enrollments_placement_key",
        ),
        ForeignKeyConstraint(
            ["student_id", "school_id"],
            ["students.id", "students.school_id"],
            name="fk_enrollments_student_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["academic_year_id", "school_id"],
            ["academic_years.id", "academic_years.school_id"],
            name="fk_enrollments_year_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_enrollments_class_same_school",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["section_id", "school_id"],
            ["sections.id", "sections.school_id"],
            name="fk_enrollments_section_same_school",
            ondelete="RESTRICT",
        ),
        # The section must belong to the enrollment's class.
        ForeignKeyConstraint(
            ["section_id", "class_id"],
            ["sections.id", "sections.class_id"],
            name="fk_enrollments_section_in_class",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_student_enrollments_one_current",
            "student_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "uq_student_enrollments_one_active_per_year",
            "student_id",
            "academic_year_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_student_enrollments_placement",
            "school_id",
            "academic_year_id",
            "class_id",
            "section_id",
        ),
    )

    school_id: Mapped[uuid.UUID] = _school_fk()
    student_id: Mapped[uuid.UUID] = mapped_column(index=True)
    academic_year_id: Mapped[uuid.UUID] = mapped_column()
    class_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    enrollment_number: Mapped[str | None] = mapped_column(String(30))
    enrollment_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(20),
        default=EnrollmentStatus.ACTIVE.value,
        server_default=EnrollmentStatus.ACTIVE.value,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
