"""Assignment request validation (no database)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.assignments import (
    AssignmentCreate,
    AssignmentUpdate,
    ReviewInput,
    SubmissionCreate,
)


def _create(**extra: Any) -> AssignmentCreate:
    body: dict[str, Any] = {
        "academic_year_id": uuid.uuid4(),
        "class_id": uuid.uuid4(),
        "section_id": uuid.uuid4(),
        "subject_id": uuid.uuid4(),
        "title": " Essay ",
        "instructions": " Write 300 words. ",
        "assigned_on": "2026-10-01",
        "due_at": "2026-10-05T18:00:00+05:30",
        **extra,
    }
    return AssignmentCreate.model_validate(body)


def test_deadlines_need_a_time_zone() -> None:
    item = _create()
    assert item.due_at.utcoffset() is not None
    assert (item.title, item.instructions) == ("Essay", "Write 300 words.")
    with pytest.raises(ValidationError):
        _create(due_at="2026-10-05T18:00:00")
    with pytest.raises(ValidationError):
        AssignmentUpdate.model_validate({"due_at": "2026-10-05 18:00"})


def test_marks_and_text_limits() -> None:
    assert _create(max_marks=25.5).max_marks == Decimal("25.50")
    for override in (
        {"max_marks": 0},
        {"max_marks": 25.555},
        {"title": "x" * 201},
        {"instructions": "a\x00b"},
    ):
        with pytest.raises(ValidationError):
            _create(**override)
    assert AssignmentUpdate.model_validate({"max_marks": None}).changes() == {"max_marks": None}
    with pytest.raises(ValidationError):
        AssignmentUpdate.model_validate({"title": None})


def test_submission_content() -> None:
    assert SubmissionCreate(content="  answer  ").content == "answer"
    for content in ("", "   ", "x" * 20_001, "nul\x00"):
        with pytest.raises(ValidationError):
            SubmissionCreate(content=content)


def test_review_input() -> None:
    assert ReviewInput.model_validate({"feedback": None}).model_fields_set == {"feedback"}
    assert ReviewInput(marks_awarded=7.25).marks_awarded == Decimal("7.25")  # type: ignore[arg-type]
    for body in ({}, {"reason": "only a reason"}, {"marks_awarded": -1}, {"marks_awarded": 1.005}):
        with pytest.raises(ValidationError):
            ReviewInput.model_validate(body)
