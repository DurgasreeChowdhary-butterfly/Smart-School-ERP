"""assignments, submissions, submission revisions and review history

* Creates ``assignments`` (school + year + class + section + subject; draft -> published
  -> closed), ``assignment_submissions`` (one per student per assignment, pinned to the
  student's enrollment, holding the review), ``assignment_submission_revisions``
  (append-only submitted content, with the due time snapshot that fixes lateness) and
  ``assignment_review_changes`` (append-only feedback/grade history).
* Every reference is a composite foreign key carrying ``school_id``. A submission
  repeats its assignment's placement and an enrollment's, and carries the assignment's
  ``max_marks`` (so ``marks_awarded <= max_marks`` is a CHECK).
* Triggers enforce what a foreign key cannot: the lifecycle (created as draft; draft ->
  published -> closed; closed is read-only), immutable placement, grading settings
  frozen after publication, revisions only while the assignment is published, honest
  due-time snapshots, late work only when allowed, and append-only history. DELETE of
  submissions and history is refused unless the transaction sets
  ``app.assignment_history_purge = 'on'`` (maintenance, e.g. removing test data; a guard
  against accidents, not against the database owner).
* Adds the permission ``assignment.submit`` (students hand in their own work) and grants
  it to STUDENT only. No existing table, column or row changes otherwise.

Revision ID: 0008_assignments_submissions
Revises: 0007_examinations_marks
Create Date: 2026-10-10 09:00:00.000000+00:00
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_assignments_submissions"
down_revision: str | None = "0007_examinations_marks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same deterministic ids as migration 0002 (uuid5 over a fixed namespace).
_ID_NAMESPACE = uuid.UUID("6f1c2a52-0b7e-4f43-9d6c-2f0d8a1e5b11")
SUBMIT = "assignment.submit"
SUBMIT_ID = uuid.uuid5(_ID_NAMESPACE, f"permission:{SUBMIT}")
STUDENT_ROLE_ID = uuid.uuid5(_ID_NAMESPACE, "role:STUDENT")

MARKS = sa.Numeric(6, 2)
PLACEMENT = ["school_id", "academic_year_id", "class_id", "section_id"]


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


ASSIGNMENTS_GUARD = """
CREATE FUNCTION assignments_lifecycle_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'assignments are created as draft'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'assignments_lifecycle';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.school_id, NEW.academic_year_id, NEW.class_id, NEW.section_id, NEW.subject_id)
       IS DISTINCT FROM
       (OLD.school_id, OLD.academic_year_id, OLD.class_id, OLD.section_id, OLD.subject_id) THEN
        RAISE EXCEPTION 'assignment placement is immutable'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'assignments_placement_immutable';
    END IF;
    IF OLD.status = 'closed' THEN
        RAISE EXCEPTION 'closed assignments are read-only'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'assignments_lifecycle';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
        ('draft', 'published'), ('published', 'closed')
    ) THEN
        RAISE EXCEPTION 'invalid assignment status transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation', CONSTRAINT = 'assignments_lifecycle';
    END IF;
    IF OLD.status <> 'draft'
       AND (NEW.max_marks, NEW.assigned_on) IS DISTINCT FROM (OLD.max_marks, OLD.assigned_on) THEN
        RAISE EXCEPTION 'grading settings and the assignment date are fixed once published'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'assignments_settings_frozen';
    END IF;
    RETURN NEW;
