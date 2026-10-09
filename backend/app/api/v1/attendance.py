"""Attendance sessions, records, corrections, history and summaries.

Reads need ``attendance.read`` and are narrowed to the caller's scope (school staff:
school; faculty: assigned sessions; parent: linked children; student: self). Writes
need ``attendance.manage`` (SCHOOL_ADMIN, SUPER_ADMIN, and FACULTY within their
subject assignments). Anything outside the caller's scope is 404.
"""

from __future__ import annotations

import uuid
from datetime import date
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
from app.models.attendance import AttendanceStatus, SessionStatus, SessionType
from app.schemas.attendance import (
    AttendanceBulkCreate,
    AttendanceBulkResult,
    AttendanceRecordChangeRead,
    AttendanceRecordCreate,
    AttendanceRecordRead,
    AttendanceRecordUpdate,
    AttendanceSessionCreate,
    AttendanceSessionRead,
    AttendanceSessionUpdate,
    AttendanceSummary,
    UnmarkedStudent,
)
from app.schemas.common import Page
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.attendance import (
    AttendanceRecordService,
    AttendanceSessionService,
    RecordRows,
    record_read,
)

P = PermissionName
router = APIRouter(prefix="/attendance", tags=["attendance"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(P.ATTENDANCE_READ))]
ManageScope = Annotated[SchoolScope, Depends(require_school_permission(P.ATTENDANCE_MANAGE))]
# Correction history: people who record attendance, and school-wide staff readers.
HistoryScope = Annotated[
    SchoolScope, Depends(require_school_any_permission(P.ATTENDANCE_MANAGE, P.STUDENT_READ))
]
DateQuery = Annotated[date | None, Query()]
IdQuery = Annotated[uuid.UUID | None, Query()]


