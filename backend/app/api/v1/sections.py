from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, Pagination, RequestCtx, require_school_permission
from app.api.v1._common import ERRORS, to_page
from app.schemas.academic import SectionCreate, SectionRead, SectionUpdate
from app.schemas.common import Page
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.academic import SectionService

router = APIRouter(prefix="/sections", tags=["sections"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_READ))]
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_MANAGE))
]


@router.post("", response_model=SectionRead, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_section(
    body: SectionCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> SectionRead:
    return SectionRead.model_validate(SectionService(session, context).create(scope, body))


@router.get("", response_model=Page[SectionRead], responses=ERRORS)
def list_sections(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    class_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[SectionRead]:
    items, total = SectionService(session).list(
        scope,
        class_id=class_id,
        is_active=is_active,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(SectionRead, items, total, pagination)


@router.get("/{section_id}", response_model=SectionRead, responses=ERRORS)
def get_section(section_id: uuid.UUID, scope: ReadScope, session: DbSession) -> SectionRead:
    return SectionRead.model_validate(SectionService(session).get(scope, section_id))


@router.patch("/{section_id}", response_model=SectionRead, responses=ERRORS)
def update_section(
    section_id: uuid.UUID,
    body: SectionUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> SectionRead:
    """Update or (de)activate a section. ``class_id`` cannot be changed."""
    item = SectionService(session, context).update(scope, section_id, body)
    return SectionRead.model_validate(item)
