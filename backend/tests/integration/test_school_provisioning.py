"""School scope edge cases, user provisioning against schools, and the create-school CLI."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import cli
from app.config import Settings
from app.core.errors import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.database import Database
from app.models.school import School
from app.security.permissions import RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolInactiveError, resolve_school_scope
from app.services.users import create_user_account
from tests.conftest import SCHOOL_A, SCHOOL_B

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]


def _principal(school_id: uuid.UUID | None, *roles: RoleName) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        school_id=school_id,
        roles=frozenset(r.value for r in roles),
        permissions=frozenset(),
    )


def test_school_account_without_school_is_denied(db_session: Session) -> None:
    with pytest.raises(PermissionDeniedError, match="not associated with a school"):
        resolve_school_scope(db_session, _principal(None, RoleName.SCHOOL_ADMIN), None)


def test_inactive_school_is_unavailable_to_its_users(db_session: Session) -> None:
    """Defence in depth: authentication already rejects such users (see test_school_api)."""
    db_session.get(School, SCHOOL_A).is_active = False  # type: ignore[union-attr]
    db_session.flush()
    with pytest.raises(SchoolInactiveError):
        resolve_school_scope(db_session, _principal(SCHOOL_A, RoleName.FACULTY), None)
    # The platform administrator can still reach it.
    scope = resolve_school_scope(db_session, _principal(None, RoleName.SUPER_ADMIN), SCHOOL_A)
    assert scope.school_id == SCHOOL_A
    # Other schools are unaffected.
    assert resolve_school_scope(db_session, _principal(SCHOOL_B, RoleName.FACULTY), None)


def test_super_admin_unknown_school(db_session: Session) -> None:
    with pytest.raises(NotFoundError):
        resolve_school_scope(db_session, _principal(None, RoleName.SUPER_ADMIN), uuid.uuid4())


def test_user_cannot_be_created_for_unknown_or_inactive_school(
    db_session: Session, test_settings: Settings
) -> None:
    common = {"full_name": "T", "password": None, "roles": [RoleName.FACULTY]}
    with pytest.raises(BusinessRuleError, match="School not found"):
        create_user_account(
            db_session, test_settings, email="a@x.test", school_id=uuid.uuid4(), **common
        )  # type: ignore[arg-type]
    db_session.get(School, SCHOOL_B).is_active = False  # type: ignore[union-attr]
    with pytest.raises(BusinessRuleError, match="School is inactive"):
        create_user_account(
            db_session, test_settings, email="b@x.test", school_id=SCHOOL_B, **common
        )  # type: ignore[arg-type]


def test_cli_create_school(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], test_settings: Settings
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: test_settings)
    code = f"CLI-{uuid.uuid4().hex[:6].upper()}"
    try:
        args = [
            "create-school",
            "--name",
            "CLI School",
            "--code",
            code.lower(),
            "--timezone",
            "Asia/Kolkata",
        ]
        assert cli.main(args) == 0
        assert f"({code})" in capsys.readouterr().out  # code normalised to upper case

        assert cli.main(args) == 1  # duplicate code
        assert "already exists" in capsys.readouterr().err

        bad = ["create-school", "--name", "X", "--code", "X1", "--timezone", "Mars/Base"]
        assert cli.main(bad) == 1
        assert "timezone" in capsys.readouterr().err
    finally:
        database = Database(test_settings)
        with database.session() as session:
            session.execute(
                text(
                    "DELETE FROM audit_logs WHERE resource_id IN "
                    "(SELECT id::text FROM schools WHERE code = :c)"
                ),
                {"c": code},
            )
            session.execute(text("DELETE FROM schools WHERE code = :c"), {"c": code})
            session.commit()
        database.dispose()
