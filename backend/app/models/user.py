"""User accounts.

``school_id`` references ``schools.id`` (added in migration 0003). NULL is the
meaningful value for platform-level accounts (SUPER_ADMIN); every school-level
account belongs to exactly one school.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{7,14}$'", name="phone_e164"),
        CheckConstraint("status IN ('active', 'suspended', 'deactivated')", name="status_valid"),
        # Composite FK target so parents/students can only link to users of their school.
        UniqueConstraint("id", "school_id", name="uq_users_id_school_id"),
    )

    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schools.id", ondelete="RESTRICT"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    phone: Mapped[str | None] = mapped_column(String(16), unique=True)
    # NULL for accounts that never use a password (e.g. OTP-only parents).
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(20), default=UserStatus.ACTIVE.value, server_default=UserStatus.ACTIVE.value
    )
    # Derived from status by PostgreSQL, so the two can never disagree.
    is_active: Mapped[bool] = mapped_column(Boolean, Computed("status = 'active'", persisted=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # never include credentials or contact details
        return f"User(id={self.id!s}, status={self.status!r})"
