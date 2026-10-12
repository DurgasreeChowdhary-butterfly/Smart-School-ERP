"""Migration 0008 is additive: Phase 1-7 data survives upgrade/downgrade/re-upgrade, and
the ``assignment.submit`` permission is added and removed with it."""

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
from tests.integration.test_migration_0006 import _seed_phase_5
from tests.integration.test_migration_0007 import _seed_phase_6, _snapshot

pytestmark = pytest.mark.db

NEW_TABLES = (
    "assignments",
    "assignment_submissions",
    "assignment_submission_revisions",
    "assignment_review_changes",
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


def _tables(conn: Connection) -> dict[str, bool]:
    return {
        name: conn.execute(text("SELECT to_regclass(:t)"), {"t": name}).scalar() is not None
        for name in NEW_TABLES
    }


def _seed_phase_7(conn: Connection, school: uuid.UUID) -> None:
    conn.execute(
        text(
            "INSERT INTO exams (id, school_id, academic_year_id, class_id, name, exam_type, "
            "start_date, end_date, created_by_user_id) SELECT gen_random_uuid(), :s, y.id, c.id, "
            "'Legacy exam', 'midterm', y.start_date, y.start_date, u.id FROM academic_years y, "
            "classes c, users u WHERE y.school_id = :s AND c.school_id = :s AND u.school_id = :s "
            "AND u.email = 'legacy-t@x.test'"
        ),
        {"s": school},
    )


def _full_snapshot(conn: Connection, school: uuid.UUID) -> tuple[object, ...]:
    return (
        *_snapshot(conn, school),
        list(
            conn.execute(text("SELECT name, status FROM exams WHERE school_id = :s"), {"s": school})
        ),
        sorted(
            conn.execute(
                text(
                    "SELECT r.name, p.name FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "JOIN permissions p ON p.id = rp.permission_id"
                )
            ).all()
        ),
    )


def _grants(conn: Connection) -> set[str]:
    return set(
        conn.execute(
            text(
                "SELECT r.name FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
                "JOIN permissions p ON p.id = rp.permission_id WHERE p.name = 'assignment.submit'"
            )
        ).scalars()
    )


def test_0008_upgrade_downgrade_reupgrade_preserves_data(connection: Connection) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0007_examinations_marks")
    assert _tables(connection) == dict.fromkeys(NEW_TABLES, False)
    assert _grants(connection) == set()

    school = _seed_phase_1_to_4(connection)
    _seed_phase_5(connection, school)
    _seed_phase_6(connection, school)
    _seed_phase_7(connection, school)
    before = _full_snapshot(connection, school)

    command.upgrade(cfg, "0008_assignments_submissions")
    assert MigrationContext.configure(connection).get_current_revision() == (
        "0008_assignments_submissions"
    )
    assert _tables(connection) == dict.fromkeys(NEW_TABLES, True)
    assert _grants(connection) == {"STUDENT"}
    after = _full_snapshot(connection, school)
    # Every Phase 1-7 row is unchanged; the only catalog change is the one new grant.
    rp_count = 10  # _snapshot's count(role_permissions)
    assert (
        after[:rp_count] + after[rp_count + 1 : -1] == before[:rp_count] + before[rp_count + 1 : -1]
    )
    assert after[rp_count] == [before[rp_count][0] + 1]  # type: ignore[index]
    assert set(after[-1]) - set(before[-1]) == {("STUDENT", "assignment.submit")}  # type: ignore[arg-type]
    for table in NEW_TABLES:
        assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0  # noqa: S608

    command.downgrade(cfg, "0007_examinations_marks")
    assert _tables(connection) == dict.fromkeys(NEW_TABLES, False)
    assert _full_snapshot(connection, school) == before
    assert not connection.execute(
        text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'assignment%'")
    ).scalar()

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
        "fk_assignments_year_same_school",
        "fk_assignments_class_same_school",
        "fk_assignments_section_same_school",
        "fk_assignments_section_in_class",
        "fk_assignments_subject_same_school",
        "ck_assignments_max_marks_range",
        "fk_assignment_submissions_assignment_placement",
        "fk_assignment_submissions_max_marks",
        "fk_assignment_submissions_enrollment_placement",
        "uq_assignment_submissions_assignment_id_student_id",
        "ck_assignment_submissions_marks_range",
        "uq_assignment_submission_revisions_submission_id_revision",
        "uq_assignment_review_changes_submission_id_sequence",
        "ck_assignment_review_changes_reason_required_for_change",
    } <= constraints
    assert not [name for name in constraints if "_ck_" in name]  # no doubled "ck_<table>_" prefix
    triggers = set(
        connection.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg_assignment%'")
        ).scalars()
    )
    assert triggers == {
        "trg_assignments_lifecycle",
        "trg_assignment_submissions_guard",
        "trg_assignment_revisions_guard",
        "trg_assignment_review_changes_append_only",
    }
