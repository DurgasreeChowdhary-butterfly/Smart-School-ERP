from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, Pagination, RequestCtx, require_school_permission
from app.api.v1._common import ERRORS, to_page
from app.schemas.academic import SubjectCreate, SubjectRead, SubjectUpdate
from app.schemas.common import Page
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.academic import SubjectService

router = APIRouter(prefix="/subjects", tags=["subjects"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_READ))]
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_MANAGE))
]


@router.post("", response_model=SubjectRead, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_subject(
    body: SubjectCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> SubjectRead:
    return SubjectRead.model_validate(SubjectService(session, context).create(scope, body))


@router.get("", response_model=Page[SubjectRead], responses=ERRORS)
def list_subjects(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[SubjectRead]:
    items, total = SubjectService(session).list(
        scope, is_active=is_active, offset=pagination.offset, limit=pagination.limit
    )
    return to_page(SubjectRead, items, total, pagination)


@router.get("/{subject_id}", response_model=SubjectRead, responses=ERRORS)
def get_subject(subject_id: uuid.UUID, scope: ReadScope, session: DbSession) -> SubjectRead:
    return SubjectRead.model_validate(SubjectService(session).get(scope, subject_id))


@router.patch("/{subject_id}", response_model=SubjectRead, responses=ERRORS)
def update_subject(
    subject_id: uuid.UUID,
    body: SubjectUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> SubjectRead:
    """Update or (de)activate a subject. Subjects are never deleted."""
    item = SubjectService(session, context).update(scope, subject_id, body)
    return SubjectRead.model_validate(item)
