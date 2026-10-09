"""Authentication: password login, token issuance, refresh rotation, logout.

Transaction note: failure paths **commit before raising** so that audit records,
login-attempt counters and revocations persist even though the request fails.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.audit.service import AuditAction, AuditOutcome, record_audit
from app.config import Settings
from app.core.errors import InvalidCredentialsError, InvalidTokenError, RateLimitedError
from app.core.request_context import RequestContext
from app.models.auth import AuthMethod, AuthSession, RefreshToken
from app.models.user import User, UserStatus
from app.repositories.auth import AuthSessionRepository, LoginAttemptRepository
from app.repositories.users import UserRepository
from app.security.hashing import identifier_fingerprint
from app.security.identifiers import IdentifierKind, parse_login_identifier
from app.security.passwords import (
    Argon2Params,
    dummy_password_hash,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.security.permissions import PermissionName
from app.security.tokens import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_in: int
    session_id: uuid.UUID


class SessionRevokedReason:
    LOGOUT = "logout"
    REFRESH_TOKEN_REUSE = "refresh_token_reuse"  # noqa: S105 - reason label
    USER_NOT_ELIGIBLE = "user_not_eligible"


class AuthService:
    def __init__(self, session: Session, settings: Settings, context: RequestContext) -> None:
        self.session = session
        self.settings = settings
        self.context = context
        self.users = UserRepository(session)
        self.sessions = AuthSessionRepository(session)
        self.attempts = LoginAttemptRepository(session)
        self.argon2 = Argon2Params.from_settings(settings)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def is_login_eligible(self, user: User) -> bool:
        return (
            user.status == UserStatus.ACTIVE
            and self.users.school_is_active(user)
            and PermissionName.AUTH_LOGIN in self.users.permission_names(user.id)
        )

    def _fail(self, exc: Exception) -> Exception:
        self.session.commit()
        return exc

    # ------------------------------------------------------------- password login
    def login_with_password(
        self, identifier: str, password: str, *, school_code: str | None = None
    ) -> TokenPair:
        now = self._now()
        if school_code is not None:
            # Student sign-in: admission number within the named school.
            kind = IdentifierKind.STUDENT
            normalized = f"{school_code}:{identifier.strip().upper()}"
        else:
            kind, normalized = parse_login_identifier(identifier)
        fingerprint = identifier_fingerprint(self.settings.secret_key, kind.value, normalized)
        self._enforce_login_throttle(fingerprint, kind, now)

        user = self._find_user(kind, normalized)
        reason: str | None = None
        if user is None:
            verify_password(password, dummy_password_hash(self.argon2))
            reason = "unknown_identifier"
        elif user.password_hash is None:
            verify_password(password, dummy_password_hash(self.argon2))
            reason = "password_not_set"
        elif not verify_password(password, user.password_hash):
            reason = "invalid_password"
        elif user.status != UserStatus.ACTIVE:
            reason = "account_inactive"
        elif not self.users.school_is_active(user):
            reason = "school_inactive"
        elif not self.is_login_eligible(user):
            reason = "login_not_permitted"

        if reason is not None:
            self.attempts.record(fingerprint, self.context.ip_address, succeeded=False, now=now)
            record_audit(
                self.session,
                AuditAction.LOGIN_FAILED,
                outcome=AuditOutcome.FAILURE,
                user_id=user.id if user else None,
                context=self.context,
                details={
                    "method": AuthMethod.PASSWORD.value,
                    "identifier_kind": kind.value,
                    "reason": reason,
                },
            )
            raise self._fail(InvalidCredentialsError())

        assert user is not None  # noqa: S101 - narrowed above
        assert user.password_hash is not None  # noqa: S101
        if needs_rehash(user.password_hash, self.argon2):
            user.password_hash = hash_password(password, self.argon2)
        self.attempts.record(fingerprint, self.context.ip_address, succeeded=True, now=now)
        tokens = self.complete_login(user, AuthMethod.PASSWORD, now=now)
        self.session.commit()
        return tokens

    def _find_user(self, kind: IdentifierKind, normalized: str) -> User | None:
        if kind is IdentifierKind.EMAIL:
            return self.users.get_by_email(normalized)
        if kind is IdentifierKind.PHONE:
            return self.users.get_by_phone(normalized)
        if kind is IdentifierKind.STUDENT:
            school_code, _, admission_number = normalized.partition(":")
            return self.users.get_student_login(school_code, admission_number)
        return None

    def _enforce_login_throttle(
        self, fingerprint: str, kind: IdentifierKind, now: datetime
    ) -> None:
        window = timedelta(minutes=self.settings.login_failure_window_minutes)
        since = now - window
        scope: str | None = None
        if self.attempts.failures_for_identifier_since(fingerprint, since) >= (
            self.settings.login_max_failures
        ):
            scope = "identifier"
        elif (
            self.context.ip_address
            and self.attempts.failures_for_ip_since(self.context.ip_address, since)
            >= self.settings.login_max_failures_per_ip
        ):
            scope = "ip"
        if scope is None:
            return
        record_audit(
            self.session,
            AuditAction.LOGIN_THROTTLED,
            outcome=AuditOutcome.FAILURE,
            context=self.context,
            details={"scope": scope, "identifier_kind": kind.value},
        )
        raise self._fail(
            RateLimitedError(
                "Too many failed sign-in attempts. Please try again later.",
                retry_after_seconds=int(window.total_seconds()),
            )
        )

    # ------------------------------------------------------------ token issuance
    def complete_login(self, user: User, method: AuthMethod, *, now: datetime) -> TokenPair:
        """Start a new session for an already-authenticated user (does not commit)."""
        user.last_login_at = now
        auth_session = self.sessions.add(
            AuthSession(
                user_id=user.id,
                auth_method=method.value,
                created_at=now,
                expires_at=now + timedelta(days=self.settings.session_max_age_days),
                last_used_at=now,
                ip_address=self.context.ip_address,
                user_agent=self.context.user_agent,
            )
        )
        raw_refresh, refresh_row = self._new_refresh_token(auth_session, now)
        access_token, access_expires = create_access_token(
            self.settings, user_id=user.id, session_id=auth_session.id, now=now
        )
        record_audit(
            self.session,
            AuditAction.LOGIN_SUCCEEDED,
            user_id=user.id,
            resource_type="auth_session",
            resource_id=auth_session.id,
            context=self.context,
            details={"method": method.value},
        )
        return self._pair(access_token, access_expires, raw_refresh, refresh_row, now)

    def _new_refresh_token(
        self, auth_session: AuthSession, now: datetime
    ) -> tuple[str, RefreshToken]:
        raw = generate_refresh_token()
        expires_at = min(
            now + timedelta(days=self.settings.refresh_token_ttl_days), auth_session.expires_at
        )
        row = self.sessions.add_refresh_token(
            RefreshToken(
                session_id=auth_session.id,
                user_id=auth_session.user_id,
                token_hash=hash_refresh_token(raw),
                created_at=now,
                expires_at=expires_at,
            )
        )
        return raw, row

    @staticmethod
    def _pair(
        access_token: str,
        access_expires: datetime,
        raw_refresh: str,
        refresh_row: RefreshToken,
        now: datetime,
    ) -> TokenPair:
        return TokenPair(
            access_token=access_token,
            access_expires_in=int((access_expires - now).total_seconds()),
            refresh_token=raw_refresh,
            refresh_expires_in=int((refresh_row.expires_at - now).total_seconds()),
            session_id=refresh_row.session_id,
        )

    # ------------------------------------------------------------------ refresh
    def refresh(self, raw_refresh_token: str) -> TokenPair:
        now = self._now()
        token = self.sessions.get_refresh_token_for_update(hash_refresh_token(raw_refresh_token))
        if token is None:
            self._audit_refresh_failure(None, "unknown_token")
            raise self._fail(InvalidTokenError())

        auth_session = self.sessions.get(token.session_id, for_update=True)
        assert auth_session is not None  # noqa: S101 - enforced by FK

        if token.revoked_at is not None:
            self._handle_reuse(token, auth_session, now)
            raise self._fail(InvalidTokenError())

        if token.expires_at <= now or auth_session.expires_at <= now:
            self._audit_refresh_failure(token.user_id, "expired", auth_session.id)
            raise self._fail(InvalidTokenError())
        if auth_session.revoked_at is not None:
            self._audit_refresh_failure(token.user_id, "session_revoked", auth_session.id)
            raise self._fail(InvalidTokenError())

        user = self.users.get(token.user_id)
        if (
            user is None
            or user.status != UserStatus.ACTIVE
            or not self.users.school_is_active(user)
            or PermissionName.AUTH_REFRESH not in self.users.permission_names(user.id)
        ):
            self.sessions.revoke_session(
                auth_session, reason=SessionRevokedReason.USER_NOT_ELIGIBLE, now=now
            )
            self._audit_refresh_failure(token.user_id, "user_not_eligible", auth_session.id)
            raise self._fail(InvalidTokenError())

        raw_new, new_row = self._new_refresh_token(auth_session, now)
        token.revoked_at = now
        token.last_used_at = now
        token.replaced_by_id = new_row.id
        auth_session.last_used_at = now
        access_token, access_expires = create_access_token(
            self.settings, user_id=user.id, session_id=auth_session.id, now=now
        )
        record_audit(
            self.session,
            AuditAction.REFRESH_SUCCEEDED,
            user_id=user.id,
            resource_type="auth_session",
            resource_id=auth_session.id,
            context=self.context,
        )
        self.session.commit()
        return self._pair(access_token, access_expires, raw_new, new_row, now)

    def _handle_reuse(self, token: RefreshToken, auth_session: AuthSession, now: datetime) -> None:
        """A revoked token was presented: assume theft and kill the whole family."""
        if token.reuse_detected_at is None:
            token.reuse_detected_at = now
        self.sessions.revoke_session(
            auth_session, reason=SessionRevokedReason.REFRESH_TOKEN_REUSE, now=now
        )
        record_audit(
            self.session,
            AuditAction.REFRESH_REUSE_DETECTED,
            outcome=AuditOutcome.FAILURE,
            user_id=token.user_id,
            resource_type="auth_session",
            resource_id=auth_session.id,
            context=self.context,
            # Key names avoid "token": the audit sanitiser redacts any such key.
            details={
                "refresh_record_id": str(token.id),
                "was_rotated": token.replaced_by_id is not None,
            },
        )
        logger.warning(
            "refresh token reuse detected; session revoked",
            extra={"user_id": str(token.user_id), "session_id": str(auth_session.id)},
        )

    def _audit_refresh_failure(
        self, user_id: uuid.UUID | None, reason: str, session_id: uuid.UUID | None = None
    ) -> None:
        record_audit(
            self.session,
            AuditAction.REFRESH_FAILED,
            outcome=AuditOutcome.FAILURE,
            user_id=user_id,
            resource_type="auth_session" if session_id else None,
            resource_id=session_id,
            context=self.context,
            details={"reason": reason},
        )

    # ------------------------------------------------------------------- logout
    def logout(self, raw_refresh_token: str) -> None:
        """Revoke the session that owns the token. Idempotent; unknown tokens are ignored."""
        now = self._now()
        token = self.sessions.get_refresh_token_for_update(hash_refresh_token(raw_refresh_token))
        if token is None:
            return
        auth_session = self.sessions.get(token.session_id, for_update=True)
        assert auth_session is not None  # noqa: S101 - enforced by FK
        already_revoked = auth_session.revoked_at is not None
        self.sessions.revoke_session(auth_session, reason=SessionRevokedReason.LOGOUT, now=now)
        record_audit(
            self.session,
            AuditAction.LOGOUT,
            user_id=token.user_id,
            resource_type="auth_session",
            resource_id=auth_session.id,
            context=self.context,
            details={"already_revoked": already_revoked},
        )
        self.session.commit()
