"""examinations, marks, correction history, publications and report cards

* Creates ``exams`` (one exam for a class, optionally one section), ``exam_subjects``
  (its subject components with maximum/passing marks), ``exam_marks`` (one student's
  result in one component), ``exam_mark_changes`` (append-only correction history),
  ``exam_publications`` (numbered result publications) and ``exam_report_cards``
  (each student's result frozen at publication).
* Every reference is a composite foreign key carrying ``school_id``. A mark repeats its
  component's ``max_marks`` (so ``marks_obtained <= max_marks`` is a CHECK and a
  referenced maximum cannot change) and points at the student's enrollment with the
  same school/year/class/section.
* Triggers enforce what a foreign key cannot:
  - ``exams``: created as ``draft``; only the documented status transitions; a
    scheduled or marks-entry exam has components; ``published`` <=> an active publication exists;
    placement is immutable; published and cancelled exams are read-only.
  - ``exam_subjects``: changed only while the exam is ``draft`` or ``scheduled``;
    removed only while it is ``draft`` (and a marks reference blocks removal by FK).
  - ``exam_marks``: written only while the exam is in ``marks_entry``, only for the
    exam's section, never re-pointed, never deleted.
  - ``exam_mark_changes`` and ``exam_report_cards``: append-only; report cards are only
    added to an active publication of an exam being published.
  - ``exam_publications``: created active while the exam is in ``marks_entry``; the only
    permitted change is active -> withdrawn.
  DELETE of history/snapshot rows is refused unless the transaction sets
  ``app.exam_history_purge = 'on'`` (a deliberate maintenance step, e.g. removing test
  data; it guards against accidents, not against the database owner).
* Uses only the composite-FK targets created by earlier migrations
  (``uq_*_id_school_id``, ``uq_sections_id_class_id``,
  ``uq_student_enrollments_placement_key``): no existing table, column or row changes.
  No rows are created.

Revision ID: 0007_examinations_marks
Revises: 0006_attendance
Create Date: 2026-10-09 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_examinations_marks"
down_revision: str | None = "0006_attendance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXAM_STATUSES = "'draft', 'scheduled', 'marks_entry', 'published', 'cancelled'"
EXAM_TYPES = "'unit_test', 'periodic', 'midterm', 'term', 'final', 'practical', 'other'"
MARK_STATUSES = "'present', 'absent', 'exempt'"
RESULTS = "'pass', 'fail', 'incomplete', 'not_applicable'"
MARKS = sa.Numeric(6, 2)

ENROLLMENT_PLACEMENT = [
    "enrollment_id",
    "school_id",
    "student_id",
    "academic_year_id",
    "class_id",
    "section_id",
]
ENROLLMENT_TARGET = ["id", "school_id", "student_id", "academic_year_id", "class_id", "section_id"]
EXAM_PLACEMENT = ["exam_id", "school_id", "academic_year_id", "class_id"]
EXAM_TARGET = ["id", "school_id", "academic_year_id", "class_id"]


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


def _fk(cols: list[str], table: str, targets: list[str], name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        cols, [f"{table}.{c}" for c in targets], name=name, ondelete="RESTRICT"
    )


def _school(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["school_id"], ["schools.id"], name=f"fk_{table}_school_id_schools", ondelete="RESTRICT"
    )


def _user(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["users.id"], name=f"fk_{table}_{column}_users", ondelete="RESTRICT"
    )


def _school_index(table: str) -> None:
    op.create_index(f"ix_{table}_school_id", table, ["school_id"])


# Trigger bodies -------------------------------------------------------------------------
EXAMS_GUARD = """
CREATE FUNCTION exams_lifecycle_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'exams are created as draft'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_lifecycle';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.school_id, NEW.academic_year_id, NEW.class_id, NEW.section_id)
       IS DISTINCT FROM (OLD.school_id, OLD.academic_year_id, OLD.class_id, OLD.section_id) THEN
        RAISE EXCEPTION 'exam placement is immutable'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_placement_immutable';
    END IF;
    IF OLD.status = 'cancelled' THEN
        RAISE EXCEPTION 'cancelled exams are read-only'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_lifecycle';
    END IF;
    IF NEW.status = OLD.status THEN
        IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'published exams are read-only; reopen first'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_lifecycle';
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) NOT IN (
        ('draft', 'scheduled'), ('scheduled', 'draft'), ('scheduled', 'marks_entry'),
        ('marks_entry', 'published'), ('published', 'marks_entry'),
        ('draft', 'cancelled'), ('scheduled', 'cancelled'), ('marks_entry', 'cancelled')
    ) THEN
        RAISE EXCEPTION 'invalid exam status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_lifecycle';
    END IF;
    IF NEW.status IN ('scheduled', 'marks_entry')
       AND NOT EXISTS (SELECT 1 FROM exam_subjects WHERE exam_id = NEW.id) THEN
        RAISE EXCEPTION 'a scheduled exam needs at least one subject'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_scheduled_has_subjects';
    END IF;
    IF NEW.status = 'published' AND NOT EXISTS (
        SELECT 1 FROM exam_publications WHERE exam_id = NEW.id AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'a published exam needs an active publication'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_published_has_publication';
    END IF;
    IF OLD.status = 'published' AND EXISTS (
        SELECT 1 FROM exam_publications WHERE exam_id = NEW.id AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'withdraw the active publication before reopening'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exams_published_has_publication';
    END IF;
    RETURN NEW;
END;
$$
"""

EXAM_SUBJECTS_GUARD = """
CREATE FUNCTION exam_subjects_config_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target uuid;
    exam_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('app.exam_history_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        target := OLD.exam_id;
    ELSE
        target := NEW.exam_id;
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.exam_id, NEW.school_id, NEW.subject_id)
       IS DISTINCT FROM (OLD.exam_id, OLD.school_id, OLD.subject_id) THEN
        RAISE EXCEPTION 'exam subject placement is immutable'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_subjects_config_frozen';
    END IF;
    SELECT status INTO exam_status FROM exams WHERE id = target;
    IF TG_OP = 'DELETE' AND exam_status IS NOT NULL AND exam_status <> 'draft' THEN
        RAISE EXCEPTION 'exam subjects can only be removed while the exam is draft'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_subjects_removal_draft_only';
    END IF;
    IF exam_status IS NOT NULL AND exam_status NOT IN ('draft', 'scheduled') THEN
        RAISE EXCEPTION 'exam subjects can only change while the exam is draft or scheduled'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_subjects_config_frozen';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""

EXAM_MARKS_GUARD = """
CREATE FUNCTION exam_marks_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    exam_status text;
    exam_section uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('app.exam_history_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'exam marks are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.school_id, NEW.exam_id, NEW.exam_subject_id, NEW.max_marks, NEW.student_id,
        NEW.enrollment_id, NEW.academic_year_id, NEW.class_id, NEW.section_id,
        NEW.entered_by_user_id, NEW.entered_at
    ) IS DISTINCT FROM (
        OLD.school_id, OLD.exam_id, OLD.exam_subject_id, OLD.max_marks, OLD.student_id,
        OLD.enrollment_id, OLD.academic_year_id, OLD.class_id, OLD.section_id,
        OLD.entered_by_user_id, OLD.entered_at
    ) THEN
        RAISE EXCEPTION 'only the result, remarks and correction fields of a mark can change'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_marks_immutable';
    END IF;
    SELECT status, section_id INTO exam_status, exam_section FROM exams WHERE id = NEW.exam_id;
    IF exam_status IS DISTINCT FROM 'marks_entry' THEN
        RAISE EXCEPTION 'marks can only be written while the exam is open for marks entry'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_marks_exam_accepts_marks';
    END IF;
    IF exam_section IS NOT NULL AND exam_section <> NEW.section_id THEN
        RAISE EXCEPTION 'the student is not in the section this exam is for'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_marks_section_in_exam';
    END IF;
    RETURN NEW;
END;
$$
"""

HISTORY_GUARD = """
CREATE FUNCTION exam_history_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('app.exam_history_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

REPORT_CARDS_INSERT_GUARD = """
CREATE FUNCTION exam_report_cards_insert_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM exam_publications p JOIN exams e ON e.id = p.exam_id
        WHERE p.id = NEW.publication_id AND p.status = 'active' AND e.status = 'marks_entry'
    ) THEN
        RAISE EXCEPTION 'report cards are only added while an exam is being published'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_report_cards_frozen';
    END IF;
    RETURN NEW;
END;
$$
"""

PUBLICATIONS_GUARD = """
CREATE FUNCTION exam_publications_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('app.exam_history_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'exam publications are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' OR NOT EXISTS (
            SELECT 1 FROM exams WHERE id = NEW.exam_id AND status = 'marks_entry'
        ) THEN
            RAISE EXCEPTION 'results are published from marks entry, as an active publication'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_publications_lifecycle';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'active' OR NEW.status <> 'withdrawn' OR (
        NEW.school_id, NEW.exam_id, NEW.version, NEW.settings, NEW.published_by_user_id,
        NEW.published_at
    ) IS DISTINCT FROM (
        OLD.school_id, OLD.exam_id, OLD.version, OLD.settings, OLD.published_by_user_id,
        OLD.published_at
    ) THEN
        RAISE EXCEPTION 'a publication can only be withdrawn'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'exam_publications_lifecycle';
    END IF;
    RETURN NEW;
