"""Faculty. Staff manage the school's faculty; a faculty member reads only themselves."""

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
from app.api.v1._common import ERRORS, to_page
from app.models.faculty import Faculty, FacultyStatus
from app.models.user import User
from app.schemas.common import Page
from app.schemas.faculty import FacultyAssignmentRead, FacultyCreate, FacultyRead, FacultyUpdate
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.faculty import FacultyService

P = PermissionName
router = APIRouter(prefix="/faculty", tags=["faculty"])

# Managers and staff with user.read (MANAGEMENT) read the school's faculty; a faculty
# member (faculty.read_self) reads only their own record.
FACULTY_READERS = (P.FACULTY_MANAGE, P.USER_READ, P.FACULTY_READ_SELF)

ReadScope = Annotated[SchoolScope, Depends(require_school_any_permission(*FACULTY_READERS))]
ManageScope = Annotated[SchoolScope, Depends(require_school_permission(P.FACULTY_MANAGE))]
SelfScope = Annotated[SchoolScope, Depends(require_school_permission(P.FACULTY_READ_SELF))]


def faculty_read(faculty: Faculty, user: User) -> FacultyRead:
    return FacultyRead(
        id=faculty.id,
        school_id=faculty.school_id,
        user_id=faculty.user_id,
        employee_id=faculty.employee_id,
        first_name=faculty.first_name,
        middle_name=faculty.middle_name,
        last_name=faculty.last_name,
        designation=faculty.designation,
        department=faculty.department,
        joining_date=faculty.joining_date,
        email=user.email,
        mobile=user.phone,
        status=faculty.status,
        is_active=faculty.is_active,
        created_at=faculty.created_at,
        updated_at=faculty.updated_at,
    )


@router.post("", response_model=FacultyRead, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_faculty(
    body: FacultyCreate,
    scope: ManageScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> FacultyRead:
    """Create a faculty member and their FACULTY login (email/mobile + password), or
    attach an existing FACULTY user with ``user_id``. All-or-nothing."""
    return faculty_read(*FacultyService(session, settings, context).create(scope, body))


@router.get("", response_model=Page[FacultyRead], responses=ERRORS)
def list_faculty(
    scope: ReadScope,
    session: DbSession,
    settings: SettingsDep,
    pagination: Pagination,
    status_filter: Annotated[FacultyStatus | None, Query(alias="status")] = None,
    employee_id: Annotated[str | None, Query(max_length=30)] = None,
) -> Page[FacultyRead]:
    """Staff: the school's faculty. A faculty member: only their own record."""
    rows, total = FacultyService(session, settings).list(
        scope,
        status=status_filter,
        employee_id=employee_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return Page[FacultyRead].create(
        [faculty_read(*row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/me", response_model=FacultyRead, responses=ERRORS)
def get_my_faculty_profile(
    scope: SelfScope, session: DbSession, settings: SettingsDep
) -> FacultyRead:
    """The caller's own faculty record (404 if they have none)."""
    return faculty_read(*FacultyService(session, settings).me(scope))


@router.get("/me/assignments", response_model=Page[FacultyAssignmentRead], responses=ERRORS)
def list_my_assignments(
    scope: SelfScope, session: DbSession, settings: SettingsDep, pagination: Pagination
) -> Page[FacultyAssignmentRead]:
    """The caller's *effective* assignments: exactly the scope they hold today.

    Full history (including ended assignments) is at ``GET /faculty-assignments``.
    """
    items, total = FacultyService(session, settings).my_assignments(
        scope, offset=pagination.offset, limit=pagination.limit
    )
    return to_page(FacultyAssignmentRead, items, total, pagination)


@router.get("/{faculty_id}", response_model=FacultyRead, responses=ERRORS)
def get_faculty(
    faculty_id: uuid.UUID, scope: ReadScope, session: DbSession, settings: SettingsDep
) -> FacultyRead:
    return faculty_read(*FacultyService(session, settings).get(scope, faculty_id))


@router.patch("/{faculty_id}", response_model=FacultyRead, responses=ERRORS)
def update_faculty(
    faculty_id: uuid.UUID,
    body: FacultyUpdate,
    scope: ManageScope,
    session: DbSession,
    settings: SettingsDep,
    context: RequestCtx,
) -> FacultyRead:
    """Update a faculty member; ``status`` (de)activates them and their login."""
    return faculty_read(*FacultyService(session, settings, context).update(scope, faculty_id, body))