END;
$$
"""

SUBMISSIONS_GUARD = """
CREATE FUNCTION assignment_submissions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    assignment_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('app.assignment_history_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'assignment submissions are never deleted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    SELECT status INTO assignment_status FROM assignments WHERE id = NEW.assignment_id;
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.school_id, NEW.assignment_id, NEW.student_id, NEW.enrollment_id,
            NEW.academic_year_id, NEW.class_id, NEW.section_id, NEW.max_marks,
            NEW.first_submitted_at)
           IS DISTINCT FROM
           (OLD.school_id, OLD.assignment_id, OLD.student_id, OLD.enrollment_id,
            OLD.academic_year_id, OLD.class_id, OLD.section_id, OLD.max_marks,
            OLD.first_submitted_at) THEN
            RAISE EXCEPTION 'a submission''s student, enrollment and assignment are immutable'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_submissions_immutable';
        END IF;
        IF NEW.revision_count < OLD.revision_count THEN
            RAISE EXCEPTION 'revisions are never removed'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_submissions_immutable';
        END IF;
        IF NEW.revision_count = OLD.revision_count
           AND (NEW.last_submitted_at, NEW.is_late) IS NOT DISTINCT FROM
               (OLD.last_submitted_at, OLD.is_late) THEN
            RETURN NEW;  -- a review change; allowed while published or closed
        END IF;
    END IF;
    IF assignment_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'work can only be submitted while the assignment is published'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'assignment_submissions_assignment_open';
    END IF;
    RETURN NEW;
