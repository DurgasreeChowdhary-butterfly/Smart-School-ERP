"""OTP primitives, identifier normalisation, keyed hashing and the OTP delivery stub."""

from __future__ import annotations

import logging
import uuid

import pytest
from pydantic import SecretStr

from app.config import Environment, Settings
from app.security.hashing import identifier_fingerprint, keyed_digest
from app.security.identifiers import (
    IdentifierKind,
    mask_phone,
    normalize_phone,
    parse_login_identifier,
)
from app.security.otp import generate_otp, hash_otp, verify_otp
from app.security.otp_delivery import DevelopmentConsoleOtpSender, build_otp_sender

KEY = SecretStr("unit-test-key-0123456789-abcdefghijkl")


@pytest.mark.parametrize("length", [4, 6, 8, 10])
def test_generate_otp_has_requested_length_and_digits(length: int) -> None:
    codes = {generate_otp(length) for _ in range(200)}
    assert all(len(code) == length and code.isdigit() for code in codes)
    assert len(codes) > 150  # random, not constant


def test_otp_hash_is_keyed_and_bound_to_challenge() -> None:
    challenge = uuid.uuid4()
    digest = hash_otp(KEY, challenge, "123456")
    assert "123456" not in digest
    assert verify_otp(KEY, challenge, "123456", digest)
    assert not verify_otp(KEY, challenge, "123457", digest)
    assert not verify_otp(KEY, uuid.uuid4(), "123456", digest)  # not replayable
    assert not verify_otp(
        SecretStr("another-key-0123456789-abcdefghij"), challenge, "123456", digest
    )


def test_keyed_digest_domain_separation() -> None:
    assert keyed_digest(KEY, "a", "bc") != keyed_digest(KEY, "ab", "c")
    assert identifier_fingerprint(KEY, "email", "x@y.z") != identifier_fingerprint(
        KEY, "phone", "x@y.z"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+91 98765 43210", "+919876543210"),
        ("+1 (415) 555-0100", "+14155550100"),
        ("0091-9876543210", "+919876543210"),
        ("9876543210", None),  # no country code: ambiguous, rejected
        ("+0123456789", None),
        ("+12", None),
        ("phone", None),
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("  Admin@School.Example ", IdentifierKind.EMAIL, "admin@school.example"),
        ("+91 98765 43210", IdentifierKind.PHONE, "+919876543210"),
        ("STU-2024-001", IdentifierKind.UNKNOWN, "stu-2024-001"),
    ],
)
def test_parse_login_identifier(raw: str, kind: IdentifierKind, value: str) -> None:
    assert parse_login_identifier(raw) == (kind, value)


def test_mask_phone_hides_most_digits() -> None:
    assert mask_phone("+919876543210") == "+91********10"


def test_dev_otp_stub_refuses_production_and_staging() -> None:
    for environment in (Environment.PRODUCTION, Environment.STAGING):
        with pytest.raises(RuntimeError):
            DevelopmentConsoleOtpSender(environment)


def test_dev_otp_stub_logs_masked_phone(caplog: pytest.LogCaptureFixture) -> None:
    sender = DevelopmentConsoleOtpSender(Environment.DEVELOPMENT)
    with caplog.at_level(logging.WARNING, logger="app.security.otp_delivery"):
        sender.send(phone="+919876543210", code="424242", expires_in_seconds=300)
    assert "DEVELOPMENT OTP STUB" in caplog.text
    assert "+919876543210" not in caplog.text


def test_build_otp_sender() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert build_otp_sender("disabled", settings) is None
    assert isinstance(
        build_otp_sender("development_console", settings), DevelopmentConsoleOtpSender
    )
