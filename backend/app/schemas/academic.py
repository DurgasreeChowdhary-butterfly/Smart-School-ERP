from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.fields import Code, PatchInput, StrictInput, Text50, Text100, Text150, Text1000

DisplayOrder = Annotated[int, Field(ge=0, le=10_000)]
Capacity = Annotated[int, Field(gt=0, le=10_000)]


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    school_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ academic years
class AcademicYearRead(_Read):
    name: str
    start_date: date
    end_date: date
    is_current: bool


class AcademicYearCreate(StrictInput):
    name: Text50 = Field(examples=["2026-2027"])
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _ordered(self) -> AcademicYearCreate:
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date.")
        return self


class AcademicYearUpdate(PatchInput):
    """Date order is re-validated against stored values by the service."""

    name: Text50 | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None


# ------------------------------------------------------------------------- classes
class ClassRead(_Read):
    name: str
    code: str
    display_order: int


class ClassCreate(StrictInput):
    name: Text100 = Field(examples=["Class 10"])
    code: Code = Field(examples=["C10"])
    display_order: DisplayOrder = 0


class ClassUpdate(PatchInput):
    name: Text100 | None = None
    code: Code | None = None
    display_order: DisplayOrder | None = None
    is_active: bool | None = None


# ------------------------------------------------------------------------ sections
class SectionRead(_Read):
    class_id: uuid.UUID
    name: str
    code: str
    capacity: int | None


class SectionCreate(StrictInput):
    class_id: uuid.UUID
    name: Text50 = Field(examples=["A"])
    code: Code = Field(examples=["A"])
    capacity: Capacity | None = None


class SectionUpdate(PatchInput):
    """``class_id`` is immutable: sections are not moved between classes."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset({"capacity"})

    name: Text50 | None = None
    code: Code | None = None
    capacity: Capacity | None = None
    is_active: bool | None = None


# ------------------------------------------------------------------------ subjects
class SubjectRead(_Read):
    name: str
    code: str
    description: str | None


class SubjectCreate(StrictInput):
    name: Text150 = Field(examples=["Mathematics"])
    code: Code = Field(examples=["MATH"])
    description: Text1000 | None = None


class SubjectUpdate(PatchInput):
    nullable_fields: ClassVar[frozenset[str]] = frozenset({"description"})

    name: Text150 | None = None
    code: Code | None = None
    description: Text1000 | None = None
    is_active: bool | None = None
