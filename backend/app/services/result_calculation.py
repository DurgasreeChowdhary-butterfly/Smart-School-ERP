"""The single implementation of exam result arithmetic (pure: no database access).

Formulas (``Decimal`` throughout; percentages rounded half-up to 2 decimals)
-------------------------------------------------------------------------------
Per component the student is eligible for (or has an entry in):

* ``present``  -> ``marks_obtained`` out of ``max_marks``;
* ``absent``   -> 0 out of ``max_marks`` (an explicit entry, not a missing one);
* ``exempt``   -> left out of both totals;
* no entry     -> ``pending``: never treated as zero or as a failure.

``total_obtained`` = sum of marks over present/absent components;
``total_max``      = sum of ``max_marks`` over present/absent components;
``percentage``     = ``total_obtained / total_max * 100``; ``null`` when any component is
pending or ``total_max`` is 0.

Component result: ``pass`` if ``marks_obtained >= pass_marks``, else ``fail`` (only when
passing marks are configured, otherwise ``not_applicable``); ``absent``, ``exempt`` and
``pending`` as entered.

Overall result, in order:

1. any component pending                    -> ``incomplete``;
2. nothing counted, or no pass criteria      -> ``not_applicable`` (no component has
   passing marks and the exam has no ``pass_percentage``);
3. a component with passing marks is failed or absent, or ``percentage <
   pass_percentage``                          -> ``fail``;
4. otherwise                                  -> ``pass``.

Grades are reported only when the exam has grade bands: the band with the highest
``min_percentage`` not above the percentage (overall, and per present component).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models.exams import ExamResult, MarkStatus

CENT = Decimal("0.01")
HUNDRED = Decimal(100)
PENDING = "pending"
PASS = ExamResult.PASS.value
FAIL = ExamResult.FAIL.value
NOT_APPLICABLE = ExamResult.NOT_APPLICABLE.value


@dataclass(frozen=True, slots=True)
class Component:
    id: uuid.UUID
    subject_id: uuid.UUID
    subject_code: str
    subject_name: str
    max_marks: Decimal
    pass_marks: Decimal | None


@dataclass(frozen=True, slots=True)
class Entry:
    status: str
    marks: Decimal | None


@dataclass(frozen=True, slots=True)
class Band:
    grade: str
    min_percentage: Decimal


@dataclass(frozen=True, slots=True)
class Line:
    component: Component
    status: str
    marks: Decimal | None
    percentage: Decimal | None
    result: str
    grade: str | None

    def as_json(self) -> dict[str, Any]:
        """Snapshot form (decimals as strings, so JSONB never holds floats)."""
        c = self.component
        return {
            "exam_subject_id": str(c.id),
            "subject_id": str(c.subject_id),
            "subject_code": c.subject_code,
            "subject_name": c.subject_name,
            "max_marks": str(c.max_marks),
            "pass_marks": _text(c.pass_marks),
            "status": self.status,
            "marks_obtained": _text(self.marks),
            "percentage": _text(self.percentage),
            "result": self.result,
            "grade": self.grade,
        }


@dataclass(frozen=True, slots=True)
class Outcome:
    lines: list[Line]
    total_obtained: Decimal
    total_max: Decimal
    percentage: Decimal | None
    grade: str | None
    result: str

    @property
    def pending(self) -> int:
        return sum(1 for line in self.lines if line.status == PENDING)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def percent(obtained: Decimal, maximum: Decimal) -> Decimal:
    return (obtained * HUNDRED / maximum).quantize(CENT, rounding=ROUND_HALF_UP)


def bands_from(raw: Iterable[Mapping[str, Any]] | None) -> list[Band]:
    """Grade bands from their stored form, highest threshold first."""
    bands = [Band(str(b["grade"]), Decimal(str(b["min_percentage"]))) for b in raw or ()]
    return sorted(bands, key=lambda band: band.min_percentage, reverse=True)


def grade_for(percentage: Decimal | None, bands: Sequence[Band]) -> str | None:
    if percentage is None:
        return None
    for band in bands:  # highest threshold first
        if percentage >= band.min_percentage:
            return band.grade
    return None


def _line(component: Component, entry: Entry | None, bands: Sequence[Band]) -> Line:
    if entry is None:
        return Line(component, PENDING, None, None, PENDING, None)
    if entry.status == MarkStatus.PRESENT:
        assert entry.marks is not None  # noqa: S101 - guaranteed by a CHECK constraint
        pct = percent(entry.marks, component.max_marks)
        if component.pass_marks is None:
            result = NOT_APPLICABLE
        else:
            result = PASS if entry.marks >= component.pass_marks else FAIL
        return Line(component, entry.status, entry.marks, pct, result, grade_for(pct, bands))
    # absent / exempt: an explicit entry without marks
    return Line(component, entry.status, None, None, entry.status, None)


def calculate(
    components: Sequence[Component],
    entries: Mapping[uuid.UUID, Entry],
    *,
    applicable: Iterable[uuid.UUID],
    pass_percentage: Decimal | None,
    bands: Sequence[Band],
) -> Outcome:
    """One student's result.

    ``entries`` maps component id -> the student's entry; ``applicable`` are the
    components the student is eligible for. A component appears on the result when it
    is applicable or has an entry; it is pending when it has no entry.
    """
    wanted = set(applicable) | set(entries)
    lines = [
        _line(c, entries.get(c.id), bands)
        for c in sorted(components, key=lambda c: (c.subject_code, str(c.id)))
        if c.id in wanted
    ]
    counted = [line for line in lines if line.status in (MarkStatus.PRESENT, MarkStatus.ABSENT)]
    total_max = sum((line.component.max_marks for line in counted), Decimal(0))
    total_obtained = sum((line.marks or Decimal(0) for line in counted), Decimal(0))
    pending = any(line.status == PENDING for line in lines)
    percentage = None if pending or not total_max else percent(total_obtained, total_max)
    criteria = pass_percentage is not None or any(
        line.component.pass_marks is not None for line in counted
    )
    if pending:
        result = ExamResult.INCOMPLETE.value
    elif not counted or not criteria:
        result = NOT_APPLICABLE
    elif any(
        line.result == FAIL
        or (line.status == MarkStatus.ABSENT and line.component.pass_marks is not None)
        for line in counted
    ) or (pass_percentage is not None and (percentage is None or percentage < pass_percentage)):
        result = FAIL
    else:
        result = PASS
    return Outcome(
        lines=lines,
        total_obtained=total_obtained,
        total_max=total_max,
        percentage=percentage,
        grade=grade_for(percentage, bands) if bands else None,
        result=result,
    )
