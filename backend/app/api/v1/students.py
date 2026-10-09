"""Students and their parent/guardian links.

Reads are scoped per caller: staff see their school, a parent sees linked
children, a student sees only themselves. Anything else is 404.
"""

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
from app.api.v1._people import STUDENT_READERS, link_with_parent, student_read
from app.models.people import StudentStatus
from app.schemas.common import Page
from app.schemas.people import (
    StudentCreate,
    StudentParentCreate,
    StudentParentRead,
    StudentParentUpdate,
    StudentRead,
    StudentUpdate,
)
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.students import StudentService

router = APIRouter(prefix="/students", tags=["students"])

ReadScope = Annotated[SchoolScope, Depends(require_school_any_permission(*STUDENT_READERS))]
CreateScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.STUDENT_CREATE))
]
UpdateScope = Annotated[
    SchoolScope, Depends(require_school_permission(PermissionName.STUDENT_UPDATE))
]


@router.post("", response_model=StudentRead, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_student(
    body: StudentCreate,
    scope: CreateScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> StudentRead:
    """Create a student; with ``password``, also their login (admission number + password)."""
    student = StudentService(session, settings, context).create(scope, body)
    return StudentRead.model_validate(student)


@router.get("", response_model=Page[StudentRead], responses=ERRORS)
def list_students(
    scope: ReadScope,
    session: DbSession,
    settings: SettingsDep,
    pagination: Pagination,
    status_filter: Annotated[StudentStatus | None, Query(alias="status")] = None,
    admission_number: Annotated[str | None, Query(max_length=30)] = None,
    academic_year_id: Annotated[uuid.UUID | None, Query()] = None,
    class_id: Annotated[uuid.UUID | None, Query()] = None,
    section_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[StudentRead]:
    """Filters only narrow the caller's authorised set; they never widen it.

    Faculty see only students enrolled in their assigned sections, without contact details.
    """
    items, total = StudentService(session, settings).list(
        scope,
        status=status_filter,
        admission_number=admission_number,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return Page[StudentRead].create(
        [student_read(item, scope.principal) for item in items],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{student_id}", response_model=StudentRead, responses=ERRORS)
def get_student(
    student_id: uuid.UUID, scope: ReadScope, session: DbSession, settings: SettingsDep
) -> StudentRead:
    return student_read(StudentService(session, settings).get(scope, student_id), scope.principal)


@router.patch("/{student_id}", response_model=StudentRead, responses=ERRORS)
def update_student(
    student_id: uuid.UUID,
    body: StudentUpdate,
    scope: UpdateScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> StudentRead:
    """Update a student. ``status`` changes additionally require ``student.deactivate``."""
    student = StudentService(session, settings, context).update(scope, student_id, body)
    return StudentRead.model_validate(student)


@router.get("/{student_id}/parents", response_model=list[StudentParentRead], responses=ERRORS)
def list_student_parents(
    student_id: uuid.UUID, scope: ReadScope, session: DbSession, settings: SettingsDep
) -> list[StudentParentRead]:
    """Staff see every link; a parent sees only their own link; a student their active ones;
    faculty none (parents' personal data is not part of the faculty scope)."""
    rows = StudentService(session, settings).list_parents(scope, student_id)
    return [link_with_parent(*row) for row in rows]


@router.post(
    "/{student_id}/parents",
    response_model=StudentParentRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def link_parent(
    student_id: uuid.UUID,
    body: StudentParentCreate,
    scope: UpdateScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> StudentParentRead:
    row = StudentService(session, settings, context).link_parent(scope, student_id, body)
    return link_with_parent(*row)


@router.patch(
    "/{student_id}/parents/{parent_id}", response_model=StudentParentRead, responses=ERRORS
)
def update_parent_link(
    student_id: uuid.UUID,
    parent_id: uuid.UUID,
    body: StudentParentUpdate,
    scope: UpdateScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> StudentParentRead:
    """Change or (de)activate a relationship. Links are never deleted."""
    row = StudentService(session, settings, context).update_link(scope, student_id, parent_id, body)
    return link_with_parent(*row)