def _records_page(
    rows: RecordRows, total: int, pagination: Pagination
) -> Page[AttendanceRecordRead]:
    return Page[AttendanceRecordRead].create(
        [record_read(*row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# ========================================================================== sessions
@router.post(
    "/sessions",
    response_model=AttendanceSessionRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_session(
    body: AttendanceSessionCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> AttendanceSessionRead:
    """Daily roll call (no ``subject_id``; school staff only) or subject roll call
    (faculty need an assignment for that year, class, section and subject)."""
    item = AttendanceSessionService(session, context).create(scope, body)
    return AttendanceSessionRead.model_validate(item)


@router.get("/sessions", response_model=Page[AttendanceSessionRead], responses=ERRORS)
def list_sessions(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    academic_year_id: IdQuery = None,
    class_id: IdQuery = None,
    section_id: IdQuery = None,
    subject_id: IdQuery = None,
    session_type: Annotated[SessionType | None, Query()] = None,
    session_date: DateQuery = None,
    from_date: DateQuery = None,
    to_date: DateQuery = None,
    status_filter: Annotated[SessionStatus | None, Query(alias="status")] = None,
) -> Page[AttendanceSessionRead]:
    """Filters only narrow the caller's authorised set; they never widen it."""
    items, total = AttendanceSessionService(session).list(
        scope,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        subject_id=subject_id,
        session_type=session_type,
        session_date=session_date,
        from_date=from_date,
        to_date=to_date,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(AttendanceSessionRead, items, total, pagination)


@router.get("/sessions/{session_id}", response_model=AttendanceSessionRead, responses=ERRORS)
def get_session(
    session_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> AttendanceSessionRead:
    return AttendanceSessionRead.model_validate(
        AttendanceSessionService(session).get(scope, session_id)
    )


@router.patch("/sessions/{session_id}", response_model=AttendanceSessionRead, responses=ERRORS)
def update_session(
    session_id: uuid.UUID,
    body: AttendanceSessionUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> AttendanceSessionRead:
    """Times and ``status``. An inactive session keeps its records but accepts no
    changes and is left out of summaries. Never deleted."""
    item = AttendanceSessionService(session, context).update(scope, session_id, body)
    return AttendanceSessionRead.model_validate(item)


# ======================================================================= session records
@router.post(
    "/sessions/{session_id}/records",
    response_model=AttendanceRecordRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_record(
    session_id: uuid.UUID,
    body: AttendanceRecordCreate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> AttendanceRecordRead:
    return record_read(*AttendanceRecordService(session, context).create(scope, session_id, body))


@router.post(
    "/sessions/{session_id}/records/bulk",
    response_model=AttendanceBulkResult,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def bulk_create_records(
    session_id: uuid.UUID,
    body: AttendanceBulkCreate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> AttendanceBulkResult:
    """Mark many students at once. All-or-nothing: any duplicate, ineligible or
    already-marked student rejects the whole request (nothing is written)."""
    records, item = AttendanceRecordService(session, context).bulk_create(scope, session_id, body)
    return AttendanceBulkResult(
        session_id=item.id,
        created=len(records),
        items=[record_read(record, item) for record in records],
    )


@router.get(
    "/sessions/{session_id}/records", response_model=Page[AttendanceRecordRead], responses=ERRORS
)
def list_session_records(
    session_id: uuid.UUID,
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    status_filter: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
) -> Page[AttendanceRecordRead]:
    rows, total = AttendanceRecordService(session).list_for_session(
        scope, session_id, status=status_filter, offset=pagination.offset, limit=pagination.limit
    )
    return _records_page(rows, total, pagination)


@router.get(
    "/sessions/{session_id}/unmarked-students",
    response_model=Page[UnmarkedStudent],
    responses=ERRORS,
)
def list_unmarked_students(
    session_id: uuid.UUID, scope: ReadScope, session: DbSession, pagination: Pagination
) -> Page[UnmarkedStudent]:
    """Students enrolled in the session's section on its date who have no record yet."""
    rows, total = AttendanceRecordService(session).unmarked(
        scope, session_id, offset=pagination.offset, limit=pagination.limit
    )
    return Page[UnmarkedStudent].create(
        [
            UnmarkedStudent(
                student_id=student.id,
                enrollment_id=enrollment_id,
                admission_number=student.admission_number,
                first_name=student.first_name,
                last_name=student.last_name,
            )
            for student, enrollment_id in rows
        ],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# =========================================================================== records
@router.get("/records", response_model=Page[AttendanceRecordRead], responses=ERRORS)
def list_records(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    student_id: IdQuery = None,
    academic_year_id: IdQuery = None,
    class_id: IdQuery = None,
    section_id: IdQuery = None,
    subject_id: IdQuery = None,
    session_type: Annotated[SessionType | None, Query()] = None,
    status_filter: Annotated[AttendanceStatus | None, Query(alias="status")] = None,
    session_date: DateQuery = None,
    from_date: DateQuery = None,
    to_date: DateQuery = None,
) -> Page[AttendanceRecordRead]:
    """Attendance history. Staff: the school; faculty: their sessions; parents: linked
    children; students: themselves. Filters only narrow that set.

    Year/class/section are those of the *session* (the student's placement that day),
    not the student's current enrollment.
    """
    rows, total = AttendanceRecordService(session).list(
        scope,
        student_id=student_id,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        subject_id=subject_id,
        session_type=session_type,
        status=status_filter,
        session_date=session_date,
        from_date=from_date,
        to_date=to_date,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return _records_page(rows, total, pagination)


@router.get("/records/{record_id}", response_model=AttendanceRecordRead, responses=ERRORS)
def get_record(record_id: uuid.UUID, scope: ReadScope, session: DbSession) -> AttendanceRecordRead:
    return record_read(*AttendanceRecordService(session).get(scope, record_id))


@router.patch("/records/{record_id}", response_model=AttendanceRecordRead, responses=ERRORS)
def update_record(
    record_id: uuid.UUID,
    body: AttendanceRecordUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> AttendanceRecordRead:
    """Change remarks, or correct the status (``reason`` required). Every change is
    appended to the record's history; ``original_status`` never changes."""
    return record_read(*AttendanceRecordService(session, context).update(scope, record_id, body))


@router.get(
    "/records/{record_id}/history",
    response_model=list[AttendanceRecordChangeRead],
    responses=ERRORS,
)
def get_record_history(
    record_id: uuid.UUID, scope: HistoryScope, session: DbSession
) -> list[AttendanceRecordChangeRead]:
    """Every change to the record, oldest first (who, when, from, to, why)."""
    changes = AttendanceRecordService(session).history(scope, record_id)
    return [AttendanceRecordChangeRead.model_validate(change) for change in changes]


# =========================================================================== summary
@router.get("/students/{student_id}/summary", response_model=AttendanceSummary, responses=ERRORS)
def get_student_summary(
    student_id: uuid.UUID,
    scope: ReadScope,
    session: DbSession,
    academic_year_id: IdQuery = None,
    from_date: DateQuery = None,
    to_date: DateQuery = None,
    subject_id: IdQuery = None,
    session_type: Annotated[SessionType | None, Query()] = None,
) -> AttendanceSummary:
    """Counts over the records the caller may see. LATE counts as attended; EXCUSED is
    excluded from the percentage; unmarked sessions are not counted."""
    return AttendanceRecordService(session).summary(
        scope,
        student_id,
        academic_year_id=academic_year_id,
        from_date=from_date,
        to_date=to_date,
        subject_id=subject_id,
        session_type=session_type,
    )
