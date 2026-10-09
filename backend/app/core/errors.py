"""Application exceptions and their mapping to the standard error envelope.

Services raise :class:`AppError` subclasses; they are translated to HTTP
responses here so business logic never deals with HTTP concerns. Internal
details (stack traces, SQL, submitted values) are never sent to clients.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.common import ErrorBody, ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class AppError(Exception):
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."
    headers: dict[str, str] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        details: list[ErrorDetail] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.message = message or self.message
        self.details = details or []
        if headers is not None:
            self.headers = {**(self.headers or {}), **headers}
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    message = "The request could not be processed."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_required"
    message = "Authentication is required."
    headers = {"WWW-Authenticate": "Bearer"}  # noqa: RUF012 - immutable by convention


class InvalidCredentialsError(AuthenticationError):
    """Deliberately generic: never reveals whether the account exists or why it failed."""

    code = "invalid_credentials"
    message = "Invalid credentials."


class InvalidTokenError(AuthenticationError):
    code = "invalid_token"
    message = "The token is invalid or has expired."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class BusinessRuleError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "business_rule_violation"
    message = "The request violates a business rule."


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please try again later."

    def __init__(self, message: str | None = None, *, retry_after_seconds: int):
        super().__init__(message, headers={"Retry-After": str(max(1, retry_after_seconds))})
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "The service is temporarily unavailable."


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=details or [], request_id=request_id)
    )
    response_headers = dict(headers or {})
    if request_id:
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers=response_headers,
    )


def _http_code(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_").replace("-", "_")
    except ValueError:
        return "http_error"


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.error("application error", extra={"error_code": exc.code}, exc_info=exc)
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return error_response(
        request,
        status_code=exc.status_code,
        code=_http_code(exc.status_code),
        message=message,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Deliberately omit the submitted "input" values: they may contain secrets.
    details = [
        ErrorDetail(
            loc=[part for part in err.get("loc", ()) if isinstance(part, str | int)],
            message=str(err.get("msg", "Invalid value")),
            type=str(err.get("type")) if err.get("type") else None,
        )
        for err in exc.errors()
    ]
    return error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="The request contains invalid data.",
        details=details,
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled exception",
        extra={"path": request.url.path, "method": request.method},
        exc_info=exc,
    )
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="An unexpected error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    handlers: dict[type[Exception], Any] = {
        AppError: _handle_app_error,
        StarletteHTTPException: _handle_http_exception,
        RequestValidationError: _handle_validation_error,
        Exception: _handle_unexpected,
    }
    for exc_class, handler in handlers.items():
        app.add_exception_handler(exc_class, handler)
