from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.schemas.fields import Code
from app.security.identifiers import normalize_phone

# Hard caps that bound hashing/lookup work regardless of configured policy.
_MAX_PASSWORD_INPUT = 1024
_MAX_REFRESH_TOKEN = 256


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_Request):
    identifier: str = Field(
        min_length=1,
        max_length=320,
        description="Email address or mobile number in international format; for "
        "students (with school_code), the admission number.",
        examples=["admin@school.example"],
    )
    password: SecretStr = Field(min_length=1, max_length=_MAX_PASSWORD_INPUT)
    school_code: Code | None = Field(
        default=None,
        description="Student sign-in only: the school's code. The identifier is then the "
        "student's admission number.",
        examples=["GHS"],
    )


class RefreshRequest(_Request):
    refresh_token: SecretStr = Field(min_length=16, max_length=_MAX_REFRESH_TOKEN)


class LogoutRequest(RefreshRequest):
    pass


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    refresh_token: str
    refresh_expires_in: int = Field(description="Refresh-token lifetime in seconds.")


def _phone(value: str) -> str:
    normalized = normalize_phone(value)
    if normalized is None:
        raise ValueError("Mobile number must be in international format, e.g. +919876543210.")
    return normalized


class OtpRequest(_Request):
    phone: str = Field(min_length=8, max_length=32, examples=["+919876543210"])
    purpose: Literal["login"] = "login"

    _normalize_phone = field_validator("phone")(_phone)


class OtpVerifyRequest(_Request):
    phone: str = Field(min_length=8, max_length=32, examples=["+919876543210"])
    code: SecretStr = Field(min_length=4, max_length=10)
    purpose: Literal["login"] = "login"

    _normalize_phone = field_validator("phone")(_phone)

    @field_validator("code")
    @classmethod
    def _digits_only(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().isdigit():
            raise ValueError("Code must contain digits only.")
        return value


class OtpRequestResponse(BaseModel):
    message: str = "If the number is registered, a code has been sent."
    expires_in: int = Field(description="Seconds until the code expires.")
    resend_after: int = Field(description="Seconds before another code may be requested.")


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    school_id: uuid.UUID | None
    status: str
    last_login_at: datetime | None
    roles: list[str]
    permissions: list[str]
