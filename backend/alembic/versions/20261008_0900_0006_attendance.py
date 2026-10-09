"""attendance sessions, records and record change history

* Creates ``attendance_sessions`` (one roll call: section + date [+ subject] + period),
  ``attendance_records`` (one student's status in a session) and
  ``attendance_record_changes`` (append-only history of record changes).
* Every reference is a composite foreign key carrying ``school_id``. A record repeats its
  session's (school, year, class, section) and points at the student's enrollment with
  the same values, so PostgreSQL itself guarantees the student was placed in exactly
  that school/year/class/section, and the record stays pinned to that enrollment.
* Adds ``UNIQUE (id, school_id, student_id, academic_year_id, class_id, section_id)`` to
  ``student_enrollments`` as the composite-FK target. ``id`` is already unique, so no
  existing row can violate it; no existing column or row changes.
* ``attendance_record_changes`` rejects UPDATE, and DELETE unless the transaction sets
  ``app.attendance_history_purge = 'on'`` (a deliberate maintenance step, e.g. removing
  test data; it guards against accidents, not against the database owner).
* No rows are created.

Revision ID: 0006_attendance
Revises: 0005_faculty_assignments
Create Date: 2026-10-08 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_attendance"
down_revision: str | None = "0005_faculty_assignments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'present', 'absent', 'late', 'excused'"
PLACEMENT = ("school_id", "academic_year_id", "class_id", "section_id")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _fk(cols: list[str], target: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        cols, [f"{target}.{c}" for c in ("id", *cols[1:])], name=name, ondelete="RESTRICT"
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_student_enrollments_placement_key",
        "student_enrollments",
        ["id", "school_id", "student_id", "academic_year_id", "class_id", "section_id"],
    )

    # ------------------------------------------------------------------ sessions
    op.create_table(
        "attendance_sessions",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("period", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column(
            "session_type",
            sa.String(length=10),
            sa.Computed(
                "CASE WHEN subject_id IS NULL THEN 'daily' ELSE 'subject' END", persisted=True
            ),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            sa.Computed("status = 'active'", persisted=True),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name=op.f("ck_attendance_sessions_status_valid")
        ),
        sa.CheckConstraint(
            "period BETWEEN 1 AND 20", name=op.f("ck_attendance_sessions_period_range")
        ),
        sa.CheckConstraint(
            "start_time IS NULL OR end_time IS NULL OR start_time < end_time",
            name=op.f("ck_attendance_sessions_times_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_attendance_sessions_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_attendance_sessions_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        _fk(
            ["academic_year_id", "school_id"],
            "academic_years",
            "fk_attendance_sessions_year_same_school",
        ),
        _fk(
            ["class_id", "school_id"],
            "classes",
            "fk_attendance_sessions_class_same_school",
        ),
        _fk(
            ["section_id", "school_id"],
            "sections",
            "fk_attendance_sessions_section_same_school",
        ),
        _fk(
            ["section_id", "class_id"],
            "sections",
            "fk_attendance_sessions_section_in_class",
        ),
        _fk(
            ["subject_id", "school_id"],
            "subjects",
            "fk_attendance_sessions_subject_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attendance_sessions")),
        sa.UniqueConstraint(
            "id", *PLACEMENT, name="uq_attendance_sessions_placement_key"
        ),
    )
    op.create_index(
        op.f("ix_attendance_sessions_school_id"), "attendance_sessions", ["school_id"]
    )
    op.create_index(
        "ix_attendance_sessions_section_date",
        "attendance_sessions",
        ["school_id", "section_id", "session_date"],
    )
    op.create_index(
        "uq_attendance_sessions_daily_active",
        "attendance_sessions",
        ["academic_year_id", "section_id", "session_date", "period"],
        unique=True,
        postgresql_where=sa.text("subject_id IS NULL AND status = 'active'"),
    )
    op.create_index(
        "uq_attendance_sessions_subject_active",
        "attendance_sessions",
        ["academic_year_id", "section_id", "subject_id", "session_date", "period"],
        unique=True,
        postgresql_where=sa.text("subject_id IS NOT NULL AND status = 'active'"),
    )

    # ------------------------------------------------------------------- records
    op.create_table(
        "attendance_records",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("original_status", sa.String(length=10), nullable=False),
        sa.Column("remarks", sa.String(length=500), nullable=True),
        sa.Column("marked_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corrected_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correction_reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            f"status IN ({STATUSES})", name=op.f("ck_attendance_records_status_valid")
        ),
        sa.CheckConstraint(
            f"original_status IN ({STATUSES})",
            name=op.f("ck_attendance_records_original_status_valid"),
        ),
        sa.CheckConstraint(
            "(corrected_at IS NULL) = (corrected_by_user_id IS NULL) "
            "AND (corrected_at IS NULL) = (correction_reason IS NULL)",
            name=op.f("ck_attendance_records_correction_complete"),
        ),
        sa.CheckConstraint(
            "correction_reason IS NULL OR length(trim(correction_reason)) > 0",
            name=op.f("ck_attendance_records_correction_reason_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_attendance_records_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["marked_by_user_id"],
            ["users.id"],
            name=op.f("fk_attendance_records_marked_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["corrected_by_user_id"],
            ["users.id"],
            name=op.f("fk_attendance_records_corrected_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", *PLACEMENT],
            [f"attendance_sessions.{c}" for c in ("id", *PLACEMENT)],
            name="fk_attendance_records_session_placement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enrollment_id", "school_id", "student_id", *PLACEMENT[1:]],
            [
                f"student_enrollments.{c}"
                for c in ("id", "school_id", "student_id", *PLACEMENT[1:])
            ],
            name="fk_attendance_records_enrollment_placement",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attendance_records")),
        sa.UniqueConstraint(
            "session_id", "student_id", name=op.f("uq_attendance_records_session_id_student_id")
        ),
        sa.UniqueConstraint("id", "school_id", name="uq_attendance_records_id_school_id"),
    )
    op.create_index(op.f("ix_attendance_records_school_id"), "attendance_records", ["school_id"])
    op.create_index("ix_attendance_records_student_id", "attendance_records", ["student_id"])

    # ------------------------------------------------------------ change history
    op.create_table(
        "attendance_record_changes",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.String(length=10), nullable=False),
        sa.Column("new_status", sa.String(length=10), nullable=False),
        sa.Column("previous_remarks", sa.String(length=500), nullable=True),
        sa.Column("new_remarks", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"previous_status IN ({STATUSES})",
            name=op.f("ck_attendance_record_changes_previous_status_valid"),
        ),
        sa.CheckConstraint(
            f"new_status IN ({STATUSES})",
            name=op.f("ck_attendance_record_changes_new_status_valid"),
        ),
        sa.CheckConstraint(
            "previous_status = new_status OR reason IS NOT NULL",
            name=op.f("ck_attendance_record_changes_reason_required_for_status_change"),
        ),
        sa.CheckConstraint(
            "reason IS NULL OR length(trim(reason)) > 0",
            name=op.f("ck_attendance_record_changes_reason_not_blank"),
        ),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_attendance_record_changes_sequence_positive")
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_attendance_record_changes_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name=op.f("fk_attendance_record_changes_changed_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["record_id", "school_id"],
            ["attendance_records.id", "attendance_records.school_id"],
            name="fk_attendance_record_changes_record_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attendance_record_changes")),
        sa.UniqueConstraint(
            "record_id", "sequence", name=op.f("uq_attendance_record_changes_record_id_sequence")
        ),
    )
    op.create_index(
        op.f("ix_attendance_record_changes_school_id"), "attendance_record_changes", ["school_id"]
    )
    op.execute(
        """
        CREATE FUNCTION attendance_record_changes_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('app.attendance_history_purge', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'attendance_record_changes is append-only'
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_attendance_record_changes_append_only "
        "BEFORE UPDATE OR DELETE ON attendance_record_changes "
        "FOR EACH ROW EXECUTE FUNCTION attendance_record_changes_append_only()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_attendance_record_changes_append_only ON attendance_record_changes"
    )
    op.execute("DROP FUNCTION attendance_record_changes_append_only()")
    op.drop_index(
        op.f("ix_attendance_record_changes_school_id"), table_name="attendance_record_changes"
    )
    op.drop_table("attendance_record_changes")
    op.drop_index("ix_attendance_records_student_id", table_name="attendance_records")
    op.drop_index(op.f("ix_attendance_records_school_id"), table_name="attendance_records")
    op.drop_table("attendance_records")
    op.drop_index(
        "uq_attendance_sessions_subject_active",
        table_name="attendance_sessions",
        postgresql_where=sa.text("subject_id IS NOT NULL AND status = 'active'"),
    )
    op.drop_index(
        "uq_attendance_sessions_daily_active",
        table_name="attendance_sessions",
        postgresql_where=sa.text("subject_id IS NULL AND status = 'active'"),
    )
    op.drop_index("ix_attendance_sessions_section_date", table_name="attendance_sessions")
    op.drop_index(op.f("ix_attendance_sessions_school_id"), table_name="attendance_sessions")
    op.drop_table("attendance_sessions")
    op.drop_constraint(
        "uq_student_enrollments_placement_key", "student_enrollments", type_="unique"
    )
