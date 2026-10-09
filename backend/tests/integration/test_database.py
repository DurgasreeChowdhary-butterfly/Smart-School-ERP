"""Database foundation: migrations, model conventions, session handling."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import String, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models import Base
from app.models.base import NAMING_CONVENTION, TimestampMixin, UUIDPrimaryKeyMixin
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def test_migrations_at_head(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_heads()
        script = ScriptDirectory.from_config(alembic_config(connection))
        assert set(current) == set(script.get_heads())
        assert len(script.get_heads()) == 1, "migration history must not branch"


def test_models_and_migrations_in_sync(db_engine: Engine) -> None:
    """Fails if a model changed without a corresponding Alembic migration."""
    with db_engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_downgrade_and_upgrade_roundtrip(db_engine: Engine) -> None:
    with db_engine.begin() as connection:
        cfg = alembic_config(connection)
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")


def test_session_executes_queries(db_session: Session) -> None:
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1
    timezone = db_session.execute(text("SHOW TIMEZONE")).scalar_one()
    assert isinstance(timezone, str)


# --- model mixins, exercised on an isolated metadata so they never leak into
# --- the application's schema or Alembic autogenerate.
class _ScratchBase(DeclarativeBase):
    metadata = Base.metadata.__class__(naming_convention=NAMING_CONVENTION)
    type_annotation_map = Base.type_annotation_map


class _Widget(UUIDPrimaryKeyMixin, TimestampMixin, _ScratchBase):
    __tablename__ = "_phase1_widget"
    name: Mapped[str] = mapped_column(String(50), unique=True)


def test_mixins_provide_uuid_pk_and_timezone_aware_timestamps(db_session: Session) -> None:
    connection = db_session.connection()
    _ScratchBase.metadata.create_all(connection)  # inside the rolled-back transaction

    widget = _Widget(name="first")
    db_session.add(widget)
    db_session.flush()
    db_session.refresh(widget)

    assert isinstance(widget.id, uuid.UUID)
    assert isinstance(widget.created_at, datetime)
    assert widget.created_at.tzinfo is not None
    assert widget.updated_at.tzinfo is not None

    columns = {c["name"]: c for c in inspect(connection).get_columns("_phase1_widget")}
    assert str(columns["id"]["type"]) == "UUID"
    assert columns["created_at"]["type"].timezone is True  # type: ignore[attr-defined]

    constraints = inspect(connection).get_unique_constraints("_phase1_widget")
    assert [c["name"] for c in constraints] == ["uq__phase1_widget_name"]
    assert inspect(connection).get_pk_constraint("_phase1_widget")["name"] == "pk__phase1_widget"

    found = db_session.scalars(select(_Widget).where(_Widget.name == "first")).one()
    assert found.id == widget.id
