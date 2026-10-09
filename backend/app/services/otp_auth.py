"""Mobile-number OTP sign-in (intended for parents; usable by any eligible account).

Flow: ``request_login_otp(phone)`` -> code delivered by SMS -> ``verify_login_otp``
-> a normal session/token pair (same as password login).

Anti-enumeration: requests for unknown or ineligible numbers get the *same*
response and the same rate limits; a challenge is still recorded (with no user)
but nothing is sent and verification can never succeed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import AuditAction, AuditOutcome, record_audit
from app.config import Settings
from app.core.errors import InvalidCredentialsError, RateLimitedError, ServiceUnavailableError
from app.core.request_context import RequestContext
from app.models.auth import AuthMethod, OtpChallenge, OtpChannel, OtpPurpose
from app.repositories.auth import OtpChallengeRepository
from app.repositories.users import UserRepository
from app.security.hashing import identifier_fingerprint
from app.security.identifiers import IdentifierKind
from app.security.otp import generate_otp, hash_otp, verify_otp
from app.security.otp_delivery import OtpSender
from app.services.auth import AuthService, TokenPair

logger = logging.getLogger(__name__)

_HOUR = timedelta(hours=1)
INVALID_CODE_MESSAGE = "The code is invalid or has expired."


@dataclass(frozen=True, slots=True)
class OtpRequestResult:
    expires_in: int
    resend_after: int


class OtpAuthService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        context: RequestContext,
        sender: OtpSender | None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.context = context
        self.sender = sender
        self.challenges = OtpChallengeRepository(session)
        self.users = UserRepository(session)
        self.auth = AuthService(session, settings, context)

    def _fingerprint(self, phone: str) -> str:
        return identifier_fingerprint(self.settings.secret_key, IdentifierKind.PHONE.value, phone)

    def _throttled(self, scope: str, retry_after: int) -> RateLimitedError:
        record_audit(
            self.session,
            AuditAction.OTP_THROTTLED,
            outcome=AuditOutcome.FAILURE,
            context=self.context,
            details={"scope": scope},
        )
        self.session.commit()
        return RateLimitedError(
            "Too many code requests. Please try again later.", retry_after_seconds=retry_after
        )

    # ------------------------------------------------------------------ request
    def request_login_otp(self, phone: str) -> OtpRequestResult:
        if self.sender is None:
            raise ServiceUnavailableError("Sign-in with a one-time code is not available.")

        now = datetime.now(UTC)
        purpose = OtpPurpose.LOGIN.value
        fingerprint = self._fingerprint(phone)

        latest = self.challenges.latest(fingerprint, purpose)
        cooldown = timedelta(seconds=self.settings.otp_resend_cooldown_seconds)
        if latest is not None and latest.created_at + cooldown > now:
            remaining = int((latest.created_at + cooldown - now).total_seconds()) + 1
            raise self._throttled("resend_cooldown", remaining)
        if (
            self.challenges.count_for_identifier_since(fingerprint, purpose, now - _HOUR)
            >= self.settings.otp_max_requests_per_hour
        ):
            raise self._throttled("identifier_hourly_limit", int(_HOUR.total_seconds()))
        if (
            self.context.ip_address
            and self.challenges.count_for_ip_since(self.context.ip_address, now - _HOUR)
            >= self.settings.otp_max_requests_per_ip_per_hour
        ):
            raise self._throttled("ip_hourly_limit", int(_HOUR.total_seconds()))

        # A new code supersedes any earlier one for this number.
        self.challenges.invalidate_active(fingerprint, purpose, now)

        user = self.users.get_by_phone(phone)
        eligible = user is not None and self.auth.is_login_eligible(user)
        code = generate_otp(self.settings.otp_length)
        challenge_id = uuid.uuid4()
        new_challenge = OtpChallenge(
            id=challenge_id,
            user_id=user.id if eligible and user else None,
            identifier_hash=fingerprint,
            purpose=purpose,
            channel=OtpChannel.SMS.value,
            code_hash=hash_otp(self.settings.secret_key, challenge_id, code),
            expires_at=now + timedelta(seconds=self.settings.otp_ttl_seconds),
            max_attempts=self.settings.otp_max_attempts,
            requested_ip=self.context.ip_address,
            created_at=now,
        )
        try:
            self.challenges.add(new_challenge)
        except IntegrityError as exc:
            # A concurrent request for the same number won the one-active-challenge race.
            self.session.rollback()
            raise RateLimitedError(
                "Too many code requests. Please try again later.",
                retry_after_seconds=max(1, self.settings.otp_resend_cooldown_seconds),
            ) from exc
        record_audit(
            self.session,
            AuditAction.OTP_REQUESTED,
            user_id=user.id if eligible and user else None,
            resource_type="otp_challenge",
            resource_id=challenge_id,
            context=self.context,
            details={"purpose": purpose, "channel": OtpChannel.SMS.value, "delivered": eligible},
        )
        self.session.commit()

        if eligible:
            try:
                self.sender.send(
                    phone=phone, code=code, expires_in_seconds=self.settings.otp_ttl_seconds
                )
            except Exception as exc:
                logger.error("OTP delivery failed", extra={"error": type(exc).__name__})
                raise ServiceUnavailableError(
                    "The code could not be sent. Please try again."
                ) from exc

        return OtpRequestResult(
            expires_in=self.settings.otp_ttl_seconds,
            resend_after=self.settings.otp_resend_cooldown_seconds,
        )

    # ------------------------------------------------------------------- verify
    def verify_login_otp(self, phone: str, code: str) -> TokenPair:
        now = datetime.now(UTC)
        purpose = OtpPurpose.LOGIN.value
        challenge = self.challenges.active_for_update(self._fingerprint(phone), purpose)

        if challenge is None:
            raise self._verify_failed(None, None, "no_active_challenge")
        if challenge.expires_at <= now:
            challenge.invalidated_at = now
            raise self._verify_failed(challenge, None, "expired")
        if challenge.attempts >= challenge.max_attempts:
            challenge.invalidated_at = now
            raise self._verify_failed(challenge, None, "max_attempts_exceeded")

        challenge.attempts += 1
        if not verify_otp(self.settings.secret_key, challenge.id, code, challenge.code_hash):
            if challenge.attempts >= challenge.max_attempts:
                challenge.invalidated_at = now
            raise self._verify_failed(challenge, None, "invalid_code")

        challenge.consumed_at = now  # one-time: consumed even if the account is ineligible
        user = self.users.get(challenge.user_id) if challenge.user_id else None
        if user is None or not self.auth.is_login_eligible(user):
            raise self._verify_failed(challenge, user.id if user else None, "account_not_eligible")

        record_audit(
            self.session,
            AuditAction.OTP_VERIFIED,
            user_id=user.id,
            resource_type="otp_challenge",
            resource_id=challenge.id,
            context=self.context,
            details={"purpose": purpose, "attempts": challenge.attempts},
        )
        tokens = self.auth.complete_login(user, AuthMethod.OTP, now=now)
        self.session.commit()
        return tokens

    def _verify_failed(
        self, challenge: OtpChallenge | None, user_id: uuid.UUID | None, reason: str
    ) -> InvalidCredentialsError:
        record_audit(
            self.session,
            AuditAction.OTP_FAILED,
            outcome=AuditOutcome.FAILURE,
            user_id=user_id or (challenge.user_id if challenge else None),
            resource_type="otp_challenge" if challenge else None,
            resource_id=challenge.id if challenge else None,
            context=self.context,
            details={
                "reason": reason,
                "attempts": challenge.attempts if challenge else None,
            },
        )
        self.session.commit()
        return InvalidCredentialsError(INVALID_CODE_MESSAGE)
