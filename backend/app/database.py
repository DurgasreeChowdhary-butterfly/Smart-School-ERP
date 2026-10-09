"""Database engine and session management (SQLAlchemy 2.x, synchronous, psycopg 3).

A :class:`Database` instance is created per application (see ``app.main``) and
stored on ``app.state`` rather than as a module-level global, so tests and
tooling can construct isolated instances against different databases.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        echo=settings.db_echo,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            "application_name": "smart-school-backend",
        },
    )


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = build_engine(settings)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session; roll back on error. Callers commit explicitly."""
        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> None:
        """Raise if the database is unreachable."""
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self.engine.dispose()
