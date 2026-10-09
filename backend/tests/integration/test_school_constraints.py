"""Database-level guarantees, exercised with raw SQL so the services are bypassed."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import SCHOOL_A, SCHOOL_B

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]


def _violates(session: Session, sql: str, params: dict[str, object], expected: type) -> None:
    with pytest.raises(IntegrityError) as exc_info, session.begin_nested():
        session.execute(text(sql), params)
    assert isinstance(exc_info.value.orig, expected), exc_info.value.orig


def _insert_class(session: Session, school_id: uuid.UUID, code: str = "C1") -> uuid.UUID:
    class_id = uuid.uuid4()
    session.execute(
        text("INSERT INTO classes (id, school_id, name, code) VALUES (:id, :s, 'Class', :c)"),
        {"id": class_id, "s": school_id, "c": code},
    )
    return class_id


# ---------------------------------------------------------------- users.school_id FK
def test_user_with_unknown_school_rejected(db_session: Session) -> None:
    _violates(
        db_session,
        "INSERT INTO users (id, full_name, email, school_id) "
        "VALUES (gen_random_uuid(), 'X', 'x@y.test', :s)",
        {"s": uuid.uuid4()},
        pg.ForeignKeyViolation,
    )


def test_platform_user_may_have_no_school(db_session: Session) -> None:
    db_session.execute(
        text(
            "INSERT INTO users (id, full_name, email) "
            "VALUES (gen_random_uuid(), 'Root', 'root@x.test')"
        )
    )


def test_school_with_users_cannot_be_deleted(db_session: Session) -> None:
    db_session.execute(
        text(
            "INSERT INTO users (id, full_name, email, school_id) "
            "VALUES (gen_random_uuid(), 'T', 't@x.test', :s)"
        ),
        {"s": SCHOOL_A},
    )
    _violates(
        db_session, "DELETE FROM schools WHERE id = :s", {"s": SCHOOL_A}, pg.ForeignKeyViolation
    )


# -------------------------------------------------------------------------- schools
def test_school_code_unique_and_uppercase(db_session: Session) -> None:
    sql = "INSERT INTO schools (id, name, code) VALUES (gen_random_uuid(), 'X', :c)"
    _violates(db_session, sql, {"c": "SCH-A"}, pg.UniqueViolation)
    _violates(db_session, sql, {"c": "lower"}, pg.CheckViolation)


# ------------------------------------------------------------------- academic years
YEAR_SQL = (
    "INSERT INTO academic_years (id, school_id, name, start_date, end_date, is_current) "
    "VALUES (gen_random_uuid(), :s, :n, :a, :b, :cur)"
)


def _year(
    session: Session, school: uuid.UUID, name: str, a: str, b: str, cur: bool = False
) -> None:
    session.execute(text(YEAR_SQL), {"s": school, "n": name, "a": a, "b": b, "cur": cur})


def test_exclusion_constraint_blocks_overlap(db_session: Session) -> None:
    _year(db_session, SCHOOL_A, "Y1", "2026-06-01", "2027-05-31")
    params = {"s": SCHOOL_A, "n": "Y2", "a": "2027-05-31", "b": "2028-05-30", "cur": False}
    _violates(db_session, YEAR_SQL, params, pg.ExclusionViolation)
    # The same dates in another school are fine.
    _year(db_session, SCHOOL_B, "Y1", "2026-06-01", "2027-05-31")


def test_only_one_current_year_per_school(db_session: Session) -> None:
    _year(db_session, SCHOOL_A, "Y1", "2025-06-01", "2026-05-31", cur=True)
    params = {"s": SCHOOL_A, "n": "Y2", "a": "2026-06-01", "b": "2027-05-31", "cur": True}
    _violates(db_session, YEAR_SQL, params, pg.UniqueViolation)
    _year(db_session, SCHOOL_B, "Y1", "2025-06-01", "2026-05-31", cur=True)


def test_year_date_order_and_current_must_be_active(db_session: Session) -> None:
    params = {"s": SCHOOL_A, "n": "Y", "a": "2027-01-01", "b": "2026-01-01", "cur": False}
    _violates(db_session, YEAR_SQL, params, pg.CheckViolation)
    _violates(
        db_session,
        "INSERT INTO academic_years "
        "(id, school_id, name, start_date, end_date, is_current, is_active) "
        "VALUES (gen_random_uuid(), :s, 'Y', '2026-01-01', '2026-12-31', true, false)",
        {"s": SCHOOL_A},
        pg.CheckViolation,
    )


# ------------------------------------------------------------------ classes/sections
def test_section_cannot_reference_another_schools_class(db_session: Session) -> None:
    b_class = _insert_class(db_session, SCHOOL_B)
    _violates(
        db_session,
        "INSERT INTO sections (id, school_id, class_id, name, code) "
        "VALUES (gen_random_uuid(), :s, :c, 'A', 'A')",
        {"s": SCHOOL_A, "c": b_class},
        pg.ForeignKeyViolation,
    )


def test_section_rules(db_session: Session) -> None:
    class_id = _insert_class(db_session, SCHOOL_A)
    sql = (
        "INSERT INTO sections (id, school_id, class_id, name, code, capacity) "
        "VALUES (gen_random_uuid(), :s, :c, 'A', :code, :cap)"
    )
    db_session.execute(text(sql), {"s": SCHOOL_A, "c": class_id, "code": "A", "cap": 30})
    base = {"s": SCHOOL_A, "c": class_id, "cap": None}
    _violates(db_session, sql, {**base, "code": "A"}, pg.UniqueViolation)
    _violates(db_session, sql, {**base, "code": "B", "cap": 0}, pg.CheckViolation)
    _violates(db_session, sql, {**base, "code": "b"}, pg.CheckViolation)


def test_class_with_sections_cannot_be_deleted(db_session: Session) -> None:
    class_id = _insert_class(db_session, SCHOOL_A)
    db_session.execute(
        text(
            "INSERT INTO sections (id, school_id, class_id, name, code) "
            "VALUES (gen_random_uuid(), :s, :c, 'A', 'A')"
        ),
        {"s": SCHOOL_A, "c": class_id},
    )
    _violates(
        db_session, "DELETE FROM classes WHERE id = :c", {"c": class_id}, pg.ForeignKeyViolation
    )


def test_class_and_subject_codes_unique_per_school(db_session: Session) -> None:
    _insert_class(db_session, SCHOOL_A, "C1")
    _insert_class(db_session, SCHOOL_B, "C1")
    _violates(
        db_session,
        "INSERT INTO classes (id, school_id, name, code) VALUES (gen_random_uuid(), :s, 'X', 'C1')",
        {"s": SCHOOL_A},
        pg.UniqueViolation,
    )
    sql = (
        "INSERT INTO subjects (id, school_id, name, code) "
        "VALUES (gen_random_uuid(), :s, 'Maths', 'MATH')"
    )
    db_session.execute(text(sql), {"s": SCHOOL_A})
    db_session.execute(text(sql), {"s": SCHOOL_B})
    _violates(db_session, sql, {"s": SCHOOL_A}, pg.UniqueViolation)
