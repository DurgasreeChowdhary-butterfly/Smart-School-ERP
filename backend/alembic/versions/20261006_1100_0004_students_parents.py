"""students, parents, student-parent links and enrollments; composite same-school keys

* Adds (id, school_id) uniqueness to academic_years, sections, subjects and users,
  and (id, class_id) to sections, so the new tables can reference them with
  composite foreign keys that make cross-school links impossible in the database.
* Creates students, parents, student_parents and student_enrollments.
* Purely additive for existing data: no rows are created, changed or removed.

Revision ID: 0004_students_parents
Revises: 0003_school_academic
Create Date: 2026-10-06 10:50:28.073904+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_students_parents"
down_revision: str | None = "0003_school_academic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Composite-FK targets on existing tables must exist before the new tables reference them.
    op.create_unique_constraint(
        "uq_academic_years_id_school_id", "academic_years", ["id", "school_id"]
    )
    op.create_unique_constraint("uq_sections_id_class_id", "sections", ["id", "class_id"])
    op.create_unique_constraint("uq_sections_id_school_id", "sections", ["id", "school_id"])
    op.create_unique_constraint("uq_subjects_id_school_id", "subjects", ["id", "school_id"])
    op.create_unique_constraint("uq_users_id_school_id", "users", ["id", "school_id"])
    op.create_table(
        "parents",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("occupation", sa.String(length=100), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "length(trim(first_name)) > 0", name=op.f("ck_parents_first_name_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(last_name)) > 0", name=op.f("ck_parents_last_name_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_parents_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "school_id"],
            ["users.id", "users.school_id"],
            name="fk_parents_user_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parents")),
        sa.UniqueConstraint("id", "school_id", name="uq_parents_id_school_id"),
        sa.UniqueConstraint("user_id", name=op.f("uq_parents_user_id")),
    )
    op.create_index(op.f("ix_parents_school_id"), "parents", ["school_id"], unique=False)
    op.create_table(
        "students",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("admission_number", sa.String(length=30), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("middle_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(length=10), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("admission_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "gender IN ('female', 'male', 'other')", name=op.f("ck_students_gender_valid")
        ),
        sa.CheckConstraint("phone ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f("ck_students_phone_e164")),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'transferred', 'graduated')",
            name=op.f("ck_students_status_valid"),
        ),
        sa.CheckConstraint(
            "admission_number = upper(admission_number)",
            name=op.f("ck_students_admission_number_upper"),
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_students_email_lowercase")),
        sa.CheckConstraint(
            "length(trim(first_name)) > 0", name=op.f("ck_students_first_name_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(last_name)) > 0", name=op.f("ck_students_last_name_not_blank")
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_students_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "school_id"],
            ["users.id", "users.school_id"],
            name="fk_students_user_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_students")),
        sa.UniqueConstraint("id", "school_id", name="uq_students_id_school_id"),
        sa.UniqueConstraint(
            "school_id", "admission_number", name=op.f("uq_students_school_id_admission_number")
        ),
        sa.UniqueConstraint("user_id", name=op.f("uq_students_user_id")),
    )
    op.create_index(op.f("ix_students_school_id"), "students", ["school_id"], unique=False)
    op.create_table(
        "student_enrollments",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_number", sa.String(length=30), nullable=True),
        sa.Column("enrollment_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "NOT is_current OR status = 'active'",
            name=op.f("ck_student_enrollments_current_is_active"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'withdrawn', 'superseded')",
            name=op.f("ck_student_enrollments_status_valid"),
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= enrollment_date",
            name=op.f("ck_student_enrollments_dates_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["academic_year_id", "school_id"],
            ["academic_years.id", "academic_years.school_id"],
            name="fk_enrollments_year_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_enrollments_class_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_student_enrollments_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "class_id"],
            ["sections.id", "sections.class_id"],
            name="fk_enrollments_section_in_class",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id", "school_id"],
            ["sections.id", "sections.school_id"],
            name="fk_enrollments_section_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["student_id", "school_id"],
            ["students.id", "students.school_id"],
            name="fk_enrollments_student_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_student_enrollments")),
    )
    op.create_index(
        "ix_student_enrollments_placement",
        "student_enrollments",
        ["school_id", "academic_year_id", "class_id", "section_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_student_enrollments_school_id"), "student_enrollments", ["school_id"], unique=False
    )
    op.create_index(
        op.f("ix_student_enrollments_student_id"),
        "student_enrollments",
        ["student_id"],
        unique=False,
    )
    op.create_index(
        "uq_student_enrollments_one_active_per_year",
        "student_enrollments",
        ["student_id", "academic_year_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_student_enrollments_one_current",
        "student_enrollments",
        ["student_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "student_parents",
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=20), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_guardian", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint(
            "relationship_type IN ('mother', 'father', 'guardian', 'other')",
            name=op.f("ck_student_parents_relationship_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "school_id"],
            ["parents.id", "parents.school_id"],
            name="fk_student_parents_parent_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_student_parents_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["student_id", "school_id"],
            ["students.id", "students.school_id"],
            name="fk_student_parents_student_same_school",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("student_id", "parent_id", name=op.f("pk_student_parents")),
    )
    op.create_index(
        op.f("ix_student_parents_parent_id"), "student_parents", ["parent_id"], unique=False
    )
    op.create_index(
        op.f("ix_student_parents_school_id"), "student_parents", ["school_id"], unique=False
    )
    op.create_index(
        "uq_student_parents_one_primary",
        "student_parents",
        ["student_id"],
        unique=True,
        postgresql_where=sa.text("is_primary AND is_active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_student_parents_one_primary",
        table_name="student_parents",
        postgresql_where=sa.text("is_primary AND is_active"),
    )
    op.drop_index(op.f("ix_student_parents_school_id"), table_name="student_parents")
    op.drop_index(op.f("ix_student_parents_parent_id"), table_name="student_parents")
    op.drop_table("student_parents")
    op.drop_index(
        "uq_student_enrollments_one_current",
        table_name="student_enrollments",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_index(
        "uq_student_enrollments_one_active_per_year",
        table_name="student_enrollments",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(op.f("ix_student_enrollments_student_id"), table_name="student_enrollments")
    op.drop_index(op.f("ix_student_enrollments_school_id"), table_name="student_enrollments")
    op.drop_index("ix_student_enrollments_placement", table_name="student_enrollments")
    op.drop_table("student_enrollments")
    op.drop_index(op.f("ix_students_school_id"), table_name="students")
    op.drop_table("students")
    op.drop_index(op.f("ix_parents_school_id"), table_name="parents")
    op.drop_table("parents")
    # Only after nothing references them any more.
    op.drop_constraint("uq_users_id_school_id", "users", type_="unique")
    op.drop_constraint("uq_subjects_id_school_id", "subjects", type_="unique")
    op.drop_constraint("uq_sections_id_school_id", "sections", type_="unique")
    op.drop_constraint("uq_sections_id_class_id", "sections", type_="unique")
    op.drop_constraint("uq_academic_years_id_school_id", "academic_years", type_="unique")
