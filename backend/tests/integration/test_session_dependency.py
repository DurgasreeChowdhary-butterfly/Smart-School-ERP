"""The real request-scoped session dependency (no overrides)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api.deps import DbSession

pytestmark = pytest.mark.db


@pytest.fixture
def session_app(app: FastAPI) -> FastAPI:
    @app.get("/_test/db")
    def db_value(session: DbSession) -> dict[str, int]:
        return {"value": session.execute(text("SELECT 41 + 1")).scalar_one()}

    @app.get("/_test/db-error")
    def db_error(session: DbSession) -> None:
        session.execute(text("SELECT 1"))
        raise RuntimeError("fails mid-request")

    return app


def test_request_session_executes_queries(db_engine: Engine, session_app: FastAPI) -> None:
    with TestClient(session_app) as client:
        response = client.get("/_test/db")
    assert response.status_code == 200
    assert response.json() == {"value": 42}


def test_request_session_released_after_error(db_engine: Engine, session_app: FastAPI) -> None:
    with TestClient(session_app, raise_server_exceptions=False) as client:
        assert client.get("/_test/db-error").status_code == 500
        # Pool connections were returned/rolled back: subsequent requests still work.
        for _ in range(3):
            assert client.get("/_test/db").json() == {"value": 42}
        pool = session_app.state.db.engine.pool
        assert pool.checkedout() == 0
