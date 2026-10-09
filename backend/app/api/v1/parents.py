"""Parents. Staff manage the school's parents; a parent can read only themselves."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    DbSession,
    Pagination,
    RequestCtx,
    SettingsDep,
    require_school_any_permission,
    require_school_permission,
)
from app.api.v1._common import ERRORS
from app.api.v1._people import PARENT_READERS, link_with_student, parent_read
from app.schemas.common import Page
from app.schemas.people import ParentCreate, ParentRead, ParentUpdate, StudentParentRead
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.parents import ParentService

router = APIRouter(prefix="/parents", tags=["parents"])

ReadScope = Annotated[SchoolScope, Depends(require_school_any_permission(*PARENT_READERS))]
# A parent is a user account, so parent management reuses the user.* permissions.
CreateScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.USER_CREATE))]
UpdateScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.USER_UPDATE))]


@router.post("", response_model=ParentRead, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_parent(
    body: ParentCreate,
    scope: CreateScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> ParentRead:
    """Create a parent and their PARENT user (signs in with mobile OTP; no password)."""
    return parent_read(*ParentService(session, settings, context).create(scope, body))


@router.get("", response_model=Page[ParentRead], responses=ERRORS)
def list_parents(
    scope: ReadScope,
    session: DbSession,
    settings: SettingsDep,
    pagination: Pagination,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[ParentRead]:
    """Staff: the school's parents. A parent: only their own record."""
    rows, total = ParentService(session, settings).list(
        scope, is_active=is_active, offset=pagination.offset, limit=pagination.limit
    )
    return Page[ParentRead].create(
        [parent_read(*row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{parent_id}", response_model=ParentRead, responses=ERRORS)
def get_parent(
    parent_id: uuid.UUID, scope: ReadScope, session: DbSession, settings: SettingsDep
) -> ParentRead:
    return parent_read(*ParentService(session, settings).get(scope, parent_id))


@router.patch("/{parent_id}", response_model=ParentRead, responses=ERRORS)
def update_parent(
    parent_id: uuid.UUID,
    body: ParentUpdate,
    scope: UpdateScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> ParentRead:
    """Update a parent. ``is_active`` changes additionally require ``user.deactivate``."""
    return parent_read(*ParentService(session, settings, context).update(scope, parent_id, body))


@router.get("/{parent_id}/children", response_model=list[StudentParentRead], responses=ERRORS)
def list_children(
    parent_id: uuid.UUID, scope: ReadScope, session: DbSession, settings: SettingsDep
) -> list[StudentParentRead]:
    rows = ParentService(session, settings).children(scope, parent_id)
    return [link_with_student(*row) for row in rows]
