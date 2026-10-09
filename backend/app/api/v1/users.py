"""User endpoints. Phase 2 exposes role management only; account CRUD arrives later."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, RequestCtx, require_permission
from app.schemas.common import ErrorResponse
from app.schemas.rbac import UserRolesRead, UserRolesUpdate
from app.security.permissions import PermissionName
from app.security.principal import Principal
from app.services.rbac import RbacService

router = APIRouter(prefix="/users", tags=["users"])
_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get("/{user_id}/roles", response_model=UserRolesRead, responses=_ERRORS)
def get_user_roles(
    user_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_permission(PermissionName.USER_READ))],
    session: DbSession,
) -> UserRolesRead:
    roles = RbacService(session).get_user_roles(principal, user_id)
    return UserRolesRead(user_id=user_id, roles=roles)


@router.put("/{user_id}/roles", response_model=UserRolesRead, responses=_ERRORS)
def set_user_roles(
    user_id: uuid.UUID,
    body: UserRolesUpdate,
    principal: Annotated[Principal, Depends(require_permission(PermissionName.USER_ASSIGN_ROLE))],
    session: DbSession,
    context: RequestCtx,
) -> UserRolesRead:
    roles = RbacService(session, context).set_user_roles(principal, user_id, body.roles)
    return UserRolesRead(user_id=user_id, roles=roles)
