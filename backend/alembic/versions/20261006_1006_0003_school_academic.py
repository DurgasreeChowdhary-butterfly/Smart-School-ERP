"""school and academic structure; users.school_id foreign key

Creates schools, academic_years, classes, sections and subjects, and completes
the Phase 2 design by adding ``fk_users_school_id_schools``.

* Overlapping academic years are prevented by an exclusion constraint, which
  needs the ``btree_gist`` extension (a trusted extension since PostgreSQL 13;
  the migration role must be allowed to create it). Downgrade leaves the
  extension installed because other objects may come to depend on it.
* Existing users are preserved. Because no school exists before this
  migration, any user that already carries a ``school_id`` would violate the new
  foreign key; in that case the upgrade stops with an explanation instead of
  inventing schools or discarding the value.

Revision ID: 0003_school_academic
Revises: 0002_auth_rbac
Create Date: 2026-10-06 10:06:10.495503+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_school_academic"
down_revision: str | None = "0002_auth_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_no_orphaned_user_schools() -> None:
    orphaned = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM users u WHERE u.school_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM schools s WHERE s.id = u.school_id)"
            )
        )
        .scalar_one()
    )
    if orphaned:
        raise RuntimeError(
            f"{orphaned} user(s) reference a school_id that does not exist. Create those "
            "schools (keeping the same ids) or clear users.school_id deliberately, then "
            "re-run the migration. No data was changed."
        )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "schools",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=100), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("timezone", sa.String(length=64), server_default="UTC", nullable=False),
        sa.Column("logo_url", sa.String(length=2048), nullable=True),
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
        sa.CheckConstraint("phone ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f("ck_schools_phone_e164")),
        sa.CheckConstraint("code = upper(code)", name=op.f("ck_schools_code_uppercase")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_schools_email_lowercase")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_schools_name_not_blank")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schools")),
        sa.UniqueConstraint("code", name=op.f("uq_schools_code")),
    )
    op.create_table(
        "academic_years",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        postgresql.ExcludeConstraint(
            (sa.column("school_id"), "="),
            (sa.text("daterange(start_date, end_date, '[]')"), "&&"),
            using="gist",
            name="ex_academic_years_no_overlap",
        ),
        sa.CheckConstraint(
            "NOT is_current OR is_active", name=op.f("ck_academic_years_current_is_active")
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_academic_years_name_not_blank")),
        sa.CheckConstraint("start_date < end_date", name=op.f("ck_academic_years_dates_ordered")),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_academic_years_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_academic_years")),
        sa.UniqueConstraint("school_id", "name", name=op.f("uq_academic_years_school_id_name")),
    )
    op.create_index(
        op.f("ix_academic_years_school_id"), "academic_years", ["school_id"], unique=False
    )
    op.create_index(
        "uq_academic_years_one_current_per_school",
        "academic_years",
        ["school_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "classes",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
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
        sa.CheckConstraint("code = upper(code)", name=op.f("ck_classes_code_uppercase")),
        sa.CheckConstraint(
            "display_order >= 0", name=op.f("ck_classes_display_order_non_negative")
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_classes_name_not_blank")),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_classes_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_classes")),
        sa.UniqueConstraint("id", "school_id", name="uq_classes_id_school_id"),
        sa.UniqueConstraint("school_id", "code", name=op.f("uq_classes_school_id_code")),
    )
    op.create_index(op.f("ix_classes_school_id"), "classes", ["school_id"], unique=False)
    op.create_table(
        "subjects",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
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
        sa.CheckConstraint("code = upper(code)", name=op.f("ck_subjects_code_uppercase")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_subjects_name_not_blank")),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_subjects_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subjects")),
        sa.UniqueConstraint("school_id", "code", name=op.f("uq_subjects_school_id_code")),
    )
    op.create_index(op.f("ix_subjects_school_id"), "subjects", ["school_id"], unique=False)
    op.create_table(
        "sections",
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("class_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=True),
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
            "capacity IS NULL OR capacity > 0", name=op.f("ck_sections_capacity_positive")
        ),
        sa.CheckConstraint("code = upper(code)", name=op.f("ck_sections_code_uppercase")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_sections_name_not_blank")),
        sa.ForeignKeyConstraint(
            ["class_id", "school_id"],
            ["classes.id", "classes.school_id"],
            name="fk_sections_class_same_school",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["school_id"],
            ["schools.id"],
            name=op.f("fk_sections_school_id_schools"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sections")),
        sa.UniqueConstraint("class_id", "code", name=op.f("uq_sections_class_id_code")),
    )
    op.create_index(op.f("ix_sections_class_id"), "sections", ["class_id"], unique=False)
    op.create_index(op.f("ix_sections_school_id"), "sections", ["school_id"], unique=False)
    _assert_no_orphaned_user_schools()
    op.create_foreign_key(
        op.f("fk_users_school_id_schools"),
        "users",
        "schools",
        ["school_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # users keep their school_id values; only the constraint is removed.
    op.drop_constraint(op.f("fk_users_school_id_schools"), "users", type_="foreignkey")
    op.drop_index(op.f("ix_sections_school_id"), table_name="sections")
    op.drop_index(op.f("ix_sections_class_id"), table_name="sections")
    op.drop_table("sections")
    op.drop_index(op.f("ix_subjects_school_id"), table_name="subjects")
    op.drop_table("subjects")
    op.drop_index(op.f("ix_classes_school_id"), table_name="classes")
    op.drop_table("classes")
    op.drop_index(
        "uq_academic_years_one_current_per_school",
        table_name="academic_years",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_index(op.f("ix_academic_years_school_id"), table_name="academic_years")
    op.drop_table("academic_years")
    op.drop_table("schools")
