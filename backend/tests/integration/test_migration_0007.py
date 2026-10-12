"""Migration 0007 is additive: Phase 1-6 data survives upgrade/downgrade/re-upgrade.

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
from tests.integration.test_migration_0005 import _seed_phase_1_to_4
from tests.integration.test_migration_0006 import _phase_5_snapshot, _seed_phase_5

pytestmark = pytest.mark.db

NEW_TABLES = (
    "exams",
    "exam_subjects",
    "exam_marks",
    "exam_mark_changes",
    "exam_publications",
    "exam_report_cards",
)
FUNCTIONS = (
    "exams_lifecycle_guard",
    "exam_subjects_config_guard",
    "exam_marks_guard",
    "exam_history_append_only",
    "exam_report_cards_insert_guard",
    "exam_publications_guard",
)


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


def _count(conn: Connection, sql: str, **params: object) -> int:
    return int(conn.execute(text(sql), params).scalar() or 0)


def _seed_phase_6(conn: Connection, school: uuid.UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO attendance_sessions (id, school_id, academic_year_id, class_id, "
            "section_id, subject_id, session_date, created_by_user_id) "
            "SELECT gen_random_uuid(), e.school_id, e.academic_year_id, e.class_id, "
            "e.section_id, NULL, e.enrollment_date, u.id FROM student_enrollments e, users u "
            "WHERE e.school_id = :s AND u.school_id = :s AND u.email = 'legacy-t@x.test'"
        ),
        {"s": school},
    )
    conn.execute(
        text(
            "INSERT INTO attendance_records (id, school_id, session_id, enrollment_id, "
            "student_id, academic_year_id, class_id, section_id, status, original_status, "
            "marked_by_user_id, marked_at) "
            "SELECT gen_random_uuid(), e.school_id, s.id, e.id, e.student_id, "
            "e.academic_year_id, e.class_id, e.section_id, 'present', 'present', "
            "s.created_by_user_id, now() FROM student_enrollments e "
            "JOIN attendance_sessions s ON s.section_id = e.section_id WHERE e.school_id = :s"
        ),
        {"s": school},
    )


def _snapshot(conn: Connection, school: uuid.UUID) -> tuple[object, ...]:
    return (
        *_phase_5_snapshot(conn, school),
        list(
            conn.execute(
                text(
                    "SELECT student_id, status, original_status FROM attendance_records "
                    "WHERE school_id = :s"
                ),
                {"s": school},
            ).all()
        ),
    )


def test_0007_upgrade_downgrade_reupgrade_preserves_data(connection: Connection) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0006_attendance")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)

    school = _seed_phase_1_to_4(connection)
    _seed_phase_5(connection, school)
    _seed_phase_6(connection, school)
    before = _snapshot(connection, school)
    assert len(before[-1]) == 1  # type: ignore[arg-type]

    command.upgrade(cfg, "0007_examinations_marks")
    assert MigrationContext.configure(connection).get_current_revision() == (
        "0007_examinations_marks"
    )
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, True)
    assert _snapshot(connection, school) == before
    for table in NEW_TABLES:  # no exams or marks were invented
        assert _count(connection, f"SELECT count(*) FROM {table}") == 0  # noqa: S608

    command.downgrade(cfg, "0006_attendance")
    assert _tables_exist(connection) == dict.fromkeys(NEW_TABLES, False)
    assert _snapshot(connection, school) == before
    for function in FUNCTIONS:
        assert not _count(connection, "SELECT count(*) FROM pg_proc WHERE proname = :f", f=function)

    # Compare at this revision: later migrations may legitimately add catalog rows.
    command.upgrade(cfg, "0007_examinations_marks")
    assert _snapshot(connection, school) == before
    command.upgrade(cfg, "head")
    constraints = set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = ANY(:tables)"
            ),
            {"tables": list(NEW_TABLES)},
        ).scalars()
    )
    assert {
        "fk_exams_year_same_school",
        "fk_exams_class_same_school",
        "fk_exams_section_same_school",
        "fk_exams_section_in_class",
        "uq_exams_placement_key",
        "ck_exams_status_valid",
        "ck_exams_cancellation_complete",
        "fk_exam_subjects_exam_same_school",
        "fk_exam_subjects_subject_same_school",
        "uq_exam_subjects_exam_id_subject_id",
        "uq_exam_subjects_marks_key",
        "ck_exam_subjects_max_marks_range",
        "ck_exam_subjects_pass_marks_range",
        "fk_exam_marks_component",
        "fk_exam_marks_exam_placement",
        "fk_exam_marks_enrollment_placement",
        "uq_exam_marks_exam_subject_id_student_id",
        "ck_exam_marks_marks_range",
        "ck_exam_marks_marks_match_status",
        "fk_exam_mark_changes_mark_same_school",
        "uq_exam_mark_changes_mark_id_sequence",
        "ck_exam_mark_changes_reason_not_blank",
        "fk_exam_publications_exam_same_school",
        "uq_exam_publications_exam_id_version",
        "fk_exam_report_cards_publication",
        "fk_exam_report_cards_exam_placement",
        "fk_exam_report_cards_enrollment_placement",
        "uq_exam_report_cards_publication_id_student_id",
    } <= constraints
    # Names follow the model naming convention exactly (no doubled "ck_<table>_" prefix).
    assert not [name for name in constraints if name.count("ck_") > 1]
    for name, fragments in (
        ("uq_exams_name_per_placement", ("UNIQUE", "NULLS NOT DISTINCT", "<> 'cancelled'")),
        ("uq_exam_publications_one_active", ("UNIQUE", "status")),
    ):
        index = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": name}
        ).scalar()
        assert index is not None, name
        for fragment in fragments:
            assert fragment in index, (name, fragment)
    triggers = set(
        connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_exam%'")
        ).scalars()
    )
    assert triggers == {
        "trg_exams_lifecycle",
        "trg_exam_subjects_config",
        "trg_exam_marks_guard",
        "trg_exam_mark_changes_append_only",
        "trg_exam_report_cards_append_only",
        "trg_exam_report_cards_insert",
        "trg_exam_publications_guard",
    }
