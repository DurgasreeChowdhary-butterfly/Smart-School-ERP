"""Shared pytest fixtures.

Integration tests run against a real PostgreSQL database named
``TEST_POSTGRES_DB`` (default: ``<POSTGRES_DB>_test``). It is created on demand
and migrated to ``head`` once per session. The name must end in ``_test`` so a
misconfiguration can never point the suite at a development/production DB.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_db_session
from app.config import Environment, Settings
from app.database import build_engine
from app.main import create_app
from app.models.school import School
from app.models.user import User, UserStatus
from app.security.permissions import RoleName
from app.services.users import create_user_account

DEFAULT_PASSWORD = "Correct-Horse-Battery-9"
SCHOOL_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
SCHOOL_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    base = Settings()
    db_name = os.environ.get("TEST_POSTGRES_DB") or f"{base.postgres_db}_test"
    if not db_name.endswith("_test"):
        pytest.exit(f"Refusing to run: test database {db_name!r} must end with '_test'.")
    return Settings(
        app_env=Environment.TEST,
        postgres_db=db_name,
        log_json=False,
        log_level="WARNING",
        cors_allow_origins=["http://testserver.local"],
        secret_key="test-hmac-key-not-for-production-0123456789",
        jwt_secret_key="test-jwt-key-not-for-production-0123456789",
        # Cheap hashing keeps the suite fast; production parameters are validated separately.
        argon2_time_cost=1,
        argon2_memory_cost_kib=1024,
        argon2_parallelism=1,
        otp_delivery_backend="disabled",
    )


def _ensure_database_exists(settings: Settings) -> None:
    admin_url = settings.database_url.set(database="postgres")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": settings.postgres_db},
            ).scalar()
            if not exists:
                # Name is validated above (suffix) and quoted by the dialect.
                quoted = conn.dialect.identifier_preparer.quote(settings.postgres_db)
                conn.execute(text(f"CREATE DATABASE {quoted}"))
    except OperationalError as exc:
        pytest.fail(
            "PostgreSQL is not reachable for integration tests "
            f"({settings.postgres_host}:{settings.postgres_port}). "
            "Start it with `docker compose up -d db`.\n"
            f"{exc.orig}",
            pytrace=False,
        )
    finally:
        engine.dispose()


def alembic_config(connection: object) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def db_engine(test_settings: Settings) -> Iterator[Engine]:
    _ensure_database_exists(test_settings)
    engine = build_engine(test_settings)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Session bound to an outer transaction that is always rolled back."""
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    return create_app(test_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


TEST_CLIENT_IP = "203.0.113.10"  # TEST-NET-3 documentation address


@pytest.fixture
def db_client(app: FastAPI, db_engine: Engine, db_session: Session) -> Iterator[TestClient]:
    """Client whose request-scoped DB session shares the rolled-back test transaction."""
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app, client=(TEST_CLIENT_IP, 50000)) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@dataclass
class CapturingOtpSender:
    """TEST-ONLY OTP channel: records codes in memory so tests can complete the flow.

    Installed per test on ``app.state``; production code paths are unchanged.
    """

    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, *, phone: str, code: str, expires_in_seconds: int) -> None:
        self.sent.append((phone, code))

    def last_code(self, phone: str) -> str:
        return next(code for sent_phone, code in reversed(self.sent) if sent_phone == phone)


@pytest.fixture
def otp_outbox(app: FastAPI) -> CapturingOtpSender:
    sender = CapturingOtpSender()
    app.state.otp_sender = sender
    return sender


class UserFactory(Protocol):
    def __call__(
        self,
        *,
        roles: tuple[RoleName, ...] = ...,
        email: str | None = ...,
        phone: str | None = ...,
        password: str | None = ...,
        status: UserStatus = ...,
        school_id: uuid.UUID | None = ...,
    ) -> User: ...


@pytest.fixture
def seed_schools(db_session: Session) -> tuple[School, School]:
    """Two schools (A and B) for tenant-isolation tests; rolled back after each test."""
    schools = (
        School(id=SCHOOL_A, name="School A", code="SCH-A"),
        School(id=SCHOOL_B, name="School B", code="SCH-B"),
    )
    db_session.add_all(schools)
    db_session.flush()
    return schools


@pytest.fixture
def make_user(
    db_session: Session, test_settings: Settings, seed_schools: tuple[School, School]
) -> UserFactory:
    def factory(
        *,
        roles: tuple[RoleName, ...] = (RoleName.SCHOOL_ADMIN,),
        email: str | None = None,
        phone: str | None = None,
        password: str | None = DEFAULT_PASSWORD,
        status: UserStatus = UserStatus.ACTIVE,
        school_id: uuid.UUID | None = SCHOOL_A,
    ) -> User:
        if email is None and phone is None:
            email = f"user-{uuid.uuid4().hex[:10]}@school.test"
        user = create_user_account(
            db_session,
            test_settings,
            full_name="Test User",
            email=email,
            phone=phone,
            password=password,
            roles=list(roles),
            school_id=None if RoleName.SUPER_ADMIN in roles else school_id,
            source="test",
        )
        user.status = status.value
        db_session.flush()
        return user

    return factory


def login(client: TestClient, identifier: str, password: str = DEFAULT_PASSWORD) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/login", json={"identifier": identifier, "password": password}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class HeadersFactory(Protocol):
    def __call__(
        self, role: RoleName, school_id: uuid.UUID | None = ..., *, select_school: bool = ...
    ) -> dict[str, str]: ...


@pytest.fixture
def auth_headers(db_client: TestClient, make_user: UserFactory) -> HeadersFactory:
    """Sign in a fresh user with ``role`` and return request headers.

    SUPER_ADMIN users have no school; ``school_id`` then becomes the ``X-School-ID``
    selection (unless ``select_school=False``).
    """

    def factory(
        role: RoleName, school_id: uuid.UUID | None = SCHOOL_A, *, select_school: bool = True
    ) -> dict[str, str]:
        user = make_user(roles=(role,), school_id=school_id or SCHOOL_A)
        assert user.email is not None
        headers = bearer(login(db_client, user.email)["access_token"])
        if role is RoleName.SUPER_ADMIN and select_school and school_id is not None:
            headers["X-School-ID"] = str(school_id)
        return headers

    return factory
