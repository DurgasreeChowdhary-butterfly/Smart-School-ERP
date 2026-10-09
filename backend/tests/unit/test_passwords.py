from __future__ import annotations

import pytest

from app.config import Settings
from app.security.passwords import (
    Argon2Params,
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)

FAST = Argon2Params(time_cost=1, memory_cost_kib=1024, parallelism=1)


def test_hash_is_argon2id_and_never_contains_plaintext() -> None:
    hashed = hash_password("Correct-Horse-Battery-9", FAST)
    assert hashed.startswith("$argon2id$")
    assert "Correct-Horse-Battery-9" not in hashed


def test_hashes_are_salted() -> None:
    assert hash_password("same-password-123", FAST) != hash_password("same-password-123", FAST)


def test_verify_accepts_correct_password() -> None:
    assert verify_password(
        "Correct-Horse-Battery-9", hash_password("Correct-Horse-Battery-9", FAST)
    )


def test_verify_rejects_incorrect_password() -> None:
    hashed = hash_password("Correct-Horse-Battery-9", FAST)
    assert not verify_password("correct-horse-battery-9", hashed)
    assert not verify_password("", hashed)


def test_verify_never_raises_on_malformed_hash() -> None:
    assert not verify_password("anything", "not-a-valid-hash")
    assert not verify_password("anything", "")


def test_needs_rehash_when_parameters_change() -> None:
    hashed = hash_password("Correct-Horse-Battery-9", FAST)
    assert not needs_rehash(hashed, FAST)
    assert needs_rehash(hashed, Argon2Params(time_cost=2, memory_cost_kib=2048, parallelism=1))
    assert needs_rehash("garbage", FAST)


@pytest.fixture
def policy_settings() -> Settings:
    return Settings(_env_file=None, password_min_length=10, password_max_length=128)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "password",
    ["Correct-Horse-Battery-9", "a long passphrase with spaces", "ÜnïcødePässwörd!"],
)
def test_policy_accepts_reasonable_passwords(policy_settings: Settings, password: str) -> None:
    validate_password_policy(password, policy_settings)


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("short", "at least 10"),
        ("x" * 129, "at most 128"),
        ("          ", "blank"),
        ("aaaaaaaaaaaa", "single repeated"),
        ("password123", "too common"),
        ("Password123", "too common"),
    ],
)
def test_policy_rejects_weak_passwords(
    policy_settings: Settings, password: str, fragment: str
) -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password_policy(password, policy_settings)
    messages = " ".join(d.message for d in exc_info.value.details)
    assert fragment in messages
    assert password not in messages  # the password is never echoed


def test_policy_rejects_password_containing_user_details(policy_settings: Settings) -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password_policy(
            "priya.sharma-2026", policy_settings, user_inputs=("priya.sharma@school.example",)
        )


def test_policy_is_configurable() -> None:
    settings = Settings(_env_file=None, password_min_length=16)  # type: ignore[call-arg]
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("Fifteen-chars-1", settings)
