"""Health endpoints when the database is unreachable (no PostgreSQL required).

The app is pointed at a closed local port, so the real driver fails to connect;
nothing is mocked.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _unreachable_db_client(test_settings: Settings) -> TestClient:
    settings = test_settings.model_copy(
        update={"postgres_host": "127.0.0.1", "postgres_port": 1, "db_connect_timeout_seconds": 1}
    )
    return TestClient(create_app(settings))


def test_health_reports_503_when_database_unreachable(test_settings: Settings) -> None:
    with _unreachable_db_client(test_settings) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["database"]["status"] == "unavailable"
    # Driver/connection details must not leak to clients.
    assert "127.0.0.1" not in response.text
    assert "connection" not in response.text.lower()


def test_liveness_ok_even_when_database_unreachable(test_settings: Settings) -> None:
    with _unreachable_db_client(test_settings) as client:
        response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["Cache-Control"] == "no-store"


def test_docs_disabled_when_configured(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"api_docs_enabled": False})
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/docs").status_code == 404
        assert client.get("/api/v1/openapi.json").status_code == 404