END;
$$
"""

TRIGGERS = (
    ("trg_exams_lifecycle", "BEFORE INSERT OR UPDATE", "exams", "exams_lifecycle_guard"),
    (
        "trg_exam_subjects_config",
        "BEFORE INSERT OR UPDATE OR DELETE",
        "exam_subjects",
        "exam_subjects_config_guard",
    ),
    ("trg_exam_marks_guard", "BEFORE INSERT OR UPDATE OR DELETE", "exam_marks", "exam_marks_guard"),
    (
        "trg_exam_mark_changes_append_only",
        "BEFORE UPDATE OR DELETE",
        "exam_mark_changes",
        "exam_history_append_only",
    ),
    (
        "trg_exam_report_cards_append_only",
        "BEFORE UPDATE OR DELETE",
        "exam_report_cards",
        "exam_history_append_only",
    ),
    (
        "trg_exam_report_cards_insert",
        "BEFORE INSERT",
        "exam_report_cards",
        "exam_report_cards_insert_guard",
    ),
    (
        "trg_exam_publications_guard",
        "BEFORE INSERT OR UPDATE OR DELETE",
        "exam_publications",
        "exam_publications_guard",
    ),
)
FUNCTIONS = (
    ("exams_lifecycle_guard", EXAMS_GUARD),
    ("exam_subjects_config_guard", EXAM_SUBJECTS_GUARD),
    ("exam_marks_guard", EXAM_MARKS_GUARD),
    ("exam_history_append_only", HISTORY_GUARD),
    ("exam_report_cards_insert_guard", REPORT_CARDS_INSERT_GUARD),
    ("exam_publications_guard", PUBLICATIONS_GUARD),
)


def upgrade() -> None:
    # --------------------------------------------------------------------- exams
    op.create_table(
        "exams",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("exam_type", sa.String(length=20), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("pass_percentage", sa.Numeric(5, 2), nullable=True),
        sa.Column("grade_bands", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("cancelled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(f"status IN ({EXAM_STATUSES})", name=op.f("ck_exams_status_valid")),
        sa.CheckConstraint(f"exam_type IN ({EXAM_TYPES})", name=op.f("ck_exams_exam_type_valid")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_exams_name_not_blank")),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_exams_dates_ordered")),
        sa.CheckConstraint(
            "pass_percentage IS NULL OR pass_percentage BETWEEN 0 AND 100",
            name=op.f("ck_exams_pass_percentage_range"),
        ),
        sa.CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL) "
            "AND (cancelled_at IS NULL) = (cancelled_by_user_id IS NULL) "
            "AND (cancelled_at IS NULL) = (cancellation_reason IS NULL)",
            name=op.f("ck_exams_cancellation_complete"),
        ),
        sa.CheckConstraint(
            "cancellation_reason IS NULL OR length(trim(cancellation_reason)) > 0",
            name=op.f("ck_exams_cancellation_reason_not_blank"),
        ),
        _school("exams"),
        _user("exams", "created_by_user_id"),
        _user("exams", "cancelled_by_user_id"),
        _fk(
            ["academic_year_id", "school_id"],
            "academic_years",
            ["id", "school_id"],
            "fk_exams_year_same_school",
        ),
        _fk(
            ["class_id", "school_id"], "classes", ["id", "school_id"], "fk_exams_class_same_school"
        ),
        _fk(
            ["section_id", "school_id"],
            "sections",
            ["id", "school_id"],
            "fk_exams_section_same_school",
        ),
        _fk(
            ["section_id", "class_id"], "sections", ["id", "class_id"], "fk_exams_section_in_class"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exams"),
        sa.UniqueConstraint("id", "school_id", name="uq_exams_id_school_id"),
        sa.UniqueConstraint(
            "id", "school_id", "academic_year_id", "class_id", name="uq_exams_placement_key"
        ),
    )
    _school_index("exams")
    op.create_index("ix_exams_placement", "exams", ["school_id", "academic_year_id", "class_id"])
    op.create_index(
        "uq_exams_name_per_placement",
        "exams",
        ["academic_year_id", "class_id", "section_id", "name"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )

    # ------------------------------------------------------------- exam subjects
    op.create_table(
        "exam_subjects",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("max_marks", MARKS, nullable=False),
        sa.Column("pass_marks", MARKS, nullable=True),
        sa.Column("assessment_date", sa.Date(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "max_marks > 0 AND max_marks <= 1000", name=op.f("ck_exam_subjects_max_marks_range")
        ),
        sa.CheckConstraint(
            "pass_marks IS NULL OR (pass_marks >= 0 AND pass_marks <= max_marks)",
            name=op.f("ck_exam_subjects_pass_marks_range"),
        ),
        sa.CheckConstraint(
            "start_time IS NULL OR end_time IS NULL OR start_time < end_time",
            name=op.f("ck_exam_subjects_times_ordered"),
        ),
        _school("exam_subjects"),
        _user("exam_subjects", "created_by_user_id"),
        _fk(
            ["exam_id", "school_id"],
            "exams",
            ["id", "school_id"],
            "fk_exam_subjects_exam_same_school",
        ),
        _fk(
            ["subject_id", "school_id"],
            "subjects",
            ["id", "school_id"],
            "fk_exam_subjects_subject_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exam_subjects"),
        sa.UniqueConstraint("exam_id", "subject_id", name="uq_exam_subjects_exam_id_subject_id"),
        sa.UniqueConstraint(
            "id", "exam_id", "school_id", "max_marks", name="uq_exam_subjects_marks_key"
        ),
    )
    _school_index("exam_subjects")

    # ---------------------------------------------------------------------- marks
    op.create_table(
        "exam_marks",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("exam_subject_id", sa.Uuid(), nullable=False),
        sa.Column("max_marks", MARKS, nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("marks_obtained", MARKS, nullable=True),
        sa.Column("remarks", sa.String(length=500), nullable=True),
        sa.Column("entered_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corrected_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correction_reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(f"status IN ({MARK_STATUSES})", name=op.f("ck_exam_marks_status_valid")),
        sa.CheckConstraint(
            "(status = 'present') = (marks_obtained IS NOT NULL)",
            name=op.f("ck_exam_marks_marks_match_status"),
        ),
        sa.CheckConstraint(
            "marks_obtained IS NULL OR (marks_obtained >= 0 AND marks_obtained <= max_marks)",
            name=op.f("ck_exam_marks_marks_range"),
        ),
        sa.CheckConstraint(
            "(corrected_at IS NULL) = (corrected_by_user_id IS NULL) "
            "AND (corrected_at IS NULL) = (correction_reason IS NULL)",
            name=op.f("ck_exam_marks_correction_complete"),
        ),
        sa.CheckConstraint(
            "correction_reason IS NULL OR length(trim(correction_reason)) > 0",
            name=op.f("ck_exam_marks_correction_reason_not_blank"),
        ),
        _school("exam_marks"),
        _user("exam_marks", "entered_by_user_id"),
        _user("exam_marks", "corrected_by_user_id"),
        _fk(
            ["exam_subject_id", "exam_id", "school_id", "max_marks"],
            "exam_subjects",
            ["id", "exam_id", "school_id", "max_marks"],
            "fk_exam_marks_component",
        ),
        _fk(EXAM_PLACEMENT, "exams", EXAM_TARGET, "fk_exam_marks_exam_placement"),
        _fk(
            ENROLLMENT_PLACEMENT,
            "student_enrollments",
            ENROLLMENT_TARGET,
            "fk_exam_marks_enrollment_placement",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exam_marks"),
        sa.UniqueConstraint(
            "exam_subject_id", "student_id", name="uq_exam_marks_exam_subject_id_student_id"
        ),
        sa.UniqueConstraint("id", "school_id", name="uq_exam_marks_id_school_id"),
    )
    _school_index("exam_marks")
    op.create_index("ix_exam_marks_student_id", "exam_marks", ["student_id"])
    op.create_index("ix_exam_marks_exam_id", "exam_marks", ["exam_id"])

    # --------------------------------------------------------------- mark history
    op.create_table(
        "exam_mark_changes",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("mark_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_status", sa.String(length=10), nullable=False),
        sa.Column("new_status", sa.String(length=10), nullable=False),
        sa.Column("previous_marks", MARKS, nullable=True),
        sa.Column("new_marks", MARKS, nullable=True),
        sa.Column("previous_remarks", sa.String(length=500), nullable=True),
        sa.Column("new_remarks", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"previous_status IN ({MARK_STATUSES})",
            name=op.f("ck_exam_mark_changes_previous_status_valid"),
        ),
        sa.CheckConstraint(
            f"new_status IN ({MARK_STATUSES})", name=op.f("ck_exam_mark_changes_new_status_valid")
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_exam_mark_changes_reason_not_blank")
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_exam_mark_changes_sequence_positive")),
        _school("exam_mark_changes"),
        _user("exam_mark_changes", "changed_by_user_id"),
        _fk(
            ["mark_id", "school_id"],
            "exam_marks",
            ["id", "school_id"],
            "fk_exam_mark_changes_mark_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exam_mark_changes"),
        sa.UniqueConstraint("mark_id", "sequence", name="uq_exam_mark_changes_mark_id_sequence"),
    )
    _school_index("exam_mark_changes")

    # --------------------------------------------------------------- publications
    op.create_table(
        "exam_publications",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawal_reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'withdrawn')", name=op.f("ck_exam_publications_status_valid")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_exam_publications_version_positive")),
        sa.CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL) "
            "AND (withdrawn_at IS NULL) = (withdrawn_by_user_id IS NULL) "
            "AND (withdrawn_at IS NULL) = (withdrawal_reason IS NULL)",
            name=op.f("ck_exam_publications_withdrawal_complete"),
        ),
        sa.CheckConstraint(
            "withdrawal_reason IS NULL OR length(trim(withdrawal_reason)) > 0",
            name=op.f("ck_exam_publications_withdrawal_reason_not_blank"),
        ),
        _school("exam_publications"),
        _user("exam_publications", "published_by_user_id"),
        _user("exam_publications", "withdrawn_by_user_id"),
        _fk(
            ["exam_id", "school_id"],
            "exams",
            ["id", "school_id"],
            "fk_exam_publications_exam_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exam_publications"),
        sa.UniqueConstraint("exam_id", "version", name="uq_exam_publications_exam_id_version"),
        sa.UniqueConstraint("id", "exam_id", "school_id", name="uq_exam_publications_key"),
    )
    _school_index("exam_publications")
    op.create_index(
        "uq_exam_publications_one_active",
        "exam_publications",
        ["exam_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --------------------------------------------------------------- report cards
    op.create_table(
        "exam_report_cards",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("exam_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("total_obtained", sa.Numeric(8, 2), nullable=False),
        sa.Column("total_max", sa.Numeric(8, 2), nullable=False),
        sa.Column("percentage", sa.Numeric(5, 2), nullable=True),
        sa.Column("grade", sa.String(length=10), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("subjects", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"result IN ({RESULTS})", name=op.f("ck_exam_report_cards_result_valid")
        ),
        sa.CheckConstraint(
            "total_obtained >= 0 AND total_obtained <= total_max",
            name=op.f("ck_exam_report_cards_totals_range"),
        ),
        sa.CheckConstraint(
            "percentage IS NULL OR percentage BETWEEN 0 AND 100",
            name=op.f("ck_exam_report_cards_percentage_range"),
        ),
        _school("exam_report_cards"),
        _fk(
            ["publication_id", "exam_id", "school_id"],
            "exam_publications",
            ["id", "exam_id", "school_id"],
            "fk_exam_report_cards_publication",
        ),
        _fk(EXAM_PLACEMENT, "exams", EXAM_TARGET, "fk_exam_report_cards_exam_placement"),
        _fk(
            ENROLLMENT_PLACEMENT,
            "student_enrollments",
            ENROLLMENT_TARGET,
            "fk_exam_report_cards_enrollment_placement",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_exam_report_cards"),
        sa.UniqueConstraint(
            "publication_id", "student_id", name="uq_exam_report_cards_publication_id_student_id"
        ),
    )
    _school_index("exam_report_cards")
    op.create_index("ix_exam_report_cards_student_id", "exam_report_cards", ["student_id"])

    # ------------------------------------------------------------------ triggers
    for _, body in FUNCTIONS:
        op.execute(body)
    for trigger, timing, table, function in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger} {timing} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )


def downgrade() -> None:
    for trigger, _, table, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for function, _ in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}()")
    op.drop_index("ix_exam_report_cards_student_id", table_name="exam_report_cards")
    op.drop_index("ix_exam_report_cards_school_id", table_name="exam_report_cards")
    op.drop_table("exam_report_cards")
    op.drop_index(
        "uq_exam_publications_one_active",
        table_name="exam_publications",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index("ix_exam_publications_school_id", table_name="exam_publications")
    op.drop_table("exam_publications")
    op.drop_index("ix_exam_mark_changes_school_id", table_name="exam_mark_changes")
    op.drop_table("exam_mark_changes")
    op.drop_index("ix_exam_marks_exam_id", table_name="exam_marks")
    op.drop_index("ix_exam_marks_student_id", table_name="exam_marks")
    op.drop_index("ix_exam_marks_school_id", table_name="exam_marks")
    op.drop_table("exam_marks")
    op.drop_index("ix_exam_subjects_school_id", table_name="exam_subjects")
    op.drop_table("exam_subjects")
    op.drop_index(
        "uq_exams_name_per_placement",
        table_name="exams",
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.drop_index("ix_exams_placement", table_name="exams")
    op.drop_index("ix_exams_school_id", table_name="exams")
    op.drop_table("exams")