END;
$$
"""

REVISIONS_GUARD = """
CREATE FUNCTION assignment_revisions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    a record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        IF TG_OP = 'DELETE'
           AND current_setting('app.assignment_history_purge', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'assignment_submission_revisions is append-only'
            USING ERRCODE = 'restrict_violation';
    END IF;
    SELECT x.status, x.due_at, x.allow_late INTO a
        FROM assignments x JOIN assignment_submissions s ON s.assignment_id = x.id
        WHERE s.id = NEW.submission_id;
    IF a.status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'work can only be submitted while the assignment is published'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'assignment_submissions_assignment_open';
    END IF;
    IF NEW.due_at IS DISTINCT FROM a.due_at THEN
        RAISE EXCEPTION 'the due time snapshot must equal the assignment''s due time'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_revisions_due_snapshot';
    END IF;
    IF NEW.submitted_at > NEW.due_at AND NOT a.allow_late THEN
        RAISE EXCEPTION 'late submissions are not accepted for this assignment'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'assignment_revisions_late_refused';
    END IF;
    RETURN NEW;
END;
$$
"""

HISTORY_GUARD = """
CREATE FUNCTION assignment_history_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('app.assignment_history_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

FUNCTIONS = (
    ("assignments_lifecycle_guard", ASSIGNMENTS_GUARD),
    ("assignment_submissions_guard", SUBMISSIONS_GUARD),
    ("assignment_revisions_guard", REVISIONS_GUARD),
    ("assignment_history_append_only", HISTORY_GUARD),
)
TRIGGERS = (
    (
        "trg_assignments_lifecycle",
        "BEFORE INSERT OR UPDATE",
        "assignments",
        "assignments_lifecycle_guard",
    ),
    (
        "trg_assignment_submissions_guard",
        "BEFORE INSERT OR UPDATE OR DELETE",
        "assignment_submissions",
        "assignment_submissions_guard",
    ),
    (
        "trg_assignment_revisions_guard",
        "BEFORE INSERT OR UPDATE OR DELETE",
        "assignment_submission_revisions",
        "assignment_revisions_guard",
    ),
    (
        "trg_assignment_review_changes_append_only",
        "BEFORE UPDATE OR DELETE",
        "assignment_review_changes",
        "assignment_history_append_only",
    ),
)


def upgrade() -> None:
    # ---------------------------------------------------------------- assignments
    op.create_table(
        "assignments",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("assigned_on", sa.Date(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allow_late", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("allow_resubmission", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("max_marks", MARKS, nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'closed')", name=op.f("ck_assignments_status_valid")
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name=op.f("ck_assignments_title_not_blank")),
        sa.CheckConstraint(
            "length(trim(instructions)) > 0", name=op.f("ck_assignments_instructions_not_blank")
        ),
        sa.CheckConstraint(
            "max_marks IS NULL OR (max_marks > 0 AND max_marks <= 1000)",
            name=op.f("ck_assignments_max_marks_range"),
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (published_at IS NULL) "
            "AND (published_at IS NULL) = (published_by_user_id IS NULL)",
            name=op.f("ck_assignments_publication_complete"),
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL) "
            "AND (closed_at IS NULL) = (closed_by_user_id IS NULL)",
            name=op.f("ck_assignments_closure_complete"),
        ),
        _school("assignments"),
        _user("assignments", "created_by_user_id"),
        _user("assignments", "published_by_user_id"),
        _user("assignments", "closed_by_user_id"),
        _fk(
            ["academic_year_id", "school_id"],
            "academic_years",
            ["id", "school_id"],
            "fk_assignments_year_same_school",
        ),
        _fk(
            ["class_id", "school_id"],
            "classes",
            ["id", "school_id"],
            "fk_assignments_class_same_school",
        ),
        _fk(
            ["section_id", "school_id"],
            "sections",
            ["id", "school_id"],
            "fk_assignments_section_same_school",
        ),
        _fk(
            ["section_id", "class_id"],
            "sections",
            ["id", "class_id"],
            "fk_assignments_section_in_class",
        ),
        _fk(
            ["subject_id", "school_id"],
            "subjects",
            ["id", "school_id"],
            "fk_assignments_subject_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignments"),
        sa.UniqueConstraint("id", *PLACEMENT, name="uq_assignments_placement_key"),
        sa.UniqueConstraint("id", "school_id", "max_marks", name="uq_assignments_marks_key"),
    )
    op.create_index("ix_assignments_school_id", "assignments", ["school_id"])
    op.create_index(
        "ix_assignments_placement",
        "assignments",
        ["school_id", "academic_year_id", "section_id", "subject_id"],
    )

    # --------------------------------------------------------------- submissions
    op.create_table(
        "assignment_submissions",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("max_marks", MARKS, nullable=True),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("first_submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_late", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            sa.Computed(
                "CASE WHEN reviewed_at IS NULL THEN 'submitted' ELSE 'reviewed' END",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("marks_awarded", MARKS, nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("reviewed_revision", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "revision_count >= 1", name=op.f("ck_assignment_submissions_revision_count_positive")
        ),
        sa.CheckConstraint(
            "marks_awarded IS NULL OR (max_marks IS NOT NULL AND marks_awarded >= 0 "
            "AND marks_awarded <= max_marks)",
            name=op.f("ck_assignment_submissions_marks_range"),
        ),
        sa.CheckConstraint(
            "(reviewed_at IS NULL) = (reviewed_by_user_id IS NULL) "
            "AND (reviewed_at IS NULL) = (reviewed_revision IS NULL)",
            name=op.f("ck_assignment_submissions_review_complete"),
        ),
        sa.CheckConstraint(
            "reviewed_at IS NOT NULL OR (marks_awarded IS NULL AND feedback IS NULL)",
            name=op.f("ck_assignment_submissions_review_fields_need_review"),
        ),
        sa.CheckConstraint(
            "reviewed_revision IS NULL OR reviewed_revision BETWEEN 1 AND revision_count",
            name=op.f("ck_assignment_submissions_reviewed_revision_exists"),
        ),
        sa.CheckConstraint(
            "feedback IS NULL OR length(trim(feedback)) > 0",
            name=op.f("ck_assignment_submissions_feedback_not_blank"),
        ),
        _school("assignment_submissions"),
        _user("assignment_submissions", "reviewed_by_user_id"),
        _fk(
            ["assignment_id", *PLACEMENT],
            "assignments",
            ["id", *PLACEMENT],
            "fk_assignment_submissions_assignment_placement",
        ),
        _fk(
            ["assignment_id", "school_id", "max_marks"],
            "assignments",
            ["id", "school_id", "max_marks"],
            "fk_assignment_submissions_max_marks",
        ),
        _fk(
            [
                "enrollment_id",
                "school_id",
                "student_id",
                "academic_year_id",
                "class_id",
                "section_id",
            ],
            "student_enrollments",
            ["id", "school_id", "student_id", "academic_year_id", "class_id", "section_id"],
            "fk_assignment_submissions_enrollment_placement",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_submissions"),
        sa.UniqueConstraint(
            "assignment_id",
            "student_id",
            name="uq_assignment_submissions_assignment_id_student_id",
        ),
        sa.UniqueConstraint("id", "school_id", name="uq_assignment_submissions_id_school_id"),
    )
    op.create_index("ix_assignment_submissions_school_id", "assignment_submissions", ["school_id"])
    op.create_index(
        "ix_assignment_submissions_student_id", "assignment_submissions", ["student_id"]
    )

    # ----------------------------------------------------------------- revisions
    op.create_table(
        "assignment_submission_revisions",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("submitted_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_late",
            sa.Boolean(),
            sa.Computed("submitted_at > due_at", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_assignment_submission_revisions_revision_positive")
        ),
        sa.CheckConstraint(
            "length(trim(content)) > 0 AND length(content) <= 20000",
            name=op.f("ck_assignment_submission_revisions_content_valid"),
        ),
        _school("assignment_submission_revisions"),
        _user("assignment_submission_revisions", "submitted_by_user_id"),
        _fk(
            ["submission_id", "school_id"],
            "assignment_submissions",
            ["id", "school_id"],
            "fk_assignment_submission_revisions_submission_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_submission_revisions"),
        sa.UniqueConstraint(
            "submission_id",
            "revision",
            name="uq_assignment_submission_revisions_submission_id_revision",
        ),
    )
    op.create_index(
        "ix_assignment_submission_revisions_school_id",
        "assignment_submission_revisions",
        ["school_id"],
    )

    # ------------------------------------------------------------ review history
    op.create_table(
        "assignment_review_changes",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("previous_marks", MARKS, nullable=True),
        sa.Column("new_marks", MARKS, nullable=True),
        sa.Column("previous_feedback", sa.Text(), nullable=True),
        sa.Column("new_feedback", sa.Text(), nullable=True),
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
            "sequence >= 1", name=op.f("ck_assignment_review_changes_sequence_positive")
        ),
        sa.CheckConstraint(
            "sequence = 1 OR reason IS NOT NULL",
            name=op.f("ck_assignment_review_changes_reason_required_for_change"),
        ),
        sa.CheckConstraint(
            "reason IS NULL OR length(trim(reason)) > 0",
            name=op.f("ck_assignment_review_changes_reason_not_blank"),
        ),
        _school("assignment_review_changes"),
        _user("assignment_review_changes", "changed_by_user_id"),
        _fk(
            ["submission_id", "school_id"],
            "assignment_submissions",
            ["id", "school_id"],
            "fk_assignment_review_changes_submission_same_school",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_review_changes"),
        sa.UniqueConstraint(
            "submission_id",
            "sequence",
            name="uq_assignment_review_changes_submission_id_sequence",
        ),
    )
    op.create_index(
        "ix_assignment_review_changes_school_id", "assignment_review_changes", ["school_id"]
    )

    # ------------------------------------------------------------------ triggers
    for _, body in FUNCTIONS:
        op.execute(body)
    for trigger, timing, table, function in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger} {timing} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )

    # ------------------------------------------------------------- permission
    op.execute(
        sa.text(
            "INSERT INTO permissions (id, name, description) VALUES (:id, :name, :description)"
        ).bindparams(id=SUBMIT_ID, name=SUBMIT, description="Submit own work for assignments.")
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) VALUES (:role, :permission)"
        ).bindparams(role=STUDENT_ROLE_ID, permission=SUBMIT_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_id = :p").bindparams(p=SUBMIT_ID)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE id = :p").bindparams(p=SUBMIT_ID))
    for trigger, _, table, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for function, _ in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {function}()")
    op.drop_index("ix_assignment_review_changes_school_id", table_name="assignment_review_changes")
    op.drop_table("assignment_review_changes")
    op.drop_index(
        "ix_assignment_submission_revisions_school_id",
        table_name="assignment_submission_revisions",
    )
    op.drop_table("assignment_submission_revisions")
    op.drop_index("ix_assignment_submissions_student_id", table_name="assignment_submissions")
    op.drop_index("ix_assignment_submissions_school_id", table_name="assignment_submissions")
    op.drop_table("assignment_submissions")
    op.drop_index("ix_assignments_placement", table_name="assignments")
    op.drop_index("ix_assignments_school_id", table_name="assignments")
    op.drop_table("assignments")
