"""One-time password generation and hashing.

OTPs are low-entropy (e.g. 6 digits), so a plain hash could be brute-forced from a
database dump. Codes are therefore stored as HMAC-SHA256 keyed with
``SECRET_KEY`` and bound to the challenge id, so a hash is useless on its own and
cannot be replayed against another challenge.
"""

from __future__ import annotations

import secrets
import uuid

from pydantic import SecretStr

from app.security.hashing import constant_time_equals, keyed_digest


def generate_otp(length: int) -> str:
    return f"{secrets.randbelow(10**length):0{length}d}"


def hash_otp(key: SecretStr, challenge_id: uuid.UUID, code: str) -> str:
    return keyed_digest(key, "otp", str(challenge_id), code)


def verify_otp(key: SecretStr, challenge_id: uuid.UUID, code: str, code_hash: str) -> bool:
    return constant_time_equals(hash_otp(key, challenge_id, code), code_hash)
