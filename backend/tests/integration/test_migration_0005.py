"""Migration 0005 is additive: Phase 1-4 data survives upgrade/downgrade/re-upgrade.

Runs inside one rolled-back transaction (PostgreSQL DDL is transactional).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from tests.conftest import alembic_config

pytestmark = pytest.mark.db

NEW_TABLES = ("faculty", "faculty_assignments")


@pytest.fixture
def connection(db_engine: Engine) -> Iterator[Connection]:
    conn = db_engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


def _tables_exist(conn: Connection) -> dict[str, bool]:
    return {
        name: conn.execute(text("SELECT to_regclass(:t)"), {"t": name}).scalar() is not None
        for name in NEW_TABLES
    }


def _snapshot(conn: Connection, school_id: uuid.UUID) -> tuple[object, ...]:
    def rows(sql: str) -> list[object]:
        return list(conn.execute(text(sql), {"s": school_id}).all())

    return (
        rows("SELECT name, code, is_active, timezone FROM schools WHERE id = :s"),
        rows("SELECT name, is_current FROM academic_years WHERE school_id = :s ORDER BY name"),
        rows(
            "SELECT c.code, s.code FROM classes c JOIN sections s ON s.class_id = c.id "
            "WHERE c.school_id = :s ORDER BY 1, 2"
        ),
        rows("SELECT code FROM subjects WHERE school_id = :s"),
        rows(
            "SELECT u.email, u.phone, u.password_hash, u.status, r.name FROM users u "
            "JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id "
            "WHERE u.school_id = :s ORDER BY 1, 2"
        ),
        rows("SELECT admission_number, status, user_id FROM students WHERE school_id = :s"),
        rows("SELECT first_name, user_id, is_active FROM parents WHERE school_id = :s"),
        rows("SELECT relationship_type, is_primary FROM student_parents WHERE school_id = :s"),
        rows(
            "SELECT student_id, section_id, status, is_current FROM student_enrollments "
            "WHERE school_id = :s"
        ),
        [conn.execute(text("SELECT count(*) FROM auth_sessions")).scalar()],
        [conn.execute(text("SELECT count(*) FROM role_permissions")).scalar()],
    )


def _seed_phase_1_to_4(conn: Connection) -> uuid.UUID:
    school, year, cls, section, student, parent, teacher = (uuid.uuid4() for _ in range(7))
    stmts = [
        "INSERT INTO schools (id, name, code) VALUES (:school, 'Legacy', 'LEGACY5')",
        "INSERT INTO academic_years (id, school_id, name, start_date, end_date, is_current) "
        "VALUES (:year, :school, '2026-27', '2026-06-01', '2027-05-31', true)",
        "INSERT INTO classes (id, school_id, name, code) VALUES (:cls, :school, 'C1', 'C1')",
        "INSERT INTO sections (id, school_id, class_id, name, code) "
        "VALUES (:section, :school, :cls, 'A', 'A')",
        "INSERT INTO subjects (id, school_id, name, code) "
        "VALUES (gen_random_uuid(), :school, 'Maths', 'MATH')",
        # A FACULTY user that pre-dates faculty records (e.g. created with the CLI).
        "INSERT INTO users (id, full_name, email, password_hash, school_id) "
        "VALUES (:teacher, 'Teacher', 'legacy-t@x.test', '$argon2id$placeholder', :school)",
        "INSERT INTO user_roles (user_id, role_id) SELECT :teacher, id FROM roles "
        "WHERE name = 'FACULTY'",
        "INSERT INTO users (id, full_name, phone, school_id) "
        "VALUES (:parent, 'Parent', '+919800000555', :school)",
        "INSERT INTO user_roles (user_id, role_id) SELECT :parent, id FROM roles "
        "WHERE name = 'PARENT'",
        "INSERT INTO parents (id, school_id, user_id, first_name, last_name) "
        "VALUES (:parent, :school, :parent, 'P', 'Q')",
        "INSERT INTO students (id, school_id, admission_number, first_name, last_name) "
        "VALUES (:student, :school, 'ADM-1', 'S', 'T')",
        "INSERT INTO student_parents (student_id, parent_id, school_id, relationship_type) "
        "VALUES (:student, :parent, :school, 'mother')",
        "INSERT INTO student_enrollments (id, school_id, student_id, academic_year_id, class_id, "
        "section_id, enrollment_date, is_current) VALUES (gen_random_uuid(), :school, :student, "
        ":year, :cls, :section, '2026-06-10', true)",
        "INSERT INTO auth_sessions (id, user_id, auth_method, expires_at) "
        "VALUES (gen_random_uuid(), :teacher, 'password', now() + interval '1 day')",
    ]
    params = {
        "school": school,
        "year": year,
        "cls": cls,
        "section": section,
        "student": student,
        "parent": parent,
        "teacher": teacher,
    }
    for sql in stmts:
        conn.execute(text(sql), params)
    return school


def test_0005_upgrade_downgrade_reupgrade_preserves_data(connection: Connection) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0004_students_parents")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)

    school = _seed_phase_1_to_4(connection)
    before = _snapshot(connection, school)

    command.upgrade(cfg, "0005_faculty_assignments")
    assert (
        MigrationContext.configure(connection).get_current_revision() == "0005_faculty_assignments"
    )
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, True)
    assert _snapshot(connection, school) == before
    # No faculty records or assignments were invented (not even for the FACULTY user).
    assert connection.execute(text("SELECT count(*) FROM faculty")).scalar() == 0
    assert connection.execute(text("SELECT count(*) FROM faculty_assignments")).scalar() == 0

    command.downgrade(cfg, "0004_students_parents")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)
    assert _snapshot(connection, school) == before
    # The Phase 4 composite targets that 0005 relies on are untouched by its downgrade.
    kept = connection.execute(
        text(
            "SELECT count(*) FROM pg_constraint WHERE conname IN ("
            "'uq_academic_years_id_school_id', 'uq_sections_id_school_id', "
            "'uq_sections_id_class_id', 'uq_subjects_id_school_id', 'uq_users_id_school_id', "
            "'uq_classes_id_school_id')"
        )
    ).scalar()
    assert kept == 6

    command.upgrade(cfg, "head")
    assert _snapshot(connection, school) == before
    constraints = set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid IN "
                "('faculty'::regclass, 'faculty_assignments'::regclass)"
            )
        ).scalars()
    )
    assert {
        "uq_faculty_id_school_id",
        "uq_faculty_school_id_employee_id",
        "uq_faculty_user_id",
        "fk_faculty_user_same_school",
        "fk_faculty_assignments_faculty_same_school",
        "fk_faculty_assignments_year_same_school",
        "fk_faculty_assignments_class_same_school",
        "fk_faculty_assignments_section_same_school",
        "fk_faculty_assignments_section_in_class",
        "fk_faculty_assignments_subject_same_school",
        "ck_faculty_assignments_effective_dates_ordered",
    } <= constraints
    index = connection.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_faculty_assignments_one_active'"
        )
    ).scalar()
    assert index is not None
    assert "UNIQUE" in index
    assert "WHERE ((status)::text = 'active'::text)" in index
