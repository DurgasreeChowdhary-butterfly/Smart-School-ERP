"""Persistence for sessions, refresh tokens, OTP challenges and login attempts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.auth import AuthSession, LoginAttempt, OtpChallenge, RefreshToken


class AuthSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, session_id: uuid.UUID, *, for_update: bool = False) -> AuthSession | None:
        return self.session.get(AuthSession, session_id, with_for_update=for_update)

    def add(self, auth_session: AuthSession) -> AuthSession:
        self.session.add(auth_session)
        self.session.flush()
        return auth_session

    def get_refresh_token_for_update(self, token_hash: str) -> RefreshToken | None:
        # Row lock serialises concurrent refreshes of the same token.
        return self.session.scalars(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        ).one_or_none()

    def add_refresh_token(self, token: RefreshToken) -> RefreshToken:
        self.session.add(token)
        self.session.flush()
        return token

    def revoke_session(self, auth_session: AuthSession, *, reason: str, now: datetime) -> None:
        """Revoke a session and every refresh token in its family (idempotent)."""
        if auth_session.revoked_at is None:
            auth_session.revoked_at = now
            auth_session.revoked_reason = reason
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == auth_session.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        self.session.flush()


class OtpChallengeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest(self, identifier_hash: str, purpose: str) -> OtpChallenge | None:
        return self.session.scalars(
            select(OtpChallenge)
            .where(OtpChallenge.identifier_hash == identifier_hash, OtpChallenge.purpose == purpose)
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
        ).one_or_none()

    def active_for_update(self, identifier_hash: str, purpose: str) -> OtpChallenge | None:
        return self.session.scalars(
            select(OtpChallenge)
            .where(
                OtpChallenge.identifier_hash == identifier_hash,
                OtpChallenge.purpose == purpose,
                OtpChallenge.consumed_at.is_(None),
                OtpChallenge.invalidated_at.is_(None),
            )
            .with_for_update()
        ).one_or_none()

    def count_for_identifier_since(
        self, identifier_hash: str, purpose: str, since: datetime
    ) -> int:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(OtpChallenge)
                .where(
                    OtpChallenge.identifier_hash == identifier_hash,
                    OtpChallenge.purpose == purpose,
                    OtpChallenge.created_at >= since,
                )
            )
            or 0
        )

    def count_for_ip_since(self, ip_address: str, since: datetime) -> int:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(OtpChallenge)
                .where(OtpChallenge.requested_ip == ip_address, OtpChallenge.created_at >= since)
            )
            or 0
        )

    def invalidate_active(self, identifier_hash: str, purpose: str, now: datetime) -> None:
        self.session.execute(
            update(OtpChallenge)
            .where(
                OtpChallenge.identifier_hash == identifier_hash,
                OtpChallenge.purpose == purpose,
                OtpChallenge.consumed_at.is_(None),
                OtpChallenge.invalidated_at.is_(None),
            )
            .values(invalidated_at=now)
        )

    def add(self, challenge: OtpChallenge) -> OtpChallenge:
        self.session.add(challenge)
        self.session.flush()
        return challenge


class LoginAttemptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, identifier_hash: str, ip_address: str | None, *, succeeded: bool, now: datetime
    ) -> None:
        self.session.add(
            LoginAttempt(
                identifier_hash=identifier_hash,
                ip_address=ip_address,
                succeeded=succeeded,
                created_at=now,
            )
        )

    def failures_for_identifier_since(self, identifier_hash: str, since: datetime) -> int:
        """Failures in the window that happened after the most recent success."""
        last_success = self.session.scalar(
            select(func.max(LoginAttempt.created_at)).where(
                LoginAttempt.identifier_hash == identifier_hash,
                LoginAttempt.succeeded.is_(True),
                LoginAttempt.created_at >= since,
            )
        )
        after_window_start = (
            LoginAttempt.created_at > last_success
            if last_success is not None
            else LoginAttempt.created_at >= since
        )
        return (
            self.session.scalar(
                select(func.count())
                .select_from(LoginAttempt)
                .where(
                    LoginAttempt.identifier_hash == identifier_hash,
                    LoginAttempt.succeeded.is_(False),
                    after_window_start,
                )
            )
            or 0
        )

    def failures_for_ip_since(self, ip_address: str, since: datetime) -> int:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(LoginAttempt)
                .where(
                    LoginAttempt.ip_address == ip_address,
                    LoginAttempt.succeeded.is_(False),
                    LoginAttempt.created_at >= since,
                )
            )
            or 0
        )
