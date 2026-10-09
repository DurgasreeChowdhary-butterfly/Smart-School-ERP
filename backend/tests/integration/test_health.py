from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app import __version__

pytestmark = pytest.mark.db


def test_health_ok_with_database(db_engine: Engine, client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Smart School Platform"
    assert body["version"] == __version__
    assert datetime.fromisoformat(body["timestamp"]).tzinfo is not None
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["database"]["latency_ms"] >= 0


def test_openapi_schema_served(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]
