"""Migration 0003 preserves Phase 2 authentication data and never invents schools.

Each test runs inside one outer transaction that is rolled back (PostgreSQL DDL
is transactional), so the shared test database is left at ``head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.security.passwords import Argon2Params, hash_password
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


@pytest.fixture
def connection(db_engine: Engine) -> Iterator[Connection]:
    conn = db_engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


def _revision(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


def _insert_auth_user(
    conn: Connection, settings: Settings, school_id: uuid.UUID | None
) -> uuid.UUID:
    user_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO users (id, full_name, email, password_hash, school_id) "
            "VALUES (:id, 'Existing Admin', 'existing@x.test', :h, :s)"
        ),
        {
            "id": user_id,
            "h": hash_password("Pre-Existing-Pass-1", Argon2Params(1, 1024, 1)),
            "s": school_id,
        },
    )
    conn.execute(
        text(
            "INSERT INTO user_roles (user_id, role_id) "
            "SELECT :u, id FROM roles WHERE name = 'SUPER_ADMIN'"
        ),
        {"u": user_id},
    )
    conn.execute(
        text(
            "INSERT INTO auth_sessions (id, user_id, auth_method, expires_at) "
            "VALUES (gen_random_uuid(), :u, 'password', now() + interval '1 day')"
        ),
        {"u": user_id},
    )
    return user_id


def test_upgrade_downgrade_reupgrade_preserves_auth_data(
    connection: Connection, test_settings: Settings
) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0002_auth_rbac")
    assert _revision(connection) == "0002_auth_rbac"
    assert connection.execute(text("SELECT to_regclass('schools')")).scalar() is None

    user_id = _insert_auth_user(connection, test_settings, None)
    before = connection.execute(
        text("SELECT full_name, email, password_hash FROM users WHERE id = :u"), {"u": user_id}
    ).one()

    command.upgrade(cfg, "0003_school_academic")
    assert _revision(connection) == "0003_school_academic"
    after = connection.execute(
        text("SELECT full_name, email, password_hash, school_id FROM users WHERE id = :u"),
        {"u": user_id},
    ).one()
    assert tuple(after[:3]) == tuple(before)
    assert after.school_id is None
    roles = (
        connection.execute(
            text(
                "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                "WHERE ur.user_id = :u"
            ),
            {"u": user_id},
        )
        .scalars()
        .all()
    )
    assert roles == ["SUPER_ADMIN"]
    assert (
        connection.execute(
            text("SELECT count(*) FROM auth_sessions WHERE user_id = :u"), {"u": user_id}
        ).scalar()
        == 1
    )

    # Downgrade keeps users (and their school_id values); re-upgrade works again.
    command.downgrade(cfg, "0002_auth_rbac")
    assert (
        connection.execute(
            text("SELECT count(*) FROM users WHERE id = :u"), {"u": user_id}
        ).scalar()
        == 1
    )
    command.upgrade(cfg, "0003_school_academic")
    assert _revision(connection) == "0003_school_academic"


def test_upgrade_refuses_users_with_unknown_school(
    connection: Connection, test_settings: Settings
) -> None:
    cfg = alembic_config(connection)
    command.downgrade(cfg, "0002_auth_rbac")
    orphan_school = uuid.uuid4()
    user_id = _insert_auth_user(connection, test_settings, orphan_school)

    savepoint = connection.begin_nested()
    with pytest.raises(RuntimeError, match="reference a school_id that does not exist"):
        command.upgrade(cfg, "0003_school_academic")
    savepoint.rollback()

    # Nothing was changed or invented.
    assert _revision(connection) == "0002_auth_rbac"
    assert (
        connection.execute(
            text("SELECT school_id FROM users WHERE id = :u"), {"u": user_id}
        ).scalar()
        == orphan_school
    )
