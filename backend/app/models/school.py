"""Schools: the tenant boundary for all school-owned data.

Schools are never hard-deleted; ``is_active = false`` suspends operational use
(sign-in and school-scoped APIs) while preserving history.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_SCHOOL_TIMEZONE = "UTC"


class School(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "schools"
    __table_args__ = (
        CheckConstraint("code = upper(code)", name="code_uppercase"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{7,14}$'", name="phone_e164"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )

    name: Mapped[str] = mapped_column(String(200))
    # Immutable short identifier (e.g. "GHS-PUNE"); unique across the platform.
    code: Mapped[str] = mapped_column(String(30), unique=True)
    address: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String(320))
    # IANA name, validated against the tz database by the API layer.
    timezone: Mapped[str] = mapped_column(
        String(64), default=DEFAULT_SCHOOL_TIMEZONE, server_default=DEFAULT_SCHOOL_TIMEZONE
    )
    logo_url: Mapped[str | None] = mapped_column(String(2048))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
