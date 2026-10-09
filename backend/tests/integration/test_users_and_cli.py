"""User provisioning (bootstrap CLI) and an end-to-end secret-leak sweep."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.core.errors import BusinessRuleError, ConflictError
from app.models.audit import AuditLog
from app.repositories.users import UserRepository
from app.security.passwords import PasswordPolicyError, verify_password
from app.security.permissions import RoleName
from app.services.users import create_user_account
from tests.conftest import SCHOOL_A, CapturingOtpSender, UserFactory, login

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]

PASSWORD = "Bootstrap-Passphrase-77"


def _create(session: Session, settings: Settings, **overrides: object) -> object:
    params: dict[str, object] = {
        "full_name": "Asha Rao",
        "email": "Asha.Rao@School.Test",
        "password": PASSWORD,
        "roles": [RoleName.SCHOOL_ADMIN],
        "school_id": SCHOOL_A,
    }
    params.update(overrides)
    return create_user_account(session, settings, **params)  # type: ignore[arg-type]


def test_plaintext_password_is_never_persisted(
    db_session: Session, test_settings: Settings
) -> None:
    user = _create(db_session, test_settings)
    row = str(db_session.execute(text("SELECT * FROM users")).all())
    assert PASSWORD not in row
    assert user.password_hash is not None  # type: ignore[attr-defined]
    assert user.password_hash.startswith("$argon2id$")  # type: ignore[attr-defined]
    assert verify_password(PASSWORD, user.password_hash)  # type: ignore[attr-defined]
    assert user.email == "asha.rao@school.test"  # type: ignore[attr-defined]


def test_creation_is_audited(db_session: Session, test_settings: Settings) -> None:
    user = _create(db_session, test_settings)
    event = db_session.scalars(select(AuditLog).where(AuditLog.action == "user.created")).one()
    assert event.resource_id == str(user.id)  # type: ignore[attr-defined]
    assert event.details == {"roles": ["SCHOOL_ADMIN"], "source": "cli"}


def test_duplicate_email_conflicts(db_session: Session, test_settings: Settings) -> None:
    _create(db_session, test_settings)
    with pytest.raises(ConflictError):
        _create(db_session, test_settings, email="asha.rao@school.test")


def test_duplicate_phone_conflicts(db_session: Session, test_settings: Settings) -> None:
    _create(db_session, test_settings, phone="+919812345678")
    with pytest.raises(ConflictError):
        _create(db_session, test_settings, email="b@school.test", phone="+91 98123 45678")


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "  "},
        {"email": None, "phone": None},
        {"email": "not-an-email"},
        {"phone": "12345"},
        {"school_id": None},  # school-level role without a school
    ],
)
def test_invalid_input_rejected(
    db_session: Session, test_settings: Settings, overrides: dict[str, object]
) -> None:
    with pytest.raises(BusinessRuleError):
        _create(db_session, test_settings, **overrides)


def test_weak_password_rejected(db_session: Session, test_settings: Settings) -> None:
    with pytest.raises(PasswordPolicyError):
        _create(db_session, test_settings, password="short")


def test_otp_only_account_has_no_password(db_session: Session, test_settings: Settings) -> None:
    user = _create(
        db_session,
        test_settings,
        email=None,
        phone="+919800000555",
        password=None,
        roles=[RoleName.PARENT],
    )
    assert user.password_hash is None  # type: ignore[attr-defined]


def test_database_rejects_non_normalised_identifiers(db_session: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO users (id, full_name, email) "
                "VALUES (gen_random_uuid(), 'x', 'UPPER@X.Y')"
            )
        )


def test_is_active_is_derived_from_status(db_session: Session, test_settings: Settings) -> None:
    user = _create(db_session, test_settings)
    db_session.execute(
        text("UPDATE users SET status = 'suspended' WHERE id = :id"),
        {"id": user.id},  # type: ignore[attr-defined]
    )
    assert (
        db_session.execute(
            text("SELECT is_active FROM users WHERE id = :id"),
            {"id": user.id},  # type: ignore[attr-defined]
        ).scalar_one()
        is False
    )


def test_cli_parser_never_accepts_password_argument() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["create-user", "--full-name", "X", "--role", "SUPER_ADMIN", "--password", "pw"]
        )


def test_cli_reads_password_from_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    test_settings: Settings,
    db_engine: object,
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.setenv("SSP_TEST_PASSWORD", PASSWORD)
    email = "cli-root@platform.test"
    try:
        exit_code = cli.main(
            [
                "create-user",
                "--full-name",
                "Platform Root",
                "--email",
                email,
                "--role",
                "SUPER_ADMIN",
                "--password-env",
                "SSP_TEST_PASSWORD",
            ]
        )
        out = capsys.readouterr()
        assert exit_code == 0
        assert "Created user" in out.out
        assert PASSWORD not in out.out + out.err

        # Running again reports the conflict cleanly instead of crashing.
        assert (
            cli.main(
                [
                    "create-user",
                    "--full-name",
                    "Again",
                    "--email",
                    email,
                    "--role",
                    "SUPER_ADMIN",
                    "--password-env",
                    "SSP_TEST_PASSWORD",
                ]
            )
            == 1
        )
        assert "already exists" in capsys.readouterr().err
    finally:
        # The CLI commits for real; remove the row so the shared test DB stays clean.
        from app.database import Database

        database = Database(test_settings)
        with database.session() as session:
            user = UserRepository(session).get_by_email(email)
            if user is not None:
                session.execute(
                    text("DELETE FROM audit_logs WHERE resource_id = :id"), {"id": str(user.id)}
                )
                session.delete(user)
                session.commit()
        database.dispose()


def test_cli_missing_password_env(monkeypatch: pytest.MonkeyPatch, test_settings: Settings) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    monkeypatch.delenv("SSP_UNSET_VAR", raising=False)
    with pytest.raises(SystemExit, match="not set"):
        cli.main(
            [
                "create-user",
                "--full-name",
                "X",
                "--email",
                "x@y.test",
                "--role",
                "SUPER_ADMIN",
                "--password-env",
                "SSP_UNSET_VAR",
            ]
        )


def test_no_secret_reaches_any_table(
    db_client: TestClient,
    db_session: Session,
    make_user: UserFactory,
    otp_outbox: CapturingOtpSender,
) -> None:
    """Exercise every auth flow, then scan all auth/audit tables for any secret."""
    make_user(email="sweep@school.test")
    make_user(roles=(RoleName.PARENT,), phone="+919800000777", password=None)

    secrets: list[str] = ["Correct-Horse-Battery-9", "Wrong-Password-Sweep-1"]
    db_client.post(
        "/api/v1/auth/login", json={"identifier": "sweep@school.test", "password": secrets[1]}
    )
    tokens = login(db_client, "sweep@school.test")
    rotated = db_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).json()
    db_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})  # reuse
    db_client.post("/api/v1/auth/otp/request", json={"phone": "+919800000777"})
    code = otp_outbox.last_code("+919800000777")
    otp_tokens = db_client.post(
        "/api/v1/auth/otp/verify", json={"phone": "+919800000777", "code": code}
    ).json()
    db_client.post("/api/v1/auth/logout", json={"refresh_token": otp_tokens["refresh_token"]})
    secrets += [
        tokens["access_token"],
        tokens["refresh_token"],
        rotated["access_token"],
        rotated["refresh_token"],
        otp_tokens["access_token"],
        otp_tokens["refresh_token"],
    ]

    dump = ""
    for table in (
        "users",
        "auth_sessions",
        "refresh_tokens",
        "otp_challenges",
        "login_attempts",
        "audit_logs",
    ):
        dump += str(db_session.execute(text(f"SELECT * FROM {table}")).all())  # noqa: S608 - fixed names
    for secret in secrets:
        assert secret not in dump
    # The 6-digit OTP could coincidentally appear inside a hex digest; check structured fields.
    audit_dump = str(db_session.execute(text("SELECT metadata, user_agent FROM audit_logs")).all())
    assert f"'{code}'" not in audit_dump
