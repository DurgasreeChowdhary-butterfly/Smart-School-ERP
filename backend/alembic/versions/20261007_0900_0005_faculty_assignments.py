"""faculty and faculty assignments; composite same-school keys

* Creates ``faculty`` (a domain record bound to a FACULTY user of the same school)
  and ``faculty_assignments`` (faculty + academic year + class + section + subject).
* Every reference is a composite foreign key that carries ``school_id``, so an
  assignment can never mix schools, and ``(section_id, class_id)`` pins the
  section to the assignment's class. The composite targets on existing tables
  (``uq_*_id_school_id``, ``uq_sections_id_class_id``) were added by 0004, so no
  existing table is modified.
* Purely additive: no rows are created, changed or removed.

Revision ID: 0005_faculty_assignments
Revises: 0004_students_parents
Create Date: 2026-10-07 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005_faculty_assignments"
down_revision: str | None = "0004_students_parents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    op.create_table(
        "faculty",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.String(length=30), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("middle_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("designation", sa.String(length=100), nullable=True),
        sa.Column("department", sa.String(length=100), nullable=True),
        sa.Column("joining_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            sa.Computed("status = 'active'", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "employee_id = upper(employee_id)", name=op.f("ck_faculty_employee_id_upper")
        ),
        sa.CheckConstraint(
            "length(trim(employee_id)) > 0", name=op.f("ck_faculty_employee_id_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(first_name)) > 0", name=op.f("ck_faculty_first_name_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(last_name)) > 0", name=op.f("ck_faculty_last_name_not_blank")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name=op.f("ck_faculty_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_faculty_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "school_id"],
            ["users.id", "users.school_id"],
            name="fk_faculty_user_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faculty")),
        sa.UniqueConstraint("id", "school_id", name="uq_faculty_id_school_id"),
        sa.UniqueConstraint(
            "school_id", "employee_id", name=op.f("uq_faculty_school_id_employee_id")
        ),
        sa.UniqueConstraint("user_id", name=op.f("uq_faculty_user_id")),
    )
    op.create_index(op.f("ix_faculty_school_id"), "faculty", ["school_id"], unique=False)

    op.create_table(
        "faculty_assignments",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("faculty_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            sa.Computed("status = 'active'", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name=op.f("ck_faculty_assignments_status_valid")
        ),
        sa.CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to",
            name=op.f("ck_faculty_assignments_effective_dates_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_faculty_assignments_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["faculty_id", "school_id"],
            ["faculty.id", "faculty.school_id"],
            name="fk_faculty_assignments_faculty_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["academic_year_id", "school_id"],
            ["academic_years.id", "academic_years.school_id"],
            name="fk_faculty_assignments_year_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_faculty_assignments_class_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "school_id"],
            ["sections.id", "sections.school_id"],
            name="fk_faculty_assignments_section_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "class_id"],
            ["sections.id", "sections.class_id"],
            name="fk_faculty_assignments_section_in_class",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id", "school_id"],
            ["subjects.id", "subjects.school_id"],
            name="fk_faculty_assignments_subject_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faculty_assignments")),
    )
    op.create_index(
        op.f("ix_faculty_assignments_school_id"), "faculty_assignments", ["school_id"]
    )
    op.create_index(
        op.f("ix_faculty_assignments_faculty_id"), "faculty_assignments", ["faculty_id"]
    )
    op.create_index(
        "ix_faculty_assignments_placement",
        "faculty_assignments",
        ["school_id", "academic_year_id", "class_id", "section_id"],
    )
    op.create_index(
        "uq_faculty_assignments_one_active",
        "faculty_assignments",
        ["faculty_id", "academic_year_id", "class_id", "section_id", "subject_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_faculty_assignments_one_active",
        table_name="faculty_assignments",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index("ix_faculty_assignments_placement", table_name="faculty_assignments")
    op.drop_index(op.f("ix_faculty_assignments_faculty_id"), table_name="faculty_assignments")
    op.drop_index(op.f("ix_faculty_assignments_school_id"), table_name="faculty_assignments")
    op.drop_table("faculty_assignments")
    op.drop_index(op.f("ix_faculty_school_id"), table_name="faculty")
    op.drop_table("faculty")
