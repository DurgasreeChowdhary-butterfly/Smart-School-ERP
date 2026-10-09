"""The caller's school (``/school``) and the platform-level school list (``/schools``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import (
    DbSession,
    Pagination,
    RequestCtx,
    require_permission,
    require_role,
    require_school_permission,
)
from app.api.v1._common import ERRORS, to_page
from app.schemas.common import Page
from app.schemas.school import SchoolRead, SchoolUpdate
from app.security.permissions import PermissionName, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.school import SchoolService

router = APIRouter(tags=["school"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.SCHOOL_READ))]
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.SCHOOL_MANAGE))
]


@router.get("/school", response_model=SchoolRead, responses=ERRORS)
def get_school(scope: ReadScope, session: DbSession) -> SchoolRead:
    """The caller's school (SUPER_ADMIN: the school named by ``X-School-ID``)."""
    return SchoolRead.model_validate(SchoolService(session).get(scope))


@router.patch("/school", response_model=SchoolRead, responses=ERRORS)
def update_school(
    body: SchoolUpdate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> SchoolRead:
    """Update the school profile. ``code`` is immutable; ``is_active`` is SUPER_ADMIN-only."""
    return SchoolRead.model_validate(SchoolService(session, context).update(scope, body))


@router.get("/schools", response_model=Page[SchoolRead], responses=ERRORS)
def list_schools(
    _: Annotated[Principal, Depends(require_role(RoleName.SUPER_ADMIN))],
    __: Annotated[Principal, Depends(require_permission(PermissionName.SCHOOL_READ))],
    session: DbSession,
    pagination: Pagination,
) -> Page[SchoolRead]:
    """All schools on the platform (SUPER_ADMIN only), to choose an ``X-School-ID``."""
    items, total = SchoolService(session).list_all(offset=pagination.offset, limit=pagination.limit)
    return to_page(SchoolRead, items, total, pagination)
