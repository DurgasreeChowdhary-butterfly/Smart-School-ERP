from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, Pagination, RequestCtx, require_school_permission
from app.api.v1._common import ERRORS, to_page
from app.schemas.academic import ClassCreate, ClassRead, ClassUpdate
from app.schemas.common import Page
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.academic import ClassService

router = APIRouter(prefix="/classes", tags=["classes"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_READ))]
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.ACADEMIC_MANAGE))
]


@router.post("", response_model=ClassRead, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_class(
    body: ClassCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> ClassRead:
    return ClassRead.model_validate(ClassService(session, context).create(scope, body))


@router.get("", response_model=Page[ClassRead], responses=ERRORS)
def list_classes(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[ClassRead]:
    items, total = ClassService(session).list(
        scope, is_active=is_active, offset=pagination.offset, limit=pagination.limit
    )
    return to_page(ClassRead, items, total, pagination)


@router.get("/{class_id}", response_model=ClassRead, responses=ERRORS)
def get_class(class_id: uuid.UUID, scope: ReadScope, session: DbSession) -> ClassRead:
    return ClassRead.model_validate(ClassService(session).get(scope, class_id))


@router.patch("/{class_id}", response_model=ClassRead, responses=ERRORS)
def update_class(
    class_id: uuid.UUID,
    body: ClassUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> ClassRead:
    """Update or (de)activate a class. Classes are never deleted."""
    return ClassRead.model_validate(ClassService(session, context).update(scope, class_id, body))
