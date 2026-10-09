"""Student enrollments (academic placement history)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    DbSession,
    Pagination,
    RequestCtx,
    require_school_any_permission,
    require_school_permission,
)
from app.api.v1._common import ERRORS, to_page
from app.api.v1._people import STUDENT_READERS
from app.models.people import EnrollmentStatus
from app.schemas.common import Page
from app.schemas.people import EnrollmentCreate, EnrollmentRead, EnrollmentUpdate
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.enrollments import EnrollmentService

router = APIRouter(prefix="/enrollments", tags=["enrollments"])

ReadScope = Annotated[SchoolScope, Depends(require_school_any_permission(*STUDENT_READERS))]
# Enrolling is part of managing a student's record: reuses student.update.
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.STUDENT_UPDATE))
]


@router.post(
    "", response_model=EnrollmentRead, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
def create_enrollment(
    body: EnrollmentCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> EnrollmentRead:
    return EnrollmentRead.model_validate(EnrollmentService(session, context).create(scope, body))


@router.get("", response_model=Page[EnrollmentRead], responses=ERRORS)
def list_enrollments(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    student_id: Annotated[uuid.UUID | None, Query()] = None,
    academic_year_id: Annotated[uuid.UUID | None, Query()] = None,
    class_id: Annotated[uuid.UUID | None, Query()] = None,
    section_id: Annotated[uuid.UUID | None, Query()] = None,
    is_current: Annotated[bool | None, Query(alias="current")] = None,
    status_filter: Annotated[EnrollmentStatus | None, Query(alias="status")] = None,
) -> Page[EnrollmentRead]:
    """Parents see only their children's enrollments; students only their own."""
    items, total = EnrollmentService(session).list(
        scope,
        student_id=student_id,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        is_current=is_current,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(EnrollmentRead, items, total, pagination)


@router.get("/{enrollment_id}", response_model=EnrollmentRead, responses=ERRORS)
def get_enrollment(
    enrollment_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> EnrollmentRead:
    return EnrollmentRead.model_validate(EnrollmentService(session).get(scope, enrollment_id))


@router.patch("/{enrollment_id}", response_model=EnrollmentRead, responses=ERRORS)
def update_enrollment(
    enrollment_id: uuid.UUID,
    body: EnrollmentUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> EnrollmentRead:
    """Update status/dates/number, or make it current (atomically). Placement is immutable."""
    item = EnrollmentService(session, context).update(scope, enrollment_id, body)
    return EnrollmentRead.model_validate(item)
