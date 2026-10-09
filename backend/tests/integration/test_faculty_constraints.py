"""Database-level faculty/assignment integrity, exercised with raw SQL that bypasses
every service check. PostgreSQL itself must refuse invalid data."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import SCHOOL_A, SCHOOL_B

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]


def _violates(
    session: Session,
    sql: str,
    params: dict[str, object],
    expected: type,
    constraint: str | None = None,
) -> None:
    with pytest.raises(IntegrityError) as exc_info, session.begin_nested():
        session.execute(text(sql), params)
    assert isinstance(exc_info.value.orig, expected), exc_info.value.orig
    if constraint is not None:
        assert exc_info.value.orig.diag.constraint_name == constraint


def _insert(session: Session, sql: str, **params: object) -> uuid.UUID:
    new_id = uuid.uuid4()
    session.execute(text(sql), {"id": new_id, **params})
    return new_id


def _user(session: Session, school: uuid.UUID) -> uuid.UUID:
    return _insert(
        session,
        "INSERT INTO users (id, full_name, email, school_id) VALUES (:id, 'T', :e, :s)",
        e=f"{uuid.uuid4().hex[:12]}@x.test",
        s=school,
    )


FACULTY = (
    "INSERT INTO faculty (id, school_id, user_id, employee_id, first_name, last_name{extra_cols}) "
    "VALUES (:id, :s, :u, :emp, 'F', 'L'{extra_vals})"
)


def _faculty(session: Session, school: uuid.UUID, emp: str) -> uuid.UUID:
    return _insert(
        session,
        FACULTY.format(extra_cols="", extra_vals=""),
        s=school,
        u=_user(session, school),
        emp=emp,
    )


def _structure(session: Session, school: uuid.UUID, tag: str) -> dict[str, uuid.UUID]:
    ids = {
        "year": _insert(
            session,
            "INSERT INTO academic_years (id, school_id, name, start_date, end_date) "
            "VALUES (:id, :s, :n, '2026-06-01', '2027-05-31')",
            s=school,
            n=f"Y-{tag}",
        ),
        "class": _insert(
            session,
            "INSERT INTO classes (id, school_id, name, code) VALUES (:id, :s, 'C', :c)",
            s=school,
            c=f"C-{tag}",
        ),
        "class2": _insert(
            session,
            "INSERT INTO classes (id, school_id, name, code) VALUES (:id, :s, 'C2', :c)",
            s=school,
            c=f"C2-{tag}",
        ),
        "subject": _insert(
            session,
            "INSERT INTO subjects (id, school_id, name, code) VALUES (:id, :s, 'M', :c)",
            s=school,
            c=f"M-{tag}",
        ),
        "faculty": _faculty(session, school, f"EMP-{tag}"),
    }
    for key, cls in (("section", "class"), ("section2", "class2")):
        ids[key] = _insert(
            session,
            "INSERT INTO sections (id, school_id, class_id, name, code) "
            "VALUES (:id, :s, :c, 'A', 'A')",
            s=school,
            c=ids[cls],
        )
    return ids


ASSIGN = (
    "INSERT INTO faculty_assignments (id, school_id, faculty_id, academic_year_id, class_id, "
    "section_id, subject_id, status, effective_from, effective_to) "
    "VALUES (gen_random_uuid(), :school, :faculty, :year, :class, :section, :subject, :st, "
    ":ef, :et)"
)


@pytest.fixture
def a(db_session: Session) -> dict[str, uuid.UUID]:
    return _structure(db_session, SCHOOL_A, "A")


@pytest.fixture
def b(db_session: Session) -> dict[str, uuid.UUID]:
    return _structure(db_session, SCHOOL_B, "B")


def _params(base: dict[str, uuid.UUID], **override: object) -> dict[str, object]:
    params: dict[str, object] = {
        "school": SCHOOL_A,
        "faculty": base["faculty"],
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "subject": base["subject"],
        "st": "active",
        "ef": None,
        "et": None,
    }
    params.update(override)
    return params


# ======================================================================= assignments
@pytest.mark.parametrize(
    ("foreign", "constraint"),
    [
        ("faculty", "fk_faculty_assignments_faculty_same_school"),
        ("year", "fk_faculty_assignments_year_same_school"),
        ("class", "fk_faculty_assignments_class_same_school"),
        ("section", "fk_faculty_assignments_section_same_school"),
        ("subject", "fk_faculty_assignments_subject_same_school"),
    ],
)
def test_assignment_cannot_mix_schools(
    db_session: Session,
    a: dict[str, uuid.UUID],
    b: dict[str, uuid.UUID],
    foreign: str,
    constraint: str,
) -> None:
    """School A assignment row + one School B reference -> rejected by a composite FK."""
    overrides: dict[str, object] = {foreign: b[foreign]}
    if foreign == "class":
        overrides["section"] = b["section"]  # keep section-in-class consistent; school differs
    with pytest.raises(IntegrityError) as exc_info, db_session.begin_nested():
        db_session.execute(text(ASSIGN), _params(a, **overrides))
    assert isinstance(exc_info.value.orig, pg.ForeignKeyViolation)
    violated = exc_info.value.orig.diag.constraint_name
    if foreign == "class":  # the swapped-in section is also from School B
        assert violated in {constraint, "fk_faculty_assignments_section_same_school"}
    else:
        assert violated == constraint


def test_assignment_entirely_in_school_b_with_school_a_id_rejected(
    db_session: Session, b: dict[str, uuid.UUID]
) -> None:
    """Every reference from School B but the row claims School A."""
    _violates(db_session, ASSIGN, _params(b, school=SCHOOL_A), pg.ForeignKeyViolation)


def test_section_must_belong_to_class(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    _violates(
        db_session,
        ASSIGN,
        _params(a, section=a["section2"]),
        pg.ForeignKeyViolation,
        "fk_faculty_assignments_section_in_class",
    )


def test_valid_assignment_accepted(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(ASSIGN), _params(a, ef="2026-06-01", et="2027-05-31"))
    row = db_session.execute(text("SELECT status, is_active FROM faculty_assignments")).one()
    assert tuple(row) == ("active", True)


def test_one_active_duplicate_but_history_may_repeat(
    db_session: Session, a: dict[str, uuid.UUID]
) -> None:
    db_session.execute(text(ASSIGN), _params(a))
    _violates(
        db_session,
        ASSIGN,
        _params(a),
        pg.UniqueViolation,
        "uq_faculty_assignments_one_active",
    )
    db_session.execute(text(ASSIGN), _params(a, st="inactive"))
    db_session.execute(text(ASSIGN), _params(a, st="inactive"))
    # Reactivating history while an active duplicate exists is refused too.
    _violates(
        db_session,
        "UPDATE faculty_assignments SET status = 'active' WHERE status = 'inactive'",
        {},
        pg.UniqueViolation,
        "uq_faculty_assignments_one_active",
    )
    # A different subject / section is a different assignment.
    db_session.execute(text(ASSIGN), _params(a, section=a["section2"], **{"class": a["class2"]}))


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        (
            {"ef": "2027-01-01", "et": "2026-12-31"},
            "ck_faculty_assignments_effective_dates_ordered",
        ),
        (
            {"ef": "2027-01-01", "et": "2027-01-01"},
            "ck_faculty_assignments_effective_dates_ordered",
        ),
        ({"st": "ended"}, "ck_faculty_assignments_status_valid"),
    ],
)
def test_assignment_checks(
    db_session: Session, a: dict[str, uuid.UUID], override: dict[str, object], constraint: str
) -> None:
    _violates(db_session, ASSIGN, _params(a, **override), pg.CheckViolation, constraint)


def test_is_active_is_derived_from_status(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(ASSIGN), _params(a, st="inactive"))
    assert db_session.scalar(text("SELECT is_active FROM faculty_assignments")) is False
    with pytest.raises(DBAPIError) as exc_info, db_session.begin_nested():
        db_session.execute(text("UPDATE faculty_assignments SET is_active = true"))
    assert isinstance(exc_info.value.orig, pg.GeneratedAlways)


def test_nothing_is_cascade_deleted(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(ASSIGN), _params(a))
    for table, key in (
        ("faculty", "faculty"),
        ("sections", "section"),
        ("subjects", "subject"),
        ("academic_years", "year"),
    ):
        _violates(
            db_session,
            f"DELETE FROM {table} WHERE id = :id",  # noqa: S608
            {"id": a[key]},
            pg.ForeignKeyViolation,
        )


# =========================================================================== faculty
def test_faculty_user_must_belong_to_same_school(db_session: Session) -> None:
    _violates(
        db_session,
        FACULTY.format(extra_cols="", extra_vals=""),
        {"id": uuid.uuid4(), "s": SCHOOL_A, "u": _user(db_session, SCHOOL_B), "emp": "X1"},
        pg.ForeignKeyViolation,
        "fk_faculty_user_same_school",
    )


def test_faculty_needs_a_user(db_session: Session) -> None:
    with pytest.raises(IntegrityError) as exc_info, db_session.begin_nested():
        db_session.execute(
            text(FACULTY.format(extra_cols="", extra_vals="")),
            {"id": uuid.uuid4(), "s": SCHOOL_A, "u": None, "emp": "X1"},
        )
    assert isinstance(exc_info.value.orig, pg.NotNullViolation)


def test_employee_id_unique_per_school_and_user_linked_once(db_session: Session) -> None:
    user = _user(db_session, SCHOOL_A)
    sql = FACULTY.format(extra_cols="", extra_vals="")
    db_session.execute(text(sql), {"id": uuid.uuid4(), "s": SCHOOL_A, "u": user, "emp": "E1"})
    _violates(
        db_session,
        sql,
        {"id": uuid.uuid4(), "s": SCHOOL_A, "u": _user(db_session, SCHOOL_A), "emp": "E1"},
        pg.UniqueViolation,
        "uq_faculty_school_id_employee_id",
    )
    _violates(
        db_session,
        sql,
        {"id": uuid.uuid4(), "s": SCHOOL_A, "u": user, "emp": "E2"},
        pg.UniqueViolation,
        "uq_faculty_user_id",
    )
    # Same employee id in another school is fine.
    db_session.execute(
        text(sql),
        {"id": uuid.uuid4(), "s": SCHOOL_B, "u": _user(db_session, SCHOOL_B), "emp": "E1"},
    )


@pytest.mark.parametrize(
    ("cols", "vals", "constraint"),
    [
        ("", "", "ck_faculty_employee_id_upper"),
        (", status", ", 'retired'", "ck_faculty_status_valid"),
    ],
)
def test_faculty_checks(db_session: Session, cols: str, vals: str, constraint: str) -> None:
    _violates(
        db_session,
        FACULTY.format(extra_cols=cols, extra_vals=vals),
        {
            "id": uuid.uuid4(),
            "s": SCHOOL_A,
            "u": _user(db_session, SCHOOL_A),
            "emp": "lower" if constraint.endswith("upper") else "OK-1",
        },
        pg.CheckViolation,
        constraint,
    )
