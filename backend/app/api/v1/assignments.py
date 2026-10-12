"""Assignments, submissions, revisions and reviews.

Reads need ``assignment.read`` and are narrowed to the caller's scope (school staff:
school; faculty: their section + subject; parents: linked children; students:
themselves; drafts never reach parents or students). Managing and reviewing need
``assignment.manage`` (staff, or faculty within their assignments). Students hand in
their own work with ``assignment.submit``. Anything outside the caller's scope is 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DbSession, Pagination, RequestCtx, require_school_permission
from app.api.v1._common import ERRORS, to_page
from app.models.assignments import AssignmentLifecycle, SubmissionStatus
from app.schemas.assignments import (
    AssignmentCreate,
    AssignmentRead,
    AssignmentStudent,
    AssignmentUpdate,
    ReviewChangeRead,
    ReviewInput,
    RevisionRead,
    SubmissionCreate,
    SubmissionRead,
)
from app.schemas.common import Page
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.assignment_submissions import SubmissionService
from app.services.assignments import AssignmentService

P = PermissionName
router = APIRouter(tags=["assignments"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(P.ASSIGNMENT_READ))]
ManageScope = Annotated[SchoolScope, Depends(require_school_permission(P.ASSIGNMENT_MANAGE))]
SubmitScope = Annotated[SchoolScope, Depends(require_school_permission(P.ASSIGNMENT_SUBMIT))]
IdQuery = Annotated[uuid.UUID | None, Query()]


def _page[T](items: list[T], total: int, pagination: Pagination) -> Page[T]:
    return Page[T].create(items, total=total, page=pagination.page, page_size=pagination.page_size)


# ======================================================================= assignments
@router.post(
    "/assignments",
    response_model=AssignmentRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_assignment(
    body: AssignmentCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> AssignmentRead:
    """Create a **draft**. Faculty need an assignment for that year, class, section and
    subject (effective today and on ``assigned_on``)."""
    return AssignmentRead.model_validate(AssignmentService(session, context).create(scope, body))


@router.get("/assignments", response_model=Page[AssignmentRead], responses=ERRORS)
def list_assignments(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    academic_year_id: IdQuery = None,
    class_id: IdQuery = None,
    section_id: IdQuery = None,
    subject_id: IdQuery = None,
    status_filter: Annotated[AssignmentLifecycle | None, Query(alias="status")] = None,
) -> Page[AssignmentRead]:
    """Filters only narrow the caller's authorised set; they never widen it."""
    items, total = AssignmentService(session).list(
        scope,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        subject_id=subject_id,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(AssignmentRead, items, total, pagination)


@router.get("/assignments/{assignment_id}", response_model=AssignmentRead, responses=ERRORS)
def get_assignment(
    assignment_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> AssignmentRead:
    return AssignmentRead.model_validate(AssignmentService(session).get(scope, assignment_id))


@router.patch("/assignments/{assignment_id}", response_model=AssignmentRead, responses=ERRORS)
def update_assignment(
    assignment_id: uuid.UUID,
    body: AssignmentUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> AssignmentRead:
    """Draft: any field. Published: title, instructions, due_at, allow_late,
    allow_resubmission. Closed: read-only."""
    item = AssignmentService(session, context).update(scope, assignment_id, body)
    return AssignmentRead.model_validate(item)


@router.post(
    "/assignments/{assignment_id}/publish", response_model=AssignmentRead, responses=ERRORS
)
def publish_assignment(
    assignment_id: uuid.UUID, scope: ManageScope, session: DbSession, context: RequestCtx
) -> AssignmentRead:
    """``draft -> published``: visible to the section's students and their parents."""
    item = AssignmentService(session, context).publish(scope, assignment_id)
    return AssignmentRead.model_validate(item)


@router.post("/assignments/{assignment_id}/close", response_model=AssignmentRead, responses=ERRORS)
def close_assignment(
    assignment_id: uuid.UUID, scope: ManageScope, session: DbSession, context: RequestCtx
) -> AssignmentRead:
    """``published -> closed``: no more (re)submissions; reviews remain possible."""
    item = AssignmentService(session, context).close(scope, assignment_id)
    return AssignmentRead.model_validate(item)


@router.get(
    "/assignments/{assignment_id}/students",
    response_model=Page[AssignmentStudent],
    responses=ERRORS,
)
def list_assignment_students(
    assignment_id: uuid.UUID,
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    outstanding: Annotated[bool, Query(description="Only students without a submission.")] = False,
) -> Page[AssignmentStudent]:
    """Students enrolled in the section during the assignment window, with their
    submission state (school staff and faculty only)."""
    rows, total = AssignmentService(session).students(
        scope,
        assignment_id,
        outstanding_only=outstanding,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return _page(
        [
            AssignmentStudent(
                student_id=student.id,
                enrollment_id=enrollment.id,
                section_id=enrollment.section_id,
                admission_number=student.admission_number,
                first_name=student.first_name,
                last_name=student.last_name,
                submission_id=sub.id if sub else None,
                submission_status=sub.status if sub else "not_submitted",
                is_late=sub.is_late if sub else None,
                marks_awarded=sub.marks_awarded if sub else None,
            )
            for student, enrollment, sub in rows
        ],
        total,
        pagination,
    )


# ======================================================================= submissions
@router.post(
    "/assignments/{assignment_id}/submissions",
    response_model=SubmissionRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def submit_work(
    assignment_id: uuid.UUID,
    body: SubmissionCreate,
    scope: SubmitScope,
    session: DbSession,
    context: RequestCtx,
) -> SubmissionRead:
    """A student hands in their own work (text). Late work is refused unless allowed."""
    return SubmissionService(session, context).submit(scope, assignment_id, body.content)


@router.get("/assignment-submissions", response_model=Page[SubmissionRead], responses=ERRORS)
def list_submissions(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    assignment_id: IdQuery = None,
    student_id: IdQuery = None,
    status_filter: Annotated[SubmissionStatus | None, Query(alias="status")] = None,
) -> Page[SubmissionRead]:
    """Staff: the school; faculty: their assignments; parents: linked children (without
    the submitted text); students: their own. Filters only narrow that set."""
    items, total = SubmissionService(session).list(
        scope,
        assignment_id=assignment_id,
        student_id=student_id,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return _page(items, total, pagination)


@router.get(
    "/assignment-submissions/{submission_id}", response_model=SubmissionRead, responses=ERRORS
)
def get_submission(
    submission_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> SubmissionRead:
    return SubmissionService(session).get(scope, submission_id)


@router.post(
    "/assignment-submissions/{submission_id}/revisions",
    response_model=SubmissionRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def resubmit_work(
    submission_id: uuid.UUID,
    body: SubmissionCreate,
    scope: SubmitScope,
    session: DbSession,
    context: RequestCtx,
) -> SubmissionRead:
    """Resubmit (a new revision; earlier ones are kept) while the assignment is published,
    resubmission is allowed and the work has not been reviewed."""
    return SubmissionService(session, context).resubmit(scope, submission_id, body.content)


@router.get(
    "/assignment-submissions/{submission_id}/revisions",
    response_model=list[RevisionRead],
    responses=ERRORS,
)
def list_revisions(
    submission_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> list[RevisionRead]:
    """Every revision of the work, oldest first (student and staff only, not parents)."""
    items = SubmissionService(session).revisions(scope, submission_id)
    return [RevisionRead.model_validate(item) for item in items]


@router.put(
    "/assignment-submissions/{submission_id}/review",
    response_model=SubmissionRead,
    responses=ERRORS,
)
def review_submission(
    submission_id: uuid.UUID,
    body: ReviewInput,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> SubmissionRead:
    """Record feedback and/or marks for the latest revision. Changing an existing review
    needs ``reason``; every change is kept in the review history."""
    return SubmissionService(session, context).review(scope, submission_id, body)


@router.get(
    "/assignment-submissions/{submission_id}/review-history",
    response_model=list[ReviewChangeRead],
    responses=ERRORS,
)
def review_history(
    submission_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> list[ReviewChangeRead]:
    items = SubmissionService(session).review_history(scope, submission_id)
    return [ReviewChangeRead.model_validate(item) for item in items]
