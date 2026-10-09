"""auth, RBAC and audit: users, roles, permissions, sessions, OTP, audit log

Creates the authentication/authorization schema and seeds the initial roles,
permissions and role-permission matrix.

The seed below is a literal snapshot of ``app.security.permissions`` at the
time of writing; later catalog changes must ship as new migrations (the test
suite fails if the database and the code catalog drift apart). IDs are
deterministic (uuid5) so they are identical in every environment.

users.school_id has no foreign key yet: the schools table is created in Phase 3,
whose migration adds ``fk_users_school_id_schools``.

Revision ID: 0002_auth_rbac
Revises: 0001_baseline
Create Date: 2026-10-06 06:49:00.775408+00:00
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_auth_rbac"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID_NAMESPACE = uuid.UUID("6f1c2a52-0b7e-4f43-9d6c-2f0d8a1e5b11")

ROLES: list[tuple[str, str]] = [
    ("SUPER_ADMIN", "Platform-wide administrator (all schools)."),
    ("SCHOOL_ADMIN", "Administers one school: users, structure and records."),
    ("MANAGEMENT", "School leadership: oversight, reporting and result publication."),
    ("FACULTY", "Teaching staff: works with assigned classes and students."),
    ("PARENT", "Parent or guardian: views linked children's information."),
    ("STUDENT", "Student: views own information."),
]

PERMISSIONS: list[tuple[str, str]] = [
    ("auth.login", "Sign in."),
    ("auth.refresh", "Renew a session with a refresh token."),
    ("auth.logout", "Sign out."),
    ("auth.me", "View own account profile."),
    ("user.read", "View user accounts."),
    ("user.create", "Create user accounts."),
    ("user.update", "Update user accounts."),
    ("user.deactivate", "Deactivate user accounts."),
    ("user.assign_role", "Assign or remove user roles."),
    ("role.read", "View roles."),
    ("role.manage", "Create or modify role definitions."),
    ("permission.read", "View permissions."),
    ("permission.manage", "Modify the permission catalog."),
    ("school.read", "View school information."),
    ("school.manage", "Manage school settings."),
    ("student.read_self", "View own student record."),
    ("student.read", "View student records within scope."),
    ("student.create", "Create student records."),
    ("student.update", "Update student records."),
    ("student.deactivate", "Deactivate student records."),
    ("parent.read_self", "View own parent profile."),
    ("parent.read_child", "View linked children's records."),
    ("faculty.read_self", "View own faculty profile."),
    ("faculty.read_assigned_students", "View students in assigned classes."),
    ("faculty.manage", "Manage faculty records and assignments."),
    ("attendance.read", "View attendance within scope."),
    ("attendance.manage", "Record or correct attendance within scope."),
    ("academic.read", "View academic structure (years, classes, subjects)."),
    ("academic.manage", "Manage academic structure."),
    ("marks.read", "View marks/results within scope."),
    ("marks.manage", "Enter or correct marks within scope."),
    ("marks.publish", "Publish exam results."),
    ("assignment.read", "View assignments within scope."),
    ("assignment.manage", "Create assignments and review submissions within scope."),
    ("timetable.read", "View timetables within scope."),
    ("timetable.manage", "Manage timetables."),
    ("announcement.read", "View announcements."),
    ("announcement.manage", "Create and publish announcements."),
    ("notification.read", "View own notifications."),
    ("notification.manage", "Send and manage notifications."),
    ("report.read", "View reports."),
    ("analytics.read", "View analytics."),
    ("ai.read", "View AI insights within scope."),
    ("ai.manage", "Configure AI features."),
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "SUPER_ADMIN": [
        "academic.manage",
        "academic.read",
        "ai.manage",
        "ai.read",
        "analytics.read",
        "announcement.manage",
        "announcement.read",
        "assignment.manage",
        "assignment.read",
        "attendance.manage",
        "attendance.read",
        "auth.login",
        "auth.logout",
        "auth.me",
        "auth.refresh",
        "faculty.manage",
        "marks.manage",
        "marks.publish",
        "marks.read",
        "notification.manage",
        "notification.read",
        "permission.manage",
        "permission.read",
        "report.read",
        "role.manage",
        "role.read",
        "school.manage",
        "school.read",
        "student.create",
        "student.deactivate",
        "student.read",
        "student.update",
        "timetable.manage",
        "timetable.read",
        "user.assign_role",
        "user.create",
        "user.deactivate",
        "user.read",
        "user.update",
    ],
    "SCHOOL_ADMIN": [
        "academic.manage",
        "academic.read",
        "ai.manage",
        "ai.read",
        "analytics.read",
        "announcement.manage",
        "announcement.read",
        "assignment.manage",
        "assignment.read",
        "attendance.manage",
        "attendance.read",
        "auth.login",
        "auth.logout",
        "auth.me",
        "auth.refresh",
        "faculty.manage",
        "marks.manage",
        "marks.publish",
        "marks.read",
        "notification.manage",
        "notification.read",
        "permission.read",
        "report.read",
        "role.read",
        "school.manage",
        "school.read",
        "student.create",
        "student.deactivate",
        "student.read",
        "student.update",
        "timetable.manage",
        "timetable.read",
        "user.assign_role",
        "user.create",
        "user.deactivate",
        "user.read",
        "user.update",
    ],
    "MANAGEMENT": [
        "academic.read",
        "ai.read",
        "analytics.read",
        "announcement.manage",
        "announcement.read",
        "assignment.read",
        "attendance.read",
        "auth.login",
        "auth.logout",
        "auth.me",
        "auth.refresh",
        "marks.publish",
        "marks.read",
        "notification.read",
        "report.read",
        "role.read",
        "school.read",
        "student.read",
        "timetable.read",
        "user.read",
    ],
    "FACULTY": [
        "academic.read",
        "announcement.read",
        "assignment.manage",
        "assignment.read",
        "attendance.manage",
        "attendance.read",
        "auth.login",
        "auth.logout",
        "auth.me",
        "auth.refresh",
        "faculty.read_assigned_students",
        "faculty.read_self",
        "marks.manage",
        "marks.read",
        "notification.read",
        "school.read",
        "timetable.read",
    ],
    "PARENT": [
        "academic.read",
        "announcement.read",
        "assignment.read",
        "attendance.read",
        "auth.login",
        "auth.logout",
        "auth.me",
        "auth.refresh",
        "marks.read",
        "notification.read",
        "parent.read_child",
        "parent.read_self",
        "school.read",
        "timetable.read",
    ],
    "STUDENT": [
        "academic.read",
        "announcement.read",
        "assignment.read",
        "attendance.read",
        "auth.login",
        "auth.logout",
        "auth.me",
        "auth.refresh",
        "marks.read",
        "notification.read",
        "school.read",
        "student.read_self",
        "timetable.read",
    ],
}


def _role_id(name: str) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"role:{name}")


def _permission_id(name: str) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"permission:{name}")


def _seed_rbac() -> None:
    roles = sa.table(
        "roles",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
    )
    permissions = sa.table(
        "permissions",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
    )
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Uuid()),
        sa.column("permission_id", sa.Uuid()),
    )
    op.bulk_insert(roles, [{"id": _role_id(n), "name": n, "description": d} for n, d in ROLES])
    op.bulk_insert(
        permissions,
        [{"id": _permission_id(n), "name": n, "description": d} for n, d in PERMISSIONS],
    )
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": _role_id(role), "permission_id": _permission_id(permission)}
            for role, granted in ROLE_PERMISSIONS.items()
            for permission in granted
        ],
    )


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("identifier_hash", sa.String(length=64), nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
    )
    op.create_index(
        "ix_login_attempts_identifier_created",
        "login_attempts",
        ["identifier_hash", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_login_attempts_ip_created", "login_attempts", ["ip_address", "created_at"], unique=False
    )
    op.create_table(
        "permissions",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("name", name=op.f("uq_permissions_name")),
    )
    op.create_table(
        "roles",
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_roles_name")),
    )
    op.create_table(
        "users",
        sa.Column("school_id", sa.Uuid(), nullable=True),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            sa.Computed("status = 'active'", persisted=True),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("phone ~ '^\\+[1-9][0-9]{7,14}$'", name=op.f("ck_users_phone_e164")),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'deactivated')", name=op.f("ck_users_status_valid")
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.UniqueConstraint("phone", name=op.f("uq_users_phone")),
    )
    op.create_index(op.f("ix_users_school_id"), "users", ["school_id"], unique=False)
    op.create_table(
        "audit_logs",
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure')", name=op.f("ck_audit_logs_outcome_valid")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_logs_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_action_created", "audit_logs", ["action", "created_at"], unique=False
    )
    op.create_index(op.f("ix_audit_logs_created_at"), "audit_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"], unique=False
    )
    op.create_index(
        "ix_audit_logs_user_created", "audit_logs", ["user_id", "created_at"], unique=False
    )
    op.create_table(
        "auth_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("auth_method", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=50), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "auth_method IN ('password', 'otp')", name=op.f("ck_auth_sessions_auth_method_valid")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"], unique=False)
    op.create_table(
        "otp_challenges",
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("identifier_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("channel IN ('sms')", name=op.f("ck_otp_challenges_channel_valid")),
        sa.CheckConstraint("purpose IN ('login')", name=op.f("ck_otp_challenges_purpose_valid")),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts",
            name=op.f("ck_otp_challenges_attempts_range"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_otp_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_challenges")),
    )
    op.create_index(
        "ix_otp_challenges_identifier_purpose_created",
        "otp_challenges",
        ["identifier_hash", "purpose", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_otp_challenges_requested_ip_created",
        "otp_challenges",
        ["requested_ip", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_otp_challenges_user_id"), "otp_challenges", ["user_id"], unique=False)
    op.create_index(
        "uq_otp_challenges_one_active",
        "otp_challenges",
        ["identifier_hash", "purpose"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL AND invalidated_at IS NULL"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    op.create_index(
        op.f("ix_role_permissions_permission_id"),
        "role_permissions",
        ["permission_id"],
        unique=False,
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_id"],
            ["users.id"],
            name=op.f("fk_user_roles_assigned_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_index(op.f("ix_user_roles_role_id"), "user_roles", ["role_id"], unique=False)
    op.create_table(
        "refresh_tokens",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column("reuse_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_refresh_tokens_session_id_auth_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_refresh_tokens_session_id"), "refresh_tokens", ["session_id"], unique=False
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)

    _seed_rbac()


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_session_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index(op.f("ix_user_roles_role_id"), table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_index(op.f("ix_role_permissions_permission_id"), table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_index(
        "uq_otp_challenges_one_active",
        table_name="otp_challenges",
        postgresql_where=sa.text("consumed_at IS NULL AND invalidated_at IS NULL"),
    )
    op.drop_index(op.f("ix_otp_challenges_user_id"), table_name="otp_challenges")
    op.drop_index("ix_otp_challenges_requested_ip_created", table_name="otp_challenges")
    op.drop_index("ix_otp_challenges_identifier_purpose_created", table_name="otp_challenges")
    op.drop_table("otp_challenges")
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_audit_logs_user_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_created_at"), table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index(op.f("ix_users_school_id"), table_name="users")
    op.drop_table("users")
    op.drop_table("roles")
    op.drop_table("permissions")
    op.drop_index("ix_login_attempts_ip_created", table_name="login_attempts")
    op.drop_index("ix_login_attempts_identifier_created", table_name="login_attempts")
    op.drop_table("login_attempts")
