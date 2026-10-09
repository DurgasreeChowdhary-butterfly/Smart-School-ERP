"""Faculty assignments: which faculty member teaches which subject to which section."""

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
from app.api.v1.faculty import FACULTY_READERS
from app.models.faculty import AssignmentStatus
from app.schemas.common import Page
from app.schemas.faculty import (
    FacultyAssignmentCreate,
    FacultyAssignmentRead,
    FacultyAssignmentUpdate,
)
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.faculty import FacultyAssignmentService

router = APIRouter(prefix="/faculty-assignments", tags=["faculty assignments"])

ReadScope = Annotated[SchoolScope, Depends(require_school_any_permission(*FACULTY_READERS))]
ManageScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.FACULTY_MANAGE))
]


@router.post(
    "", response_model=FacultyAssignmentRead, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
def create_assignment(
    body: FacultyAssignmentCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> FacultyAssignmentRead:
    item = FacultyAssignmentService(session, context).create(scope, body)
    return FacultyAssignmentRead.model_validate(item)


@router.get("", response_model=Page[FacultyAssignmentRead], responses=ERRORS)
def list_assignments(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    faculty_id: Annotated[uuid.UUID | None, Query()] = None,
    academic_year_id: Annotated[uuid.UUID | None, Query()] = None,
    class_id: Annotated[uuid.UUID | None, Query()] = None,
    section_id: Annotated[uuid.UUID | None, Query()] = None,
    subject_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[AssignmentStatus | None, Query(alias="status")] = None,
) -> Page[FacultyAssignmentRead]:
    """Staff: the school's assignments. A faculty member: only their own (with history).

    Filters only narrow the caller's authorised set; they never widen it.
    """
    items, total = FacultyAssignmentService(session).list(
        scope,
        faculty_id=faculty_id,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        subject_id=subject_id,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(FacultyAssignmentRead, items, total, pagination)


@router.get("/{assignment_id}", response_model=FacultyAssignmentRead, responses=ERRORS)
def get_assignment(
    assignment_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> FacultyAssignmentRead:
    item = FacultyAssignmentService(session).get(scope, assignment_id)
    return FacultyAssignmentRead.model_validate(item)


@router.patch("/{assignment_id}", response_model=FacultyAssignmentRead, responses=ERRORS)
def update_assignment(
    assignment_id: uuid.UUID,
    body: FacultyAssignmentUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> FacultyAssignmentRead:
    """Change effective dates or (de)activate. Placement is immutable; never deleted."""
    item = FacultyAssignmentService(session, context).update(scope, assignment_id, body)
    return FacultyAssignmentRead.model_validate(item)
