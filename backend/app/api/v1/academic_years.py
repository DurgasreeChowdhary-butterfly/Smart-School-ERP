from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, Pagination, RequestCtx, require_school_permission
from app.api.v1._common import ERRORS, to_page
from app.schemas.academic import AcademicYearCreate, AcademicYearRead, AcademicYearUpdate
from app.schemas.common import Page
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.academic import AcademicYearService

router = APIRouter(prefix="/academic-years", tags=["academic years"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_READ))]
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_MANAGE))
]


@router.post(
    "", response_model=AcademicYearRead, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
def create_academic_year(
    body: AcademicYearCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> AcademicYearRead:
    year = AcademicYearService(session, context).create(scope, body)
    return AcademicYearRead.model_validate(year)


@router.get("", response_model=Page[AcademicYearRead], responses=ERRORS)
def list_academic_years(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    is_active: Annotated[bool | None, Query()] = None,
    is_current: Annotated[bool | None, Query()] = None,
) -> Page[AcademicYearRead]:
    items, total = AcademicYearService(session).list(
        scope,
        is_active=is_active,
        is_current=is_current,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(AcademicYearRead, items, total, pagination)


@router.get("/{year_id}", response_model=AcademicYearRead, responses=ERRORS)
def get_academic_year(year_id: uuid.UUID, scope: ReadScope, session: DbSession) -> AcademicYearRead:
    return AcademicYearRead.model_validate(AcademicYearService(session).get(scope, year_id))


@router.patch("/{year_id}", response_model=AcademicYearRead, responses=ERRORS)
def update_academic_year(
    year_id: uuid.UUID,
    body: AcademicYearUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> AcademicYearRead:
    year = AcademicYearService(session, context).update(scope, year_id, body)
    return AcademicYearRead.model_validate(year)


@router.post("/{year_id}/set-current", response_model=AcademicYearRead, responses=ERRORS)
def set_current_academic_year(
    year_id: uuid.UUID, scope: ManageScope, session: DbSession, context: RequestCtx
) -> AcademicYearRead:
    """Make this the school's current year; the previous current year is unset atomically."""
    year = AcademicYearService(session, context).set_current(scope, year_id)
    return AcademicYearRead.model_validate(year)
