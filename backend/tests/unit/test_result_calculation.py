"""The result arithmetic (pure, no database)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.services.result_calculation import (
    Band,
    Component,
    Entry,
    bands_from,
    calculate,
    grade_for,
    percent,
)

D = Decimal
MATH = Component(uuid.uuid4(), uuid.uuid4(), "MATH", "Mathematics", D("100.00"), D("35.00"))
SCI = Component(uuid.uuid4(), uuid.uuid4(), "SCI", "Science", D("50.00"), D("20.00"))
ART = Component(uuid.uuid4(), uuid.uuid4(), "ART", "Art", D("25.00"), None)
ALL = (MATH, SCI, ART)
BANDS = bands_from(
    [
        {"grade": "C", "min_percentage": "0"},
        {"grade": "A", "min_percentage": "75.00"},
        {"grade": "B", "min_percentage": "50"},
    ]
)


def present(value: str) -> Entry:
    return Entry("present", D(value))


ABSENT = Entry("absent", None)
EXEMPT = Entry("exempt", None)


def run(
    entries: dict[Component, Entry],
    applicable: tuple[Component, ...] = ALL,
    *,
    components: tuple[Component, ...] = ALL,
    pass_percentage: str | None = None,
    bands: list[Band] | None = None,
):  # type: ignore[no-untyped-def]
    return calculate(
        components,
        {c.id: e for c, e in entries.items()},
        applicable=[c.id for c in applicable],
        pass_percentage=D(pass_percentage) if pass_percentage else None,
        bands=bands or [],
    )


def test_totals_use_each_components_own_maximum() -> None:
    out = run({MATH: present("80"), SCI: present("40"), ART: present("20")})
    assert (out.total_obtained, out.total_max) == (D("140.00"), D("175.00"))
    assert out.percentage == D("80.00")
    assert out.result == "pass"
    assert [line.component.subject_code for line in out.lines] == ["ART", "MATH", "SCI"]
    assert [line.result for line in out.lines] == ["not_applicable", "pass", "pass"]
    assert [line.percentage for line in out.lines] == [D("80.00"), D("80.00"), D("80.00")]


def test_percentage_rounds_half_up_with_exact_decimals() -> None:
    assert percent(D("1"), D("3")) == D("33.33")
    assert percent(D("2"), D("3")) == D("66.67")
    assert percent(D("0.125"), D("1")) == D("12.50")
    assert percent(D("33.335"), D("100")) == D("33.34")  # half-up, not banker's rounding
    # 0.1 + 0.2 is exactly 0.3 in Decimal (it is not in floating point).
    out = run(
        {MATH: present("0.10"), SCI: present("0.20")},
        (MATH, SCI),
        components=(MATH, SCI),
    )
    assert out.total_obtained == D("0.30")


def test_missing_marks_are_pending_not_zero() -> None:
    out = run({MATH: present("90")}, (MATH, SCI), components=(MATH, SCI))
    assert out.result == "incomplete"
    assert out.percentage is None
    assert out.pending == 1
    line = next(line for line in out.lines if line.component is SCI)
    assert (line.status, line.marks, line.result) == ("pending", None, "pending")
    # Totals cover what has been entered only; the percentage is withheld.
    assert (out.total_obtained, out.total_max) == (D("90"), D("100.00"))


def test_absent_counts_as_zero_and_fails_a_component_with_passing_marks() -> None:
    out = run({MATH: present("90"), SCI: ABSENT}, (MATH, SCI), components=(MATH, SCI))
    assert (out.total_obtained, out.total_max) == (D("90"), D("150.00"))
    assert out.percentage == D("60.00")
    assert out.result == "fail"
    sci = next(line for line in out.lines if line.component is SCI)
    assert (sci.status, sci.marks, sci.result, sci.grade) == ("absent", None, "absent", None)


def test_absent_without_passing_marks_only_lowers_the_percentage() -> None:
    out = run({MATH: present("90"), ART: ABSENT}, (MATH, ART), components=(MATH, ART))
    assert out.percentage == D("72.00")
    assert out.result == "pass"


def test_exempt_is_left_out_of_both_totals() -> None:
    out = run({MATH: EXEMPT, SCI: present("45"), ART: present("20")})
    assert (out.total_obtained, out.total_max) == (D("65"), D("75.00"))
    assert out.percentage == D("86.67")
    assert out.result == "pass"


def test_everything_exempt_is_not_applicable() -> None:
    out = run({MATH: EXEMPT, SCI: EXEMPT, ART: EXEMPT})
    assert (out.total_obtained, out.total_max, out.percentage) == (D(0), D(0), None)
    assert out.result == "not_applicable"


def test_component_pass_marks_boundary() -> None:
    at = run({MATH: present("35.00")}, (MATH,), components=(MATH,))
    below = run({MATH: present("34.99")}, (MATH,), components=(MATH,))
    assert (at.result, below.result) == ("pass", "fail")
    assert below.lines[0].result == "fail"


def test_failing_one_component_fails_the_exam_despite_a_high_total() -> None:
    out = run({MATH: present("100"), SCI: present("19.5"), ART: present("25")})
    assert out.percentage == D("82.57")  # 144.50 / 175
    assert out.result == "fail"


def test_overall_pass_percentage_is_per_exam() -> None:
    entries = {MATH: present("40"), SCI: present("20"), ART: present("10")}
    assert run(entries).percentage == D("40.00")
    assert run(entries, pass_percentage="40").result == "pass"
    assert run(entries, pass_percentage="40.01").result == "fail"


def test_no_pass_criteria_means_no_pass_or_fail() -> None:
    out = run({ART: present("1")}, (ART,), components=(ART,))
    assert out.result == "not_applicable"
    with_threshold = run({ART: present("1")}, (ART,), components=(ART,), pass_percentage="33")
    assert with_threshold.result == "fail"


def test_grades_only_with_bands() -> None:
    entries = {MATH: present("75"), SCI: present("24.5"), ART: present("0")}
    assert run(entries).grade is None
    assert all(line.grade is None for line in run(entries).lines)
    graded = run(entries, bands=BANDS)
    assert graded.percentage == D("56.86")  # 99.50 / 175
    assert graded.grade == "B"
    assert {line.component.subject_code: line.grade for line in graded.lines} == {
        "ART": "C",
        "MATH": "A",
        "SCI": "C",
    }
    assert (
        run({MATH: present("90")}, (MATH, SCI), components=(MATH, SCI), bands=BANDS).grade is None
    )


@pytest.mark.parametrize(
    ("value", "grade"),
    [("100", "A"), ("75.00", "A"), ("74.99", "B"), ("50", "B"), ("49.99", "C"), ("0", "C")],
)
def test_grade_boundaries(value: str, grade: str) -> None:
    assert grade_for(D(value), BANDS) == grade


def test_grade_without_matching_band() -> None:
    assert grade_for(D("10"), [Band("A", D("50"))]) is None
    assert grade_for(None, BANDS) is None


def test_only_applicable_or_entered_components_appear() -> None:
    out = run({SCI: present("10")}, (MATH,))
    codes = {line.component.subject_code: line.status for line in out.lines}
    assert codes == {"MATH": "pending", "SCI": "present"}


def test_snapshot_form_uses_strings_for_decimals() -> None:
    line = run({MATH: present("45.50")}, (MATH,), components=(MATH,)).lines[0].as_json()
    assert line["max_marks"] == "100.00"
    assert line["marks_obtained"] == "45.50"
    assert line["pass_marks"] == "35.00"
    assert line["percentage"] == "45.50"
    assert line["subject_code"] == "MATH"
    assert line["exam_subject_id"] == str(MATH.id)
