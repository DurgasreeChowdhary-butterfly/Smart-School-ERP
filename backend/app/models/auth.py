"""Authentication state: sessions, refresh tokens, OTP challenges, login attempts.

No raw secret is ever stored: refresh tokens are kept as SHA-256 digests, OTP
codes and login identifiers as keyed HMAC digests.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class AuthMethod(StrEnum):
    PASSWORD = "password"  # noqa: S105 - enum label, not a secret
    OTP = "otp"


class OtpPurpose(StrEnum):
    LOGIN = "login"


class OtpChannel(StrEnum):
    SMS = "sms"


class AuthSession(UUIDPrimaryKeyMixin, Base):
    """One login = one session = one refresh-token family."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("auth_method IN ('password', 'otp')", name="auth_method_valid"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    auth_method: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(50))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "refresh_tokens"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    reuse_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OtpChallenge(UUIDPrimaryKeyMixin, Base):
    """A single OTP issued to an identifier (e.g. a parent's mobile number).

    ``identifier_hash`` is an HMAC of the normalised destination, so challenges
    (and rate limits) work even for unknown numbers without storing them; in that
    case ``user_id`` is NULL and verification can never succeed.
    """

    __tablename__ = "otp_challenges"
    __table_args__ = (
        CheckConstraint("purpose IN ('login')", name="purpose_valid"),
        CheckConstraint("channel IN ('sms')", name="channel_valid"),
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_range"),
        Index(
            "ix_otp_challenges_identifier_purpose_created",
            "identifier_hash",
            "purpose",
            "created_at",
        ),
        Index("ix_otp_challenges_requested_ip_created", "requested_ip", "created_at"),
        # At most one live challenge per identifier and purpose.
        Index(
            "uq_otp_challenges_one_active",
            "identifier_hash",
            "purpose",
            unique=True,
            postgresql_where=text("consumed_at IS NULL AND invalidated_at IS NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    identifier_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(30))
    channel: Mapped[str] = mapped_column(String(20))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginAttempt(UUIDPrimaryKeyMixin, Base):
    """Password-login outcomes keyed by identifier HMAC; drives brute-force throttling."""

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_identifier_created", "identifier_hash", "created_at"),
        Index("ix_login_attempts_ip_created", "ip_address", "created_at"),
    )

    identifier_hash: Mapped[str] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(INET)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
