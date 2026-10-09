"""Application configuration loaded from environment variables.

Values are read (in increasing priority) from the repository-root ``.env``,
``backend/.env`` and the process environment. Secrets must never be committed;
see ``.env.example`` for the full list of supported variables.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent

# Placeholder values shipped in .env.example. They must never reach production.
_PLACEHOLDER_SECRETS = frozenset({"", "change-me", "changeme", "password", "postgres"})


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
OtpDeliveryBackend = Literal["disabled", "development_console"]

_MIN_SECRET_LENGTH = 32
# OWASP minimum for Argon2id is 19 MiB; production must not go below it.
_MIN_PRODUCTION_ARGON2_MEMORY_KIB = 19_456


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return lowered in _PLACEHOLDER_SECRETS or "change-me" in lowered


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Validation errors must never echo raw input: it can contain secrets.
        hide_input_in_errors=True,
    )

    # --- application -------------------------------------------------------
    app_name: str = "Smart School Platform"
    app_env: Environment = Environment.DEVELOPMENT
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    api_docs_enabled: bool = True

    # --- logging -----------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # --- HTTP --------------------------------------------------------------
    # Comma-separated list in the environment, e.g. "http://localhost:5173".
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- database ----------------------------------------------------------
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = "smart_school"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "smart_school"

    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: int = Field(default=30, ge=1)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_echo: bool = False

    # --- security: keys ------------------------------------------------------
    # HMAC key for hashing OTP codes and login identifiers (rate-limit keys).
    secret_key: SecretStr = SecretStr("")
    # Signing key for JWT access tokens.
    jwt_secret_key: SecretStr = SecretStr("")
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "smart-school-platform"
    jwt_audience: str = "smart-school-api"

    # --- security: tokens and sessions -------------------------------------
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_ttl_days: int = Field(default=7, ge=1, le=90)
    # Absolute lifetime of a login session (refresh-token family), regardless of rotation.
    session_max_age_days: int = Field(default=30, ge=1, le=365)

    # --- security: passwords -----------------------------------------------
    password_min_length: int = Field(default=10, ge=8, le=64)
    password_max_length: int = Field(default=128, ge=64, le=1024)
    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_cost_kib: int = Field(default=65_536, ge=1_024, le=1_048_576)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)

    # --- security: login throttling ----------------------------------------
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_failure_window_minutes: int = Field(default=15, ge=1, le=1440)
    login_max_failures_per_ip: int = Field(default=50, ge=1, le=10_000)

    # --- security: OTP -------------------------------------------------------
    otp_length: int = Field(default=6, ge=4, le=10)
    otp_ttl_seconds: int = Field(default=300, ge=60, le=1800)
    otp_max_attempts: int = Field(default=5, ge=1, le=10)
    otp_resend_cooldown_seconds: int = Field(default=60, ge=0, le=3600)
    otp_max_requests_per_hour: int = Field(default=5, ge=1, le=100)
    otp_max_requests_per_ip_per_hour: int = Field(default=30, ge=1, le=10_000)
    # "development_console" is a DEVELOPMENT STUB that logs codes; it is rejected in
    # staging/production. "disabled" makes OTP sign-in return 503.
    otp_delivery_backend: OtpDeliveryBackend = "disabled"

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_allow_origins")
    @classmethod
    def _reject_wildcard_origin(cls, value: list[str]) -> list[str]:
        # Credentialed CORS (cookies / auth headers) is incompatible with "*";
        # origins must always be listed explicitly.
        if "*" in value:
            raise ValueError("wildcard CORS origin '*' is not allowed; list origins explicitly")
        return [origin.rstrip("/") for origin in value]

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Self:
        if self.app_env is Environment.PRODUCTION:
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if _is_placeholder(self.postgres_password.get_secret_value()):
                raise ValueError("POSTGRES_PASSWORD must be set to a real secret in production")
            if self.argon2_memory_cost_kib < _MIN_PRODUCTION_ARGON2_MEMORY_KIB:
                raise ValueError("ARGON2_MEMORY_COST_KIB is too low for production")

        if self.app_env in (Environment.PRODUCTION, Environment.STAGING):
            for name in ("secret_key", "jwt_secret_key"):
                value: SecretStr = getattr(self, name)
                raw = value.get_secret_value()
                if _is_placeholder(raw) or len(raw) < _MIN_SECRET_LENGTH:
                    raise ValueError(
                        f"{name.upper()} must be a random secret of at least "
                        f"{_MIN_SECRET_LENGTH} characters in {self.app_env.value}"
                    )
            if self.otp_delivery_backend == "development_console":
                raise ValueError(
                    "OTP_DELIVERY_BACKEND=development_console is a development stub and is "
                    f"not allowed in {self.app_env.value}"
                )
        else:
            # Development/test convenience: an unset or placeholder key becomes an ephemeral
            # random key, so tokens simply stop validating after a restart.
            for name in ("secret_key", "jwt_secret_key"):
                if _is_placeholder(getattr(self, name).get_secret_value()):
                    setattr(self, name, SecretStr(secrets.token_urlsafe(48)))
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION

    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL; ``URL.create`` safely escapes special characters."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value() or None,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
