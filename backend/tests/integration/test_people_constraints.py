"""Database-level same-school and enrollment integrity, exercised with raw SQL."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.security.people_scope import DbStudentAccessPolicy
from tests.conftest import SCHOOL_A, SCHOOL_B

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]


def _violates(session: Session, sql: str, params: dict[str, object], expected: type) -> None:
    with pytest.raises(IntegrityError) as exc_info, session.begin_nested():
        session.execute(text(sql), params)
    assert isinstance(exc_info.value.orig, expected), exc_info.value.orig


def _insert(session: Session, sql: str, **params: object) -> uuid.UUID:
    new_id = uuid.uuid4()
    session.execute(text(sql), {"id": new_id, **params})
    return new_id


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
    }
    for key, cls in (("section", "class"), ("section2", "class2")):
        ids[key] = _insert(
            session,
            "INSERT INTO sections (id, school_id, class_id, name, code) VALUES (:id, :s, :c, 'A', "
            "'A')",
            s=school,
            c=ids[cls],
        )
    ids["student"] = _insert(
        session,
        "INSERT INTO students (id, school_id, admission_number, first_name, last_name) "
        "VALUES (:id, :s, :a, 'F', 'L')",
        s=school,
        a=f"ADM-{tag}",
    )
    return ids


ENROLL = (
    "INSERT INTO student_enrollments (id, school_id, student_id, academic_year_id, class_id, "
    "section_id, enrollment_date, is_current, status) "
    "VALUES (gen_random_uuid(), :school, :student, :year, :class, :section, '2026-06-10', :cur, "
    ":st)"
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
        "student": base["student"],
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "cur": False,
        "st": "active",
    }
    params.update(override)
    return params


@pytest.mark.parametrize("foreign", ["student", "year", "class", "section"])
def test_enrollment_cannot_mix_schools(
    db_session: Session, a: dict[str, uuid.UUID], b: dict[str, uuid.UUID], foreign: str
) -> None:
    overrides: dict[str, object] = {foreign: b[foreign]}
    if foreign == "class":
        overrides["section"] = b["section"]  # keep section-in-class consistent; school differs
    _violates(db_session, ENROLL, _params(a, **overrides), pg.ForeignKeyViolation)


def test_enrollment_section_must_belong_to_class(
    db_session: Session, a: dict[str, uuid.UUID]
) -> None:
    _violates(db_session, ENROLL, _params(a, section=a["section2"]), pg.ForeignKeyViolation)


def test_one_current_and_one_active_per_year(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(ENROLL), _params(a, cur=True))
    # Same year, another active row: blocked.
    _violates(db_session, ENROLL, _params(a), pg.UniqueViolation)
    # A completed row is allowed, but it may not be current (check) ...
    db_session.execute(text(ENROLL), _params(a, st="completed"))
    _violates(db_session, ENROLL, _params(a, st="completed", cur=True), pg.CheckViolation)


def test_second_current_enrollment_blocked(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    other_year = _insert(
        db_session,
        "INSERT INTO academic_years (id, school_id, name, start_date, end_date) "
        "VALUES (:id, :s, 'Y-next', '2027-06-01', '2028-05-31')",
        s=SCHOOL_A,
    )
    db_session.execute(text(ENROLL), _params(a, cur=True))
    _violates(db_session, ENROLL, _params(a, year=other_year, cur=True), pg.UniqueViolation)


def test_student_parent_link_cannot_cross_schools(
    db_session: Session, a: dict[str, uuid.UUID], b: dict[str, uuid.UUID]
) -> None:
    user_b = _insert(
        db_session,
        "INSERT INTO users (id, full_name, phone, school_id) VALUES (:id, 'P', '+919833300001', "
        ":s)",
        s=SCHOOL_B,
    )
    parent_b = _insert(
        db_session,
        "INSERT INTO parents (id, school_id, user_id, first_name, last_name) VALUES (:id, :s, :u, "
        "'P', 'B')",
        s=SCHOOL_B,
        u=user_b,
    )
    link = (
        "INSERT INTO student_parents (student_id, parent_id, school_id, relationship_type) "
        "VALUES (:st, :p, :s, 'mother')"
    )
    _violates(
        db_session, link, {"st": a["student"], "p": parent_b, "s": SCHOOL_A}, pg.ForeignKeyViolation
    )
    _violates(
        db_session, link, {"st": a["student"], "p": parent_b, "s": SCHOOL_B}, pg.ForeignKeyViolation
    )


def test_parent_and_student_users_must_share_the_school(
    db_session: Session, a: dict[str, uuid.UUID]
) -> None:
    user_b = _insert(
        db_session,
        "INSERT INTO users (id, full_name, phone, school_id) VALUES (:id, 'P', '+919833300002', "
        ":s)",
        s=SCHOOL_B,
    )
    _violates(
        db_session,
        "INSERT INTO parents (id, school_id, user_id, first_name, last_name) "
        "VALUES (gen_random_uuid(), :s, :u, 'P', 'A')",
        {"s": SCHOOL_A, "u": user_b},
        pg.ForeignKeyViolation,
    )
    _violates(
        db_session,
        "UPDATE students SET user_id = :u WHERE id = :st",
        {"u": user_b, "st": a["student"]},
        pg.ForeignKeyViolation,
    )


def test_student_rules(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    sql = (
        "INSERT INTO students (id, school_id, admission_number, first_name, last_name, status) "
        "VALUES (gen_random_uuid(), :s, :a, 'F', 'L', :st)"
    )
    _violates(db_session, sql, {"s": SCHOOL_A, "a": "ADM-A", "st": "active"}, pg.UniqueViolation)
    _violates(db_session, sql, {"s": SCHOOL_A, "a": "lower", "st": "active"}, pg.CheckViolation)
    _violates(db_session, sql, {"s": SCHOOL_A, "a": "NEW", "st": "expelled"}, pg.CheckViolation)


def test_student_with_enrollments_cannot_be_deleted(
    db_session: Session, a: dict[str, uuid.UUID]
) -> None:
    db_session.execute(text(ENROLL), _params(a))
    _violates(
        db_session,
        "DELETE FROM students WHERE id = :s",
        {"s": a["student"]},
        pg.ForeignKeyViolation,
    )
    _violates(
        db_session,
        "DELETE FROM sections WHERE id = :s",
        {"s": a["section"]},
        pg.ForeignKeyViolation,
    )


def test_db_student_access_policy(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    user = _insert(
        db_session,
        "INSERT INTO users (id, full_name, phone, school_id) VALUES (:id, 'P', '+919833300003', "
        ":s)",
        s=SCHOOL_A,
    )
    parent = _insert(
        db_session,
        "INSERT INTO parents (id, school_id, user_id, first_name, last_name) VALUES (:id, :s, :u, "
        "'P', 'A')",
        s=SCHOOL_A,
        u=user,
    )
    policy = DbStudentAccessPolicy(db_session)
    assert not policy.is_linked_child(user, a["student"])
    db_session.execute(
        text(
            "INSERT INTO student_parents (student_id, parent_id, school_id, relationship_type) "
            "VALUES (:st, :p, :s, 'father')"
        ),
        {"st": a["student"], "p": parent, "s": SCHOOL_A},
    )
    assert policy.is_linked_child(user, a["student"])
    db_session.execute(text("UPDATE student_parents SET is_active = false"))
    assert not policy.is_linked_child(user, a["student"])
    assert not policy.is_self(user, a["student"])
    assert not policy.is_assigned_student(user, a["student"])  # faculty phase: fail closed
