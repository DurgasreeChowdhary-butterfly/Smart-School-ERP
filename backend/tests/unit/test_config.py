from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Environment, Settings


def make(**overrides: object) -> Settings:
    # _env_file=None isolates these tests from any local .env file.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults_are_safe() -> None:
    settings = make()
    assert settings.app_env is Environment.DEVELOPMENT
    assert settings.debug is False
    assert settings.cors_allow_origins == []


def test_cors_origins_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:5173/, https://school.example ,")
    settings = make()
    assert settings.cors_allow_origins == ["http://localhost:5173", "https://school.example"]


def test_wildcard_cors_origin_rejected() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        make(cors_allow_origins=["*"])


def test_database_url_escapes_special_characters_and_hides_password() -> None:
    settings = make(postgres_password="p@ss/w:rd#1", postgres_host="db", postgres_db="school")
    url = settings.database_url
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "p@ss/w:rd#1"
    assert "p@ss" not in str(url)  # str() masks the password
    assert url.render_as_string(hide_password=False).startswith(
        "postgresql+psycopg://smart_school:p%40ss%2Fw%3Ard%231@db:5432/school"
    )


def test_password_is_secret_in_repr() -> None:
    settings = make(postgres_password="super-secret-value")
    assert "super-secret-value" not in repr(settings)


@pytest.mark.parametrize("password", ["", "change-me", "change-me-to-a-strong-password"])
def test_production_rejects_placeholder_password(password: str) -> None:
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        make(app_env="production", postgres_password=password)


PROD_SECRETS = {
    "postgres_password": "a-real-secret-9f8e7d",
    "secret_key": "k" * 20 + "-random-hmac-key-0123456789",
    "jwt_secret_key": "j" * 20 + "-random-jwt-key-0123456789",
}


def test_production_rejects_debug() -> None:
    with pytest.raises(ValidationError, match="DEBUG"):
        make(app_env="production", debug=True, **PROD_SECRETS)


def test_production_accepts_valid_configuration() -> None:
    settings = make(app_env="production", **PROD_SECRETS)
    assert settings.is_production


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("key", ["secret_key", "jwt_secret_key"])
@pytest.mark.parametrize("value", ["", "change-me", "too-short-secret"])
def test_deployed_environments_require_strong_secrets(
    environment: str, key: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=key.upper()):
        make(app_env=environment, **{**PROD_SECRETS, key: value})


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_deployed_environments_reject_dev_otp_stub(environment: str) -> None:
    with pytest.raises(ValidationError, match="development stub"):
        make(app_env=environment, otp_delivery_backend="development_console", **PROD_SECRETS)


def test_production_rejects_weak_argon2_memory() -> None:
    with pytest.raises(ValidationError, match="ARGON2"):
        make(app_env="production", argon2_memory_cost_kib=1024, **PROD_SECRETS)


def test_development_generates_ephemeral_keys_when_unset() -> None:
    first, second = make(), make()
    assert len(first.secret_key.get_secret_value()) >= 32
    assert first.jwt_secret_key.get_secret_value() != second.jwt_secret_key.get_secret_value()
    assert first.secret_key.get_secret_value() != first.jwt_secret_key.get_secret_value()


def test_invalid_port_rejected() -> None:
    with pytest.raises(ValidationError):
        make(postgres_port=70000)


def test_validation_errors_do_not_leak_secrets() -> None:
    with pytest.raises(ValidationError) as exc_info:
        make(
            app_env="production",
            debug=True,
            **{**PROD_SECRETS, "postgres_password": "real-secret-do-not-log-42"},
        )
    assert "real-secret-do-not-log-42" not in str(exc_info.value)


def test_development_replaces_placeholder_keys() -> None:
    settings = make(secret_key="change-me", jwt_secret_key="change-me")
    assert settings.secret_key.get_secret_value() != "change-me"
    assert settings.jwt_secret_key.get_secret_value() != "change-me"
