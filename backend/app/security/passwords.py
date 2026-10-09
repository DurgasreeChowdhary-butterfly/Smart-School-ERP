"""Argon2id password hashing and a pragmatic (NIST SP 800-63B style) password policy."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import Settings
from app.core.errors import BusinessRuleError
from app.schemas.common import ErrorDetail


@dataclass(frozen=True, slots=True)
class Argon2Params:
    time_cost: int = 3
    memory_cost_kib: int = 65_536
    parallelism: int = 4

    @classmethod
    def from_settings(cls, settings: Settings) -> Argon2Params:
        return cls(
            time_cost=settings.argon2_time_cost,
            memory_cost_kib=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )


@lru_cache(maxsize=8)
def _hasher(params: Argon2Params) -> PasswordHasher:
    return PasswordHasher(
        time_cost=params.time_cost,
        memory_cost=params.memory_cost_kib,
        parallelism=params.parallelism,
        type=Type.ID,
    )


def hash_password(password: str, params: Argon2Params) -> str:
    """Return an Argon2id PHC string (includes salt and parameters)."""
    return _hasher(params).hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification. Never raises for a wrong password or malformed hash."""
    try:
        # Parameters are read from the hash itself, so any hasher instance works.
        return _hasher(Argon2Params()).verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str, params: Argon2Params) -> bool:
    try:
        return _hasher(params).check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=8)
def dummy_password_hash(params: Argon2Params) -> str:
    """Hash verified when an account does not exist, equalising login timing."""
    return hash_password("dummy-password-for-timing-equalisation", params)


# Small blocklist of the most common passwords; extend or replace with a breach corpus later.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "123456789",
        "1234567890",
        "12345678910",
        "qwertyuiop",
        "qwerty123",
        "iloveyou",
        "admin12345",
        "welcome123",
        "letmein123",
        "abc1234567",
        "1q2w3e4r5t",
        "passw0rd123",
        "school1234",
        "school@123",
        "student123",
        "teacher123",
    }
)


class PasswordPolicyError(BusinessRuleError):
    code = "password_policy_violation"
    message = "The password does not meet the password policy."


def validate_password_policy(
    password: str, settings: Settings, *, user_inputs: tuple[str | None, ...] = ()
) -> None:
    """Length-based policy without composition rules. Raises :class:`PasswordPolicyError`."""
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(f"Must be at least {settings.password_min_length} characters long.")
    if len(password) > settings.password_max_length:
        problems.append(f"Must be at most {settings.password_max_length} characters long.")
    if not password.strip():
        problems.append("Must not be blank.")
    elif len(set(password)) < 3:
        problems.append("Must not consist of a single repeated character.")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("Is too common.")
    lowered = password.lower()
    for value in user_inputs:
        if not value:
            continue
        token = value.lower().split("@", 1)[0]
        if len(token) >= 4 and token in lowered:
            problems.append("Must not contain your name, email or phone number.")
            break
    if problems:
        raise PasswordPolicyError(
            details=[ErrorDetail(loc=["password"], message=problem) for problem in problems]
        )
