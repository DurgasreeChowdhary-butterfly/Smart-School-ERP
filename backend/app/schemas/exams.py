"""Request/response models for exams, marks, results and report cards.

Marks, maxima and percentages are ``Decimal`` with at most two decimal places: input
with more precision is rejected (never silently rounded), arithmetic is exact, and
responses carry them as JSON numbers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.exams import ExamType, MarkStatus
from app.schemas.fields import PatchInput, StrictInput, Text150, Text500, Text1000

MAX_BULK_MARKS = 200
MAX_GRADE_BANDS = 20


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


CENT = Decimal("0.01")


def _two_places(value: Decimal) -> Decimal:
    # Validated to have at most 2 decimals first, so this never rounds: 45.5 -> 45.50,
    # the exact value PostgreSQL stores.
    return value.quantize(CENT)


# A Decimal that leaves the API as a JSON number (2 decimals at most, so exact).
Number = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
MaxMarks = Annotated[
    Decimal, Field(gt=0, le=1000, max_digits=6, decimal_places=2), AfterValidator(_two_places)
]
MarksValue = Annotated[
    Decimal, Field(ge=0, le=1000, max_digits=6, decimal_places=2), AfterValidator(_two_places)
]
Percentage = Annotated[
    Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2), AfterValidator(_two_places)
]
Grade = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10)]
Status = Annotated[MarkStatus, BeforeValidator(_lower)]
Type = Annotated[ExamType, BeforeValidator(_lower)]


def _ordered(start: Any, end: Any, message: str) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError(message)


# ======================================================================== grading
class GradeBand(StrictInput):
    grade: Grade
    min_percentage: Percentage


class GradeBandRead(BaseModel):
    grade: str
    min_percentage: Number


def _check_bands(bands: list[GradeBand] | None) -> list[GradeBand] | None:
    """Bands map a percentage to the band with the highest ``min_percentage`` <= it.

    Grades and thresholds must be unique and one band must start at 0, so every
    percentage has exactly one grade. Returned highest threshold first.
    """
    if bands is None:
        return None
    if not 1 <= len(bands) <= MAX_GRADE_BANDS:
        raise ValueError(f"Provide between 1 and {MAX_GRADE_BANDS} grade bands.")
    if len({band.grade for band in bands}) != len(bands):
        raise ValueError("Grades must be unique.")
    if len({band.min_percentage for band in bands}) != len(bands):
        raise ValueError("min_percentage values must be unique.")
    if not any(band.min_percentage == 0 for band in bands):
        raise ValueError("One grade band must start at min_percentage 0.")
    return sorted(bands, key=lambda band: band.min_percentage, reverse=True)


# ========================================================================== exams
class ExamCreate(StrictInput):
    """Omit ``section_id`` for an exam taken by every section of the class."""

    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID | None = None
    name: Text150
    description: Text1000 | None = None
    exam_type: Type
    start_date: date = Field(description="Within the academic year.")
    end_date: date = Field(description="On or after start_date, within the academic year.")
    pass_percentage: Percentage | None = Field(
        default=None,
        description="Optional overall threshold. Without it, only subject passing marks decide.",
    )
    grade_bands: list[GradeBand] | None = Field(
        default=None,
        description="Optional grading scale. Grades are only reported when this is set.",
    )

    _bands = field_validator("grade_bands")(_check_bands)

    @model_validator(mode="after")
    def _dates(self) -> ExamCreate:
        _ordered(self.start_date, self.end_date, "start_date must not be after end_date.")
        return self


class ExamUpdate(PatchInput):
    """Placement (year, class, section) is immutable. ``draft``/``scheduled``: every
    field; ``marks_entry``: ``pass_percentage`` and ``grade_bands`` only; ``published``
    and ``cancelled`` exams are read-only."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset(
        {"description", "pass_percentage", "grade_bands"}
    )

    name: Text150 | None = None
    description: Text1000 | None = None
    exam_type: Type | None = None
    start_date: date | None = None
    end_date: date | None = None
    pass_percentage: Percentage | None = None
    grade_bands: list[GradeBand] | None = None

    _bands = field_validator("grade_bands")(_check_bands)


class ExamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID | None
    name: str
    description: str | None
    exam_type: str
    start_date: date
    end_date: date
    status: str
    pass_percentage: Number | None
    grade_bands: list[GradeBandRead] | None
    created_by_user_id: uuid.UUID
    cancelled_by_user_id: uuid.UUID | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    created_at: datetime
    updated_at: datetime


class ReasonInput(StrictInput):
    reason: Text500


class PublishInput(StrictInput):
    allow_incomplete: bool = Field(
        default=False,
        description="Publish even though some eligible students have no marks entry yet "
        "(their report cards show result 'incomplete').",
    )


# ================================================================ exam subjects
class ExamSubjectCreate(StrictInput):
    subject_id: uuid.UUID
    max_marks: MaxMarks
    pass_marks: MarksValue | None = None
    assessment_date: date | None = Field(
        default=None, description="Within the exam's dates. Defaults to the exam start date."
    )
    start_time: time | None = None
    end_time: time | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ExamSubjectCreate:
        if self.pass_marks is not None and self.pass_marks > self.max_marks:
            raise ValueError("pass_marks must not exceed max_marks.")
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time >= self.end_time
        ):
            raise ValueError("start_time must be before end_time.")
        return self


class ExamSubjectUpdate(PatchInput):
    nullable_fields: ClassVar[frozenset[str]] = frozenset(
        {"pass_marks", "assessment_date", "start_time", "end_time"}
    )

    max_marks: MaxMarks | None = None
    pass_marks: MarksValue | None = None
    assessment_date: date | None = None
    start_time: time | None = None
    end_time: time | None = None


class ExamSubjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    exam_id: uuid.UUID
    subject_id: uuid.UUID
    max_marks: Number
    pass_marks: Number | None
    assessment_date: date | None
    start_time: time | None
    end_time: time | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


# ========================================================================== marks
class MarkEntry(StrictInput):
    """``present`` needs ``marks_obtained``; ``absent`` and ``exempt`` must not have it."""

    student_id: uuid.UUID
    status: Status = MarkStatus.PRESENT
    marks_obtained: MarksValue | None = None
    remarks: Text500 | None = None

    @model_validator(mode="after")
    def _marks_match_status(self) -> MarkEntry:
        if self.status is MarkStatus.PRESENT and self.marks_obtained is None:
            raise ValueError("marks_obtained is required when status is present.")
        if self.status is not MarkStatus.PRESENT and self.marks_obtained is not None:
            raise ValueError("marks_obtained must be omitted when status is absent or exempt.")
        return self


class MarkBulkCreate(StrictInput):
    """All-or-nothing: if any entry is invalid, nothing is written."""

    marks: list[MarkEntry] = Field(min_length=1, max_length=MAX_BULK_MARKS)


class MarkCorrection(PatchInput):
    """Every change to an existing mark is a correction and needs ``reason``. Changing
    ``status`` to ``present`` needs ``marks_obtained``; to ``absent``/``exempt`` clears it."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"remarks"})

    status: Status | None = None
    marks_obtained: MarksValue | None = None
    remarks: Text500 | None = None
    reason: Text500

    @model_validator(mode="after")
    def _something_to_change(self) -> MarkCorrection:
        if not self.model_fields_set - {"reason"}:
            raise ValueError("Provide status, marks_obtained and/or remarks to change.")
        return self


class MarkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    exam_id: uuid.UUID
    exam_subject_id: uuid.UUID
    student_id: uuid.UUID
    enrollment_id: uuid.UUID
    academic_year_id: uuid.UUID
    class_id: uuid.UUID
    section_id: uuid.UUID
    status: str
    marks_obtained: Number | None
    max_marks: Number
    remarks: str | None
    entered_by_user_id: uuid.UUID
    entered_at: datetime
    corrected_by_user_id: uuid.UUID | None
    corrected_at: datetime | None
    correction_reason: str | None
    created_at: datetime
    updated_at: datetime


class MarkBulkResult(BaseModel):
    exam_subject_id: uuid.UUID
    created: int
    items: list[MarkRead]


class MarkChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mark_id: uuid.UUID
    sequence: int
    previous_status: str
    new_status: str
    previous_marks: Number | None
    new_marks: Number | None
    previous_remarks: str | None
    new_remarks: str | None
    reason: str
    changed_by_user_id: uuid.UUID
    changed_at: datetime


class EligibleStudent(BaseModel):
    """A student who may receive marks for the component, with their entry if any."""

    student_id: uuid.UUID
    enrollment_id: uuid.UUID
    section_id: uuid.UUID
    admission_number: str
    first_name: str
    last_name: str
    mark_id: uuid.UUID | None
    status: str | None
    marks_obtained: Number | None


# ======================================================================== results
class SubjectResult(BaseModel):
    """One component of a student's result.

    ``status``: ``present``/``absent``/``exempt`` (entered) or ``pending`` (eligible but
    nothing entered yet). ``result``: ``pass``/``fail`` (passing marks configured),
    ``absent``, ``exempt``, ``pending`` or ``not_applicable`` (no passing marks).
    """

    exam_subject_id: uuid.UUID
    subject_id: uuid.UUID
    subject_code: str
    subject_name: str
    max_marks: Number
    pass_marks: Number | None
    status: str
    marks_obtained: Number | None
    percentage: Number | None
    result: str
    grade: str | None


class StudentRef(BaseModel):
    id: uuid.UUID
    admission_number: str
    first_name: str
    last_name: str


class StudentResult(BaseModel):
    """Live calculation from current marks (staff preview; not a published result)."""

    student: StudentRef
    enrollment_id: uuid.UUID
    section_id: uuid.UUID
    subjects: list[SubjectResult]
    total_obtained: Number
    total_max: Number
    percentage: Number | None
    grade: str | None
    result: str


class PublicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    exam_id: uuid.UUID
    version: int
    status: str
    published_by_user_id: uuid.UUID
    published_at: datetime
    withdrawn_by_user_id: uuid.UUID | None
    withdrawn_at: datetime | None
    withdrawal_reason: str | None
    report_cards: int = 0
    incomplete: int = 0


class ReportCardExam(BaseModel):
    id: uuid.UUID
    name: str
    exam_type: str
    start_date: date
    end_date: date
    section_id: uuid.UUID | None
    pass_percentage: Number | None
    grade_bands: list[GradeBandRead] | None


class ReportCardRead(BaseModel):
    """A student's result as frozen at publication."""

    id: uuid.UUID
    school_id: uuid.UUID
    school_name: str
    publication_id: uuid.UUID
    publication_version: int
    publication_status: str
    published_at: datetime
    exam: ReportCardExam
    student: StudentRef
    enrollment_id: uuid.UUID
    academic_year_id: uuid.UUID
    academic_year_name: str
    class_id: uuid.UUID
    class_name: str
    section_id: uuid.UUID
    section_name: str | None
    subjects: list[SubjectResult]
    total_obtained: Number
    total_max: Number
    percentage: Number | None
    grade: str | None
    result: str
