"""Shared FastAPI dependencies."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.errors import AuthenticationError, InvalidTokenError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.database import Database
from app.models.user import UserStatus
from app.repositories.auth import AuthSessionRepository
from app.repositories.users import UserRepository
from app.security.otp_delivery import OtpSender
from app.security.permissions import PermissionName, RoleName
from app.security.principal import Principal
from app.security.school_scope import SCHOOL_HEADER, SchoolScope, resolve_school_scope
from app.security.tokens import TokenValidationError, decode_access_token

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 100


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


DatabaseDep = Annotated[Database, Depends(get_database)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_db_session(database: DatabaseDep) -> Iterator[Session]:
    with database.session() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db_session)]


@dataclass(frozen=True, slots=True)
class PaginationParams:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def get_pagination(
    page: Annotated[int, Query(ge=1, le=100_000, description="1-based page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page")] = 20,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


Pagination = Annotated[PaginationParams, Depends(get_pagination)]


# --------------------------------------------------------------------------- context
def get_request_context(request: Request) -> RequestContext:
    return RequestContext.from_request(request)


RequestCtx = Annotated[RequestContext, Depends(get_request_context)]


def get_otp_sender(request: Request) -> OtpSender | None:
    sender: OtpSender | None = request.app.state.otp_sender
    return sender


OtpSenderDep = Annotated[OtpSender | None, Depends(get_otp_sender)]


# -------------------------------------------------------------------- authentication
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")


def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: SettingsDep,
    session: DbSession,
) -> Principal:
    """Authenticate the bearer token against the database on every request.

    Beyond signature/expiry, the session must still be live and the user active, so
    logout, refresh-token reuse detection and deactivation take effect immediately.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError()
    try:
        claims = decode_access_token(settings, credentials.credentials)
    except TokenValidationError as exc:
        logger.info("access token rejected", extra={"reason": str(exc)})
        raise InvalidTokenError() from exc

    auth_session = AuthSessionRepository(session).get(claims.session_id)
    if (
        auth_session is None
        or auth_session.user_id != claims.user_id
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= datetime.now(UTC)
    ):
        raise InvalidTokenError()

    users = UserRepository(session)
    user = users.get(claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE or not users.school_is_active(user):
        # An inactive school suspends its users' sessions immediately.
        raise InvalidTokenError()

    return Principal(
        user_id=user.id,
        session_id=auth_session.id,
        school_id=user.school_id,
        roles=users.role_names(user.id),
        permissions=users.permission_names(user.id),
    )


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]

# Alias that reads well at call sites that only need "any signed-in user".
require_authenticated_user = get_current_principal


# --------------------------------------------------------------------- authorization
def require_permission(*permissions: PermissionName) -> Callable[[Principal], Principal]:
    """Dependency factory: the caller must hold **all** listed permissions (else 403)."""
    required = frozenset(permissions)

    def dependency(principal: CurrentPrincipal) -> Principal:
        missing = required - principal.permissions
        if missing:
            logger.info(
                "permission denied",
                extra={"user_id": str(principal.user_id), "missing": sorted(missing)},
            )
            raise PermissionDeniedError()
        return principal

    return dependency


def require_role(*roles: RoleName) -> Callable[[Principal], Principal]:
    """Dependency factory: the caller must hold **any** listed role (else 403).

    Prefer :func:`require_permission`; role checks are for the rare case where the
    role itself, not a capability, is the rule.
    """
    allowed = frozenset(roles)

    def dependency(principal: CurrentPrincipal) -> Principal:
        if not allowed & principal.roles:
            raise PermissionDeniedError()
        return principal

    return dependency


def require_any_permission(*permissions: PermissionName) -> Callable[[Principal], Principal]:
    """Dependency factory: the caller must hold **at least one** listed permission.

    Used where different roles reach the same endpoint through different
    capabilities (e.g. ``student.read`` / ``student.read_self`` /
    ``parent.read_child``); the service then narrows results to that capability's
    resource scope.
    """
    accepted = frozenset(permissions)

    def dependency(principal: CurrentPrincipal) -> Principal:
        if not accepted & principal.permissions:
            logger.info(
                "permission denied",
                extra={"user_id": str(principal.user_id), "any_of": sorted(accepted)},
            )
            raise PermissionDeniedError()
        return principal

    return dependency


# ---------------------------------------------------------------------- school scope
def require_school_permission(
    *permissions: PermissionName,
) -> Callable[..., SchoolScope]:
    """Dependency factory: authenticate, check permissions, resolve the target school.

    Every school-scoped endpoint uses this, so tenant isolation is applied
    uniformly (see :mod:`app.security.school_scope`).
    """
    return _school_scope_dependency(require_permission(*permissions))


def require_school_any_permission(
    *permissions: PermissionName,
) -> Callable[..., SchoolScope]:
    """Like :func:`require_school_permission`, but any one permission suffices."""
    return _school_scope_dependency(require_any_permission(*permissions))


def _school_scope_dependency(
    check_permissions: Callable[[Principal], Principal],
) -> Callable[..., SchoolScope]:

    # Dependencies are passed as defaults (not Annotated) because this module uses
    # postponed annotations, and FastAPI cannot resolve the closure variable
    # ``check_permissions`` from a string annotation.
    def dependency(
        principal: Principal = Depends(check_permissions),
        session: Session = Depends(get_db_session),
        x_school_id: uuid.UUID | None = Header(
            default=None,
            alias=SCHOOL_HEADER,
            description="Target school. Required for SUPER_ADMIN; optional for others "
            "(must then equal their own school).",
        ),
    ) -> SchoolScope:
        return resolve_school_scope(session, principal, x_school_id)

    return dependency
