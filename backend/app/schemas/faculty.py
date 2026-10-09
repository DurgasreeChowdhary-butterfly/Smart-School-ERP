"""Request/response models for faculty and faculty assignments."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.models.faculty import AssignmentStatus, FacultyStatus
from app.schemas.fields import Email, EmployeeId, PatchInput, Phone, StrictInput, Text100


def _ordered(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start >= end:
        raise ValueError("effective_from must be before effective_to.")


# =========================================================================== faculty
class FacultyCreate(StrictInput):
    """Create a faculty record and its FACULTY login, or attach an existing FACULTY user.

    * New account: ``password`` plus ``email`` and/or ``mobile`` (the sign-in IDs).
    * Existing account: ``user_id`` of a FACULTY user of the same school that has no
      faculty record yet; ``email``/``mobile``/``password`` must then be omitted.
    """

    employee_id: EmployeeId = Field(
        description="School-unique staff identifier. Trimmed and upper-cased.",
        examples=["EMP-0042"],
    )
    first_name: Text100
    middle_name: Text100 | None = None
    last_name: Text100
    designation: Text100 | None = None
    department: Text100 | None = None
    joining_date: date | None = None
    email: Email | None = None
    mobile: Phone | None = None
    # Argon2id-hashed into users.password_hash; never stored elsewhere or returned.
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    user_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _account_source(self) -> FacultyCreate:
        if self.user_id is not None:
            if self.email is not None or self.mobile is not None or self.password is not None:
                raise ValueError("Omit email, mobile and password when attaching user_id.")
            return self
        if self.password is None:
            raise ValueError("password is required to create the faculty login.")
        if self.email is None and self.mobile is None:
            raise ValueError("email or mobile is required (used to sign in).")
        return self


class FacultyUpdate(PatchInput):
    """Passwords are not changeable here. ``status`` (de)activates the faculty member
    and their login; their assignments stay as recorded but grant nothing while the
    faculty member is inactive."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset(
        {"middle_name", "designation", "department", "joining_date", "email", "mobile"}
    )

    employee_id: EmployeeId | None = None
    first_name: Text100 | None = None
    middle_name: Text100 | None = None
    last_name: Text100 | None = None
    designation: Text100 | None = None
    department: Text100 | None = None
    joining_date: date | None = None
    email: Email | None = None
    mobile: Phone | None = None
    status: FacultyStatus | None = None


class FacultyRead(BaseModel):
    id: uuid.UUID
    school_id: uuid.UUID
    user_id: uuid.UUID
    employee_id: str
    first_name: str
    middle_name: str | None
    last_name: str
    designation: str | None
    department: str | None
    joining_date: date | None
    email: str | None
    mobile: str | None
    status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ======================================================================= assignments
class FacultyAssignmentCreate(StrictInput):
    faculty_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID
    effective_from: date | None = Field(
        default=None, description="Inclusive; must fall within the academic year."
    )
    effective_to: date | None = Field(
        default=None, description="Inclusive; after effective_from, within the academic year."
    )

    @model_validator(mode="after")
    def _dates(self) -> FacultyAssignmentCreate:
        _ordered(self.effective_from, self.effective_to)
        return self


class FacultyAssignmentUpdate(PatchInput):
    """Faculty, year, class, section and subject are immutable: to change what someone
    teaches, deactivate this assignment and create another (history is kept)."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"effective_from", "effective_to"})

    effective_from: date | None = None
    effective_to: date | None = None
    status: AssignmentStatus | None = None


class FacultyAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    faculty_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID
    effective_from: date | None
    effective_to: date | None
    status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
