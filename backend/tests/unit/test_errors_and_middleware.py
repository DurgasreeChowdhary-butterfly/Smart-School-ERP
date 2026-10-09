"""Error envelope, request-id propagation, security headers and CORS."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError


class _Payload(BaseModel):
    password: str
    age: int


@pytest.fixture
def error_app(app: FastAPI) -> FastAPI:
    @app.get("/_test/not-found")
    def not_found() -> None:
        raise NotFoundError("Student not found.")

    @app.get("/_test/forbidden")
    def forbidden() -> None:
        raise PermissionDeniedError()

    @app.get("/_test/conflict")
    def conflict() -> None:
        raise ConflictError()

    @app.get("/_test/crash")
    def crash() -> None:
        raise RuntimeError("secret internal detail")

    @app.post("/_test/validate")
    def validate(payload: _Payload) -> dict[str, int]:
        return {"age": payload.age}

    return app


@pytest.fixture
def error_client(error_app: FastAPI) -> TestClient:
    return TestClient(error_app, raise_server_exceptions=False)


def _assert_envelope(body: dict[str, object], code: str) -> dict[str, object]:
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert isinstance(error["request_id"], str)
    return error


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/_test/not-found", 404, "not_found"),
        ("/_test/forbidden", 403, "permission_denied"),
        ("/_test/conflict", 409, "conflict"),
    ],
)
def test_app_errors_use_standard_envelope(
    error_client: TestClient, path: str, status: int, code: str
) -> None:
    response = error_client.get(path)
    assert response.status_code == status
    error = _assert_envelope(response.json(), code)
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_returns_envelope(error_client: TestClient) -> None:
    response = error_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    _assert_envelope(response.json(), "not_found")


def test_method_not_allowed_returns_envelope(error_client: TestClient) -> None:
    response = error_client.post("/api/v1/health/live")
    assert response.status_code == 405
    _assert_envelope(response.json(), "method_not_allowed")


def test_unhandled_exception_hides_internal_details(error_client: TestClient) -> None:
    response = error_client.get("/_test/crash")
    assert response.status_code == 500
    error = _assert_envelope(response.json(), "internal_error")
    assert "secret internal detail" not in response.text
    assert error["request_id"] == response.headers["X-Request-ID"]


def test_validation_error_does_not_echo_submitted_values(error_client: TestClient) -> None:
    response = error_client.post(
        "/_test/validate", json={"password": "hunter2-secret", "age": "not-a-number"}
    )
    assert response.status_code == 422
    error = _assert_envelope(response.json(), "validation_error")
    assert error["details"] == [
        {"loc": ["body", "age"], "message": error["details"][0]["message"], "type": "int_parsing"}  # type: ignore[index]
    ]
    assert "hunter2-secret" not in response.text
    assert "not-a-number" not in response.text


def test_malformed_json_body_rejected(error_client: TestClient) -> None:
    response = error_client.post(
        "/_test/validate", content=b"{bad json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    _assert_envelope(response.json(), "validation_error")


def test_request_id_generated_when_absent(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert len(response.headers["X-Request-ID"]) == 32


def test_valid_incoming_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": "trace-abc.123"})
    assert response.headers["X-Request-ID"] == "trace-abc.123"


@pytest.mark.parametrize("bad_id", ["has space", "x" * 65, "inject\tlog", "<script>"])
def test_unsafe_incoming_request_id_is_replaced(client: TestClient, bad_id: str) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": bad_id})
    assert response.headers["X-Request-ID"] != bad_id
    assert len(response.headers["X-Request-ID"]) == 32


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_cors_allows_configured_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health/live",
        headers={"Origin": "http://testserver.local", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://testserver.local"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    response = client.get("/api/v1/health/live", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in response.headers
