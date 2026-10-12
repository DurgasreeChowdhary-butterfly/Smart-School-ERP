"""Migration 0006 is additive: Phase 1-5 data survives upgrade/downgrade/re-upgrade.

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
from tests.integration.test_migration_0005 import _seed_phase_1_to_4, _snapshot

pytestmark = pytest.mark.db

NEW_TABLES = ("attendance_sessions", "attendance_records", "attendance_record_changes")
ENROLLMENT_KEY = "uq_student_enrollments_placement_key"


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


def _has(conn: Connection, sql: str, **params: object) -> bool:
    return bool(conn.execute(text(sql), params).scalar())


def _seed_phase_5(conn: Connection, school: uuid.UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO faculty (id, school_id, user_id, employee_id, first_name, last_name) "
            "SELECT gen_random_uuid(), :s, id, 'EMP-1', 'T', 'R' FROM users "
            "WHERE school_id = :s AND email = 'legacy-t@x.test'"
        ),
        {"s": school},
    )
    conn.execute(
        text(
            "INSERT INTO faculty_assignments (id, school_id, faculty_id, academic_year_id, "
            "class_id, section_id, subject_id) SELECT gen_random_uuid(), :s, f.id, y.id, c.id, "
            "sec.id, sub.id FROM faculty f, academic_years y, classes c, sections sec, "
            "subjects sub "
            "WHERE f.school_id = :s AND y.school_id = :s AND c.school_id = :s "
            "AND sec.school_id = :s AND sub.school_id = :s"
        ),
        {"s": school},
    )


def _phase_5_snapshot(conn: Connection, school: uuid.UUID) -> tuple[object, ...]:
    return (
        *_snapshot(conn, school),
        list(
            conn.execute(
                text("SELECT employee_id, status FROM faculty WHERE school_id = :s"), {"s": school}
            ).all()
        ),
        list(
            conn.execute(
                text(
                    "SELECT section_id, subject_id, status FROM faculty_assignments "
                    "WHERE school_id = :s"
                ),
                {"s": school},
            ).all()
        ),
    )


def test_0006_upgrade_downgrade_reupgrade_preserves_data(connection: Connection) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0005_faculty_assignments")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)
    assert not _has(
        connection, "SELECT count(*) FROM pg_constraint WHERE conname = :n", n=ENROLLMENT_KEY
    )

    school = _seed_phase_1_to_4(connection)
    _seed_phase_5(connection, school)
    before = _phase_5_snapshot(connection, school)
    assert len(before[-1]) == 1  # type: ignore[arg-type]

    command.upgrade(cfg, "0006_attendance")
    assert MigrationContext.configure(connection).get_current_revision() == "0006_attendance"
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, True)
    assert _phase_5_snapshot(connection, school) == before
    # No attendance was invented.
    for table in NEW_TABLES:
        assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0  # noqa: S608
    assert _has(
        connection, "SELECT count(*) FROM pg_constraint WHERE conname = :n", n=ENROLLMENT_KEY
    )
    assert _has(
        connection,
        "SELECT count(*) FROM pg_trigger "
        "WHERE tgname = 'trg_attendance_record_changes_append_only'",
    )

    command.downgrade(cfg, "0005_faculty_assignments")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)
    assert _phase_5_snapshot(connection, school) == before
    assert not _has(
        connection, "SELECT count(*) FROM pg_constraint WHERE conname = :n", n=ENROLLMENT_KEY
    )
    assert not _has(
        connection,
        "SELECT count(*) FROM pg_proc WHERE proname = 'attendance_record_changes_append_only'",
    )

    # Compare at this revision: later migrations may legitimately add catalog rows.
    command.upgrade(cfg, "0006_attendance")
    assert _phase_5_snapshot(connection, school) == before
    command.upgrade(cfg, "head")
    constraints = set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid IN ("
                "'attendance_sessions'::regclass, 'attendance_records'::regclass, "
                "'attendance_record_changes'::regclass)"
            )
        ).scalars()
    )
    assert {
        "fk_attendance_sessions_year_same_school",
        "fk_attendance_sessions_class_same_school",
        "fk_attendance_sessions_section_same_school",
        "fk_attendance_sessions_section_in_class",
        "fk_attendance_sessions_subject_same_school",
        "uq_attendance_sessions_placement_key",
        "fk_attendance_records_session_placement",
        "fk_attendance_records_enrollment_placement",
        "uq_attendance_records_session_id_student_id",
        "ck_attendance_records_status_valid",
        "fk_attendance_record_changes_record_same_school",
        "uq_attendance_record_changes_record_id_sequence",
        "ck_attendance_record_changes_reason_required_for_status_change",
    } <= constraints
    for name, predicate in (
        ("uq_attendance_sessions_daily_active", "subject_id IS NULL"),
        ("uq_attendance_sessions_subject_active", "subject_id IS NOT NULL"),
    ):
        index = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": name}
        ).scalar()
        assert index is not None
        assert "UNIQUE" in index
        assert predicate in index
