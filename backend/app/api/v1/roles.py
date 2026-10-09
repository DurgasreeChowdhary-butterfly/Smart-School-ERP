"""Read-only role and permission catalog."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, Pagination, require_permission
from app.schemas.common import ErrorResponse, Page
from app.schemas.rbac import PermissionRead, RoleRead
from app.security.permissions import PermissionName
from app.security.principal import Principal
from app.services.rbac import RbacService

router = APIRouter(tags=["rbac"])
_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
}


@router.get("/roles", response_model=Page[RoleRead], responses=_ERRORS)
def list_roles(
    _: Annotated[Principal, Depends(require_permission(PermissionName.ROLE_READ))],
    session: DbSession,
    pagination: Pagination,
) -> Page[RoleRead]:
    items, total = RbacService(session).list_roles(offset=pagination.offset, limit=pagination.limit)
    return Page[RoleRead].create(
        [
            RoleRead(name=i.role.name, description=i.role.description, permissions=i.permissions)
            for i in items
        ],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/permissions", response_model=Page[PermissionRead], responses=_ERRORS)
def list_permissions(
    _: Annotated[Principal, Depends(require_permission(PermissionName.PERMISSION_READ))],
    session: DbSession,
    pagination: Pagination,
) -> Page[PermissionRead]:
    items, total = RbacService(session).list_permissions(
        offset=pagination.offset, limit=pagination.limit
    )
    return Page[PermissionRead].create(
        [PermissionRead.model_validate(item) for item in items],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )
