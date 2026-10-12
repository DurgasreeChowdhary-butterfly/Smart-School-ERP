"""Request/response models for assignments, submissions and reviews.

``due_at`` must carry a time-zone offset (``2026-09-05T23:59:00+05:30`` or ``...Z``): a
deadline is an absolute instant, so naive date-times are rejected rather than guessed.
Marks follow the exam rules: ``Decimal`` with at most two decimals, rejected (never
rounded) otherwise, returned as JSON numbers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, ClassVar

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.models.assignments import MAX_CONTENT
from app.schemas.exams import MarksValue, MaxMarks, Number
from app.schemas.fields import PatchInput, StrictInput, Text200, Text500


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Must not contain NUL characters.")
    return value


Instructions = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
    AfterValidator(_no_nul),
]
Content = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CONTENT),
    AfterValidator(_no_nul),
]
Feedback = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000),
    AfterValidator(_no_nul),
]


# ==================================================================== assignments
class AssignmentCreate(StrictInput):
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID
    title: Text200
    instructions: Instructions
    assigned_on: date = Field(description="School-local date the work is set (in the year).")
    due_at: AwareDatetime = Field(
        description="Deadline instant with a time-zone offset; on or after assigned_on in the "
        "school's time zone and within the academic year."
    )
    allow_late: bool = False
    allow_resubmission: bool = True
    max_marks: MaxMarks | None = Field(
        default=None, description="Set to grade submissions; omit for feedback-only work."
    )


class AssignmentUpdate(PatchInput):
    """Draft: every field. Published: title, instructions, due_at, allow_late and
    allow_resubmission. Closed: read-only. Placement is immutable."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"max_marks"})

    title: Text200 | None = None
    instructions: Instructions | None = None
    assigned_on: date | None = None
    due_at: AwareDatetime | None = None
    allow_late: bool | None = None
    allow_resubmission: bool | None = None
    max_marks: MaxMarks | None = None


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    subject_id: uuid.UUID
    title: str
    instructions: str
    assigned_on: date
    due_at: datetime
    allow_late: bool
    allow_resubmission: bool
    max_marks: Number | None
    status: str
    created_by_user_id: uuid.UUID
    published_by_user_id: uuid.UUID | None
    published_at: datetime | None
    closed_by_user_id: uuid.UUID | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ==================================================================== submissions
class SubmissionCreate(StrictInput):
    content: Content


class SubmissionRead(BaseModel):
    """``content`` is the latest revision's text; it is withheld (``null``) for parents."""

    id: uuid.UUID
    school_id: uuid.UUID
    assignment_id: uuid.UUID
    student_id: uuid.UUID
    enrollment_id: uuid.UUID
    section_id: uuid.UUID
    status: str
    revision_count: int
    first_submitted_at: datetime
    last_submitted_at: datetime
    is_late: bool
    content: str | None
    max_marks: Number | None
    marks_awarded: Number | None
    feedback: str | None
    reviewed_revision: int | None
    reviewed_by_user_id: uuid.UUID | None
    reviewed_at: datetime | None


class RevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_id: uuid.UUID
    revision: int
    content: str
    submitted_by_user_id: uuid.UUID
    submitted_at: datetime
    due_at: datetime
    is_late: bool


class ReviewInput(StrictInput):
    """Record feedback and/or marks for the latest revision. Omitted fields keep their
    current value; ``null`` clears. Changing an existing review requires ``reason``."""

    marks_awarded: MarksValue | None = None
    feedback: Feedback | None = None
    reason: Text500 | None = None

    @model_validator(mode="after")
    def _something(self) -> ReviewInput:
        if not self.model_fields_set & {"marks_awarded", "feedback"}:
            raise ValueError("Provide marks_awarded and/or feedback.")
        return self


class ReviewChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_id: uuid.UUID
    sequence: int
    revision: int
    previous_marks: Number | None
    new_marks: Number | None
    previous_feedback: str | None
    new_feedback: str | None
    reason: str | None
    changed_by_user_id: uuid.UUID
    changed_at: datetime


class AssignmentStudent(BaseModel):
    """An eligible student and their submission state (``not_submitted`` when none)."""

    student_id: uuid.UUID
    enrollment_id: uuid.UUID
    section_id: uuid.UUID
    admission_number: str
    first_name: str
    last_name: str
    submission_id: uuid.UUID | None
    submission_status: str
    is_late: bool | None
    marks_awarded: Number | None
