"""Migration 0004 is additive: Phase 1-3 data survives upgrade/downgrade/re-upgrade.

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

NEW_TABLES = ("students", "parents", "student_parents", "student_enrollments")


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
    return (
        conn.execute(
            text("SELECT name, code, is_active FROM schools WHERE id = :s"), {"s": school_id}
        ).one(),
        conn.execute(
            text("SELECT name, is_current FROM academic_years WHERE school_id = :s ORDER BY name"),
            {"s": school_id},
        ).all(),
        conn.execute(
            text(
                "SELECT c.code, s.code FROM classes c JOIN sections s ON s.class_id = c.id WHERE "
                "c.school_id = :s"
            ),
            {"s": school_id},
        ).all(),
        conn.execute(
            text("SELECT code FROM subjects WHERE school_id = :s"), {"s": school_id}
        ).all(),
        conn.execute(
            text(
                "SELECT u.email, u.password_hash, r.name FROM users u "
                "JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id "
                "WHERE u.school_id = :s"
            ),
            {"s": school_id},
        ).all(),
        conn.execute(text("SELECT count(*) FROM auth_sessions")).scalar(),
    )


def test_0004_preserves_existing_data(connection: Connection) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0003_school_academic")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)

    school = uuid.uuid4()
    user = uuid.uuid4()
    class_id = uuid.uuid4()
    connection.execute(
        text("INSERT INTO schools (id, name, code) VALUES (:s, 'Legacy School', 'LEGACY')"),
        {"s": school},
    )
    connection.execute(
        text(
            "INSERT INTO academic_years (id, school_id, name, start_date, end_date, is_current) "
            "VALUES (gen_random_uuid(), :s, '2026-27', '2026-06-01', '2027-05-31', true)"
        ),
        {"s": school},
    )
    connection.execute(
        text("INSERT INTO classes (id, school_id, name, code) VALUES (:c, :s, 'Class 1', 'C1')"),
        {"c": class_id, "s": school},
    )
    connection.execute(
        text(
            "INSERT INTO sections (id, school_id, class_id, name, code) "
            "VALUES (gen_random_uuid(), :s, :c, 'A', 'A')"
        ),
        {"s": school, "c": class_id},
    )
    connection.execute(
        text(
            "INSERT INTO subjects (id, school_id, name, code) VALUES (gen_random_uuid(), :s, "
            "'Maths', 'MATH')"
        ),
        {"s": school},
    )
    connection.execute(
        text(
            "INSERT INTO users (id, full_name, email, password_hash, school_id) "
            "VALUES (:u, 'Admin', 'legacy@x.test', '$argon2id$placeholder', :s)"
        ),
        {"u": user, "s": school},
    )
    connection.execute(
        text(
            "INSERT INTO user_roles (user_id, role_id) SELECT :u, id FROM roles WHERE name = "
            "'SCHOOL_ADMIN'"
        ),
        {"u": user},
    )
    connection.execute(
        text(
            "INSERT INTO auth_sessions (id, user_id, auth_method, expires_at) "
            "VALUES (gen_random_uuid(), :u, 'password', now() + interval '1 day')"
        ),
        {"u": user},
    )
    before = _snapshot(connection, school)

    command.upgrade(cfg, "0004_students_parents")
    assert MigrationContext.configure(connection).get_current_revision() == "0004_students_parents"
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, True)
    assert _snapshot(connection, school) == before
    # No students/parents were invented.
    assert connection.execute(text("SELECT count(*) FROM students")).scalar() == 0
    assert connection.execute(text("SELECT count(*) FROM parents")).scalar() == 0

    command.downgrade(cfg, "0003_school_academic")
    assert _snapshot(connection, school) == before
    command.upgrade(cfg, "0004_students_parents")
    assert _snapshot(connection, school) == before
    constraints = set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conname IN ("
                "'uq_academic_years_id_school_id', 'uq_sections_id_school_id', "
                "'uq_sections_id_class_id', 'uq_subjects_id_school_id', 'uq_users_id_school_id')"
            )
        ).scalars()
    )
    assert len(constraints) == 5
