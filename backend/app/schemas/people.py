"""Request/response models for students, parents, links and enrollments."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.models.people import Gender, RelationshipType, StudentStatus
from app.schemas.fields import (
    AdmissionNumber,
    Email,
    PatchInput,
    Phone,
    PostalCode,
    StrictInput,
    Text30,
    Text100,
    Text500,
)

_PERSONAL_NULLABLE = frozenset(
    {
        "middle_name",
        "date_of_birth",
        "gender",
        "email",
        "phone",
        "address",
        "city",
        "state",
        "postal_code",
        "country",
        "admission_date",
    }
)


def _not_future(value: date | None) -> date | None:
    if value is not None and value > datetime.now(UTC).date():
        raise ValueError("Must not be in the future.")
    return value


# ========================================================================== students
class _StudentFields(StrictInput):
    first_name: Text100
    middle_name: Text100 | None = None
    last_name: Text100
    date_of_birth: date | None = None
    gender: Gender | None = None
    email: Email | None = None
    phone: Phone | None = None
    address: Text500 | None = None
    city: Text100 | None = None
    state: Text100 | None = None
    postal_code: PostalCode | None = None
    country: Text100 | None = None
    admission_date: date | None = None

    _dob = field_validator("date_of_birth")(_not_future)


class StudentCreate(_StudentFields):
    admission_number: AdmissionNumber = Field(
        description="Canonical, school-unique student identifier; also the login ID. "
        "Trimmed and upper-cased. Cannot be changed later.",
        examples=["2026/0001"],
    )
    # Optional: creates the student's login account (Argon2id-hashed; never returned).
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)


class StudentUpdate(PatchInput):
    """``admission_number`` (the login ID) and passwords are not changeable here."""

    nullable_fields: ClassVar[frozenset[str]] = _PERSONAL_NULLABLE

    first_name: Text100 | None = None
    middle_name: Text100 | None = None
    last_name: Text100 | None = None
    date_of_birth: date | None = None
    gender: Gender | None = None
    email: Email | None = None
    phone: Phone | None = None
    address: Text500 | None = None
    city: Text100 | None = None
    state: Text100 | None = None
    postal_code: PostalCode | None = None
    country: Text100 | None = None
    admission_date: date | None = None
    # Changing status requires the student.deactivate permission.
    status: StudentStatus | None = None

    _dob = field_validator("date_of_birth")(_not_future)


class StudentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    admission_number: str
    first_name: str
    middle_name: str | None
    last_name: str
    date_of_birth: date | None
    gender: str | None
    email: str | None
    phone: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    admission_date: date | None
    status: str
    has_login: bool
    created_at: datetime
    updated_at: datetime


class StudentSummary(BaseModel):
    id: uuid.UUID
    admission_number: str
    first_name: str
    last_name: str
    status: str


# =========================================================================== parents
class ParentCreate(StrictInput):
    first_name: Text100
    last_name: Text100
    mobile: Phone = Field(description="Used for OTP sign-in; unique across the platform.")
    email: Email | None = None
    occupation: Text100 | None = None
    address: Text500 | None = None
    city: Text100 | None = None
    state: Text100 | None = None
    postal_code: PostalCode | None = None
    country: Text100 | None = None


class ParentUpdate(PatchInput):
    nullable_fields: ClassVar[frozenset[str]] = frozenset(
        {"email", "occupation", "address", "city", "state", "postal_code", "country"}
    )

    first_name: Text100 | None = None
    last_name: Text100 | None = None
    mobile: Phone | None = None
    email: Email | None = None
    occupation: Text100 | None = None
    address: Text500 | None = None
    city: Text100 | None = None
    state: Text100 | None = None
    postal_code: PostalCode | None = None
    country: Text100 | None = None
    # Changing this requires the user.deactivate permission.
    is_active: bool | None = None


class ParentRead(BaseModel):
    id: uuid.UUID
    school_id: uuid.UUID
    first_name: str
    last_name: str
    mobile: str | None
    email: str | None
    occupation: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ParentSummary(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: str
    mobile: str | None
    is_active: bool


# ============================================================ student-parent links
class StudentParentCreate(StrictInput):
    parent_id: uuid.UUID
    relationship_type: RelationshipType
    is_primary: bool = False
    is_guardian: bool = True


class StudentParentUpdate(PatchInput):
    relationship_type: RelationshipType | None = None
    is_primary: bool | None = None
    is_guardian: bool | None = None
    is_active: bool | None = None


class StudentParentRead(BaseModel):
    student_id: uuid.UUID
    parent_id: uuid.UUID
    relationship_type: str
    is_primary: bool
    is_guardian: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    parent: ParentSummary | None = None
    student: StudentSummary | None = None


# ======================================================================= enrollments
class EnrollmentCreate(StrictInput):
    student_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    enrollment_number: Text30 | None = None
    enrollment_date: date
    is_current: bool = Field(
        default=False, description="Make this the student's current enrollment (atomically)."
    )
    supersedes_enrollment_id: uuid.UUID | None = Field(
        default=None,
        description="Required to move a student who already has an active enrollment in "
        "this academic year (class/section change). The old enrollment is closed as "
        "'superseded', never overwritten.",
    )


class EnrollmentUpdate(PatchInput):
    """Year, class, section and student are immutable: history is never rewritten."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"enrollment_number", "end_date"})

    enrollment_number: Text30 | None = None
    enrollment_date: date | None = None
    end_date: date | None = None
    status: Literal["active", "completed", "withdrawn"] | None = None
    is_current: bool | None = None


class EnrollmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    student_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    enrollment_number: str | None
    enrollment_date: date
    end_date: date | None
    status: str
    is_current: bool
    created_at: datetime
    updated_at: datetime
