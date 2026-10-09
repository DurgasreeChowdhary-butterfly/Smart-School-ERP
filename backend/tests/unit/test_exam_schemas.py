"""Exam request validation (no database)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.exams import (
    ExamCreate,
    ExamSubjectCreate,
    ExamUpdate,
    MarkCorrection,
    MarkEntry,
    MarkRead,
)

STUDENT = uuid.uuid4()


def _exam(**extra: Any) -> ExamCreate:
    body: dict[str, Any] = {
        "academic_year_id": uuid.uuid4(),
        "class_id": uuid.uuid4(),
        "name": " Midterm ",
        "exam_type": "MIDTERM",
        "start_date": "2026-09-01",
        "end_date": "2026-09-10",
        **extra,
    }
    return ExamCreate.model_validate(body)


def test_marks_are_exact_two_decimal_values() -> None:
    assert MarkEntry(student_id=STUDENT, marks_obtained=33.33).marks_obtained == Decimal("33.33")  # type: ignore[arg-type]
    assert str(MarkEntry(student_id=STUDENT, marks_obtained="45.5").marks_obtained) == "45.50"  # type: ignore[arg-type]
    for bad in (45.555, -0.01, 1000.01, "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            MarkEntry(student_id=STUDENT, marks_obtained=bad)  # type: ignore[arg-type]


def test_status_and_marks_must_agree() -> None:
    assert MarkEntry(student_id=STUDENT, status=" Absent ").status == "absent"  # type: ignore[arg-type]
    for body in (
        {"status": "present"},
        {"status": "absent", "marks_obtained": 1},
        {"status": "exempt", "marks_obtained": 0},
    ):
        with pytest.raises(ValidationError):
            MarkEntry.model_validate({"student_id": STUDENT, **body})


def test_correction_needs_a_change_and_a_reason() -> None:
    assert MarkCorrection(marks_obtained=1, reason="Recount").reason == "Recount"  # type: ignore[arg-type]
    for body in (
        {"reason": "only"},
        {"marks_obtained": 1},
        {"marks_obtained": None, "reason": "x"},
    ):
        with pytest.raises(ValidationError):
            MarkCorrection.model_validate(body)


def test_exam_normalisation_and_grade_bands() -> None:
    exam = _exam(
        grade_bands=[
            {"grade": " B ", "min_percentage": 0},
            {"grade": "A", "min_percentage": "75.5"},
        ]
    )
    assert (exam.name, exam.exam_type) == ("Midterm", "midterm")
    assert exam.grade_bands is not None
    assert [(b.grade, b.min_percentage) for b in exam.grade_bands] == [
        ("A", Decimal("75.50")),
        ("B", Decimal("0.00")),
    ]
    with pytest.raises(ValidationError):
        _exam(start_date="2026-09-11")
    with pytest.raises(ValidationError):
        _exam(grade_bands=[{"grade": "A", "min_percentage": 0}] * 21)
    with pytest.raises(ValidationError):
        ExamUpdate.model_validate({"name": None})
    assert ExamUpdate.model_validate({"grade_bands": None}).changes() == {"grade_bands": None}


def test_component_rules() -> None:
    item = ExamSubjectCreate(subject_id=uuid.uuid4(), max_marks=50, pass_marks=17.5)  # type: ignore[arg-type]
    assert (item.max_marks, item.pass_marks) == (Decimal("50.00"), Decimal("17.50"))
    for body in (
        {"max_marks": 0},
        {"max_marks": 10, "pass_marks": 11},
        {"max_marks": 10, "start_time": "10:00", "end_time": "10:00"},
    ):
        with pytest.raises(ValidationError):
            ExamSubjectCreate.model_validate({"subject_id": uuid.uuid4(), **body})


def test_responses_carry_numbers() -> None:
    now = "2026-10-09T00:00:00Z"
    ids = {
        k: uuid.uuid4()
        for k in (
            "id",
            "school_id",
            "exam_id",
            "exam_subject_id",
            "student_id",
            "enrollment_id",
            "academic_year_id",
            "class_id",
            "section_id",
            "entered_by_user_id",
        )
    }
    read = MarkRead.model_validate(
        {
            **ids,
            "status": "present",
            "marks_obtained": Decimal("45.50"),
            "max_marks": Decimal("100.00"),
            "remarks": None,
            "entered_at": now,
            "corrected_by_user_id": None,
            "corrected_at": None,
            "correction_reason": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    assert '"marks_obtained":45.5' in read.model_dump_json()
    assert '"max_marks":100.0' in read.model_dump_json()
