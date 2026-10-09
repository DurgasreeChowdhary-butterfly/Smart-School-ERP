"""Exams, exam subjects, marks, results and report cards.

Reads need ``marks.read`` and are narrowed to the caller's scope (school staff: school;
faculty: their classes/sections and subjects; parents: linked children's published
report cards; students: their own). Configuring exams and entering marks need
``marks.manage`` (exam configuration: school staff only; marks: staff, or faculty within
their assignments). Publishing and reopening need ``marks.publish``. Anything outside the
caller's scope is 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import DbSession, Pagination, RequestCtx, require_school_permission
from app.api.v1._common import ERRORS, to_page
from app.models.exams import ExamStatus, ExamType, MarkStatus
from app.schemas.common import Page
from app.schemas.exams import (
    EligibleStudent,
    ExamCreate,
    ExamRead,
    ExamSubjectCreate,
    ExamSubjectRead,
    ExamSubjectUpdate,
    ExamUpdate,
    MarkBulkCreate,
    MarkBulkResult,
    MarkChangeRead,
    MarkCorrection,
    MarkEntry,
    MarkRead,
    PublicationRead,
    PublishInput,
    ReasonInput,
    ReportCardRead,
    StudentResult,
)
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services.exam_marks import ExamMarkService
from app.services.exam_results import ExamResultService
from app.services.exams import ExamService, ExamSubjectService

P = PermissionName
router = APIRouter(tags=["exams"])

ReadScope = Annotated[SchoolScope, Depends(require_school_permission(P.MARKS_READ))]
ManageScope = Annotated[SchoolScope, Depends(require_school_permission(P.MARKS_MANAGE))]
PublishScope = Annotated[SchoolScope, Depends(require_school_permission(P.MARKS_PUBLISH))]
IdQuery = Annotated[uuid.UUID | None, Query()]
SUBJECT = "/exams/{exam_id}/subjects/{exam_subject_id}"


def _page[T](items: list[T], total: int, pagination: Pagination) -> Page[T]:
    return Page[T].create(items, total=total, page=pagination.page, page_size=pagination.page_size)


# ============================================================================= exams
@router.post(
    "/exams", response_model=ExamRead, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
def create_exam(
    body: ExamCreate, scope: ManageScope, session: DbSession, context: RequestCtx
) -> ExamRead:
    """Create a **draft** exam for a class (omit ``section_id``) or one section.
    School staff only."""
    return ExamRead.model_validate(ExamService(session, context).create(scope, body))


@router.get("/exams", response_model=Page[ExamRead], responses=ERRORS)
def list_exams(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    academic_year_id: IdQuery = None,
    class_id: IdQuery = None,
    section_id: IdQuery = None,
    exam_type: Annotated[ExamType | None, Query()] = None,
    status_filter: Annotated[ExamStatus | None, Query(alias="status")] = None,
) -> Page[ExamRead]:
    """Staff: the school's exams; faculty: exams of classes/sections they teach.
    Filters only narrow that set."""
    items, total = ExamService(session).list(
        scope,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        exam_type=exam_type,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(ExamRead, items, total, pagination)


@router.get("/exams/{exam_id}", response_model=ExamRead, responses=ERRORS)
def get_exam(exam_id: uuid.UUID, scope: ReadScope, session: DbSession) -> ExamRead:
    return ExamRead.model_validate(ExamService(session).get(scope, exam_id))


@router.patch("/exams/{exam_id}", response_model=ExamRead, responses=ERRORS)
def update_exam(
    exam_id: uuid.UUID,
    body: ExamUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> ExamRead:
    """Draft/scheduled: any field. Marks entry: ``pass_percentage``/``grade_bands`` only.
    Published or cancelled: read-only."""
    return ExamRead.model_validate(ExamService(session, context).update(scope, exam_id, body))


@router.post("/exams/{exam_id}/schedule", response_model=ExamRead, responses=ERRORS)
def schedule_exam(
    exam_id: uuid.UUID, scope: ManageScope, session: DbSession, context: RequestCtx
) -> ExamRead:
    """``draft -> scheduled``; needs at least one subject."""
    return ExamRead.model_validate(ExamService(session, context).schedule(scope, exam_id))


@router.post("/exams/{exam_id}/return-to-draft", response_model=ExamRead, responses=ERRORS)
def return_exam_to_draft(
    exam_id: uuid.UUID, scope: ManageScope, session: DbSession, context: RequestCtx
) -> ExamRead:
    """``scheduled -> draft``."""
    return ExamRead.model_validate(ExamService(session, context).return_to_draft(scope, exam_id))


@router.post("/exams/{exam_id}/open-marks-entry", response_model=ExamRead, responses=ERRORS)
def open_marks_entry(
    exam_id: uuid.UUID, scope: ManageScope, session: DbSession, context: RequestCtx
) -> ExamRead:
    """``scheduled -> marks_entry``: subjects are frozen and marks can be entered."""
    return ExamRead.model_validate(ExamService(session, context).open_marks_entry(scope, exam_id))


@router.post("/exams/{exam_id}/cancel", response_model=ExamRead, responses=ERRORS)
def cancel_exam(
    exam_id: uuid.UUID,
    body: ReasonInput,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> ExamRead:
    """Draft, scheduled or marks-entry exams; terminal. Entered marks are kept."""
    return ExamRead.model_validate(
        ExamService(session, context).cancel(scope, exam_id, body.reason)
    )


@router.post(
    "/exams/{exam_id}/publish",
    response_model=PublicationRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def publish_results(
    exam_id: uuid.UUID,
    scope: PublishScope,
    session: DbSession,
    context: RequestCtx,
    body: PublishInput | None = None,
) -> PublicationRead:
    """``marks_entry -> published``: freezes every student's report card as a new
    publication version. Refused while marks are missing unless ``allow_incomplete``."""
    allow = body.allow_incomplete if body else False
    return ExamResultService(session, context).publish(scope, exam_id, allow_incomplete=allow)


@router.post("/exams/{exam_id}/reopen", response_model=PublicationRead, responses=ERRORS)
def reopen_results(
    exam_id: uuid.UUID,
    body: ReasonInput,
    scope: PublishScope,
    session: DbSession,
    context: RequestCtx,
) -> PublicationRead:
    """``published -> marks_entry``: withdraws the active publication (kept as history,
    hidden from parents and students) so marks can be corrected and republished."""
    return ExamResultService(session, context).reopen(scope, exam_id, body.reason)


@router.get("/exams/{exam_id}/results", response_model=Page[StudentResult], responses=ERRORS)
def preview_results(
    exam_id: uuid.UUID, scope: ReadScope, session: DbSession, pagination: Pagination
) -> Page[StudentResult]:
    """Live results calculated from the current marks. School staff only; this is a
    preview, not a publication."""
    items, total = ExamResultService(session).preview(
        scope, exam_id, offset=pagination.offset, limit=pagination.limit
    )
    return _page(items, total, pagination)


@router.get("/exams/{exam_id}/publications", response_model=list[PublicationRead], responses=ERRORS)
def list_publications(
    exam_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> list[PublicationRead]:
    """Every publication of the exam, oldest first (school staff only)."""
    return ExamResultService(session).publications(scope, exam_id)


@router.get("/exams/{exam_id}/report-cards", response_model=Page[ReportCardRead], responses=ERRORS)
def list_exam_report_cards(
    exam_id: uuid.UUID,
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Page[ReportCardRead]:
    """Report cards of the active publication, or of ``version`` (school staff only)."""
    items, total = ExamResultService(session).exam_report_cards(
        scope, exam_id, version=version, offset=pagination.offset, limit=pagination.limit
    )
    return _page(items, total, pagination)


# ===================================================================== exam subjects
@router.post(
    "/exams/{exam_id}/subjects",
    response_model=ExamSubjectRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_exam_subject(
    exam_id: uuid.UUID,
    body: ExamSubjectCreate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> ExamSubjectRead:
    """Add a subject with its maximum (and optional passing) marks. Draft/scheduled only;
    one entry per subject."""
    item = ExamSubjectService(session, context).create(scope, exam_id, body)
    return ExamSubjectRead.model_validate(item)


@router.get("/exams/{exam_id}/subjects", response_model=Page[ExamSubjectRead], responses=ERRORS)
def list_exam_subjects(
    exam_id: uuid.UUID, scope: ReadScope, session: DbSession, pagination: Pagination
) -> Page[ExamSubjectRead]:
    items, total = ExamSubjectService(session).list(
        scope, exam_id, offset=pagination.offset, limit=pagination.limit
    )
    return to_page(ExamSubjectRead, items, total, pagination)


@router.get(SUBJECT, response_model=ExamSubjectRead, responses=ERRORS)
def get_exam_subject(
    exam_id: uuid.UUID, exam_subject_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> ExamSubjectRead:
    item = ExamSubjectService(session).get(scope, exam_id, exam_subject_id)
    return ExamSubjectRead.model_validate(item)


@router.patch(SUBJECT, response_model=ExamSubjectRead, responses=ERRORS)
def update_exam_subject(
    exam_id: uuid.UUID,
    exam_subject_id: uuid.UUID,
    body: ExamSubjectUpdate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> ExamSubjectRead:
    item = ExamSubjectService(session, context).update(scope, exam_id, exam_subject_id, body)
    return ExamSubjectRead.model_validate(item)


@router.delete(SUBJECT, status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def delete_exam_subject(
    exam_id: uuid.UUID,
    exam_subject_id: uuid.UUID,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> Response:
    """Remove a subject from a **draft** exam that no mark references (audited). A
    scheduled exam must return to draft first; later states are frozen."""
    ExamSubjectService(session, context).delete(scope, exam_id, exam_subject_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================================= marks
@router.get(f"{SUBJECT}/students", response_model=Page[EligibleStudent], responses=ERRORS)
def list_eligible_students(
    exam_id: uuid.UUID,
    exam_subject_id: uuid.UUID,
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    unmarked: Annotated[bool, Query(description="Only students without an entry.")] = False,
) -> Page[EligibleStudent]:
    """Students eligible for this subject (enrolled in the exam's class/section on the
    assessment date) with their entry, if any. Faculty see their own sections only."""
    rows, total = ExamMarkService(session).eligible_students(
        scope,
        exam_id,
        exam_subject_id,
        unmarked_only=unmarked,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return _page(
        [
            EligibleStudent(
                student_id=student.id,
                enrollment_id=enrollment.id,
                section_id=enrollment.section_id,
                admission_number=student.admission_number,
                first_name=student.first_name,
                last_name=student.last_name,
                mark_id=mark.id if mark else None,
                status=mark.status if mark else None,
                marks_obtained=mark.marks_obtained if mark else None,
            )
            for student, enrollment, mark in rows
        ],
        total,
        pagination,
    )


@router.post(
    f"{SUBJECT}/marks",
    response_model=MarkRead,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_mark(
    exam_id: uuid.UUID,
    exam_subject_id: uuid.UUID,
    body: MarkEntry,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> MarkRead:
    item = ExamMarkService(session, context).create(scope, exam_id, exam_subject_id, body)
    return MarkRead.model_validate(item)


@router.post(
    f"{SUBJECT}/marks/bulk",
    response_model=MarkBulkResult,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def bulk_create_marks(
    exam_id: uuid.UUID,
    exam_subject_id: uuid.UUID,
    body: MarkBulkCreate,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> MarkBulkResult:
    """Enter many students at once. All-or-nothing: any duplicate, ineligible,
    out-of-scope, over-maximum or already-entered student rejects the whole request."""
    items = ExamMarkService(session, context).bulk_create(scope, exam_id, exam_subject_id, body)
    return MarkBulkResult(
        exam_subject_id=exam_subject_id,
        created=len(items),
        items=[MarkRead.model_validate(item) for item in items],
    )


@router.get(f"{SUBJECT}/marks", response_model=Page[MarkRead], responses=ERRORS)
def list_marks(
    exam_id: uuid.UUID,
    exam_subject_id: uuid.UUID,
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    student_id: IdQuery = None,
    status_filter: Annotated[MarkStatus | None, Query(alias="status")] = None,
) -> Page[MarkRead]:
    items, total = ExamMarkService(session).list(
        scope,
        exam_id,
        exam_subject_id,
        student_id=student_id,
        status=status_filter,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return to_page(MarkRead, items, total, pagination)


@router.get("/exam-marks/{mark_id}", response_model=MarkRead, responses=ERRORS)
def get_mark(mark_id: uuid.UUID, scope: ReadScope, session: DbSession) -> MarkRead:
    return MarkRead.model_validate(ExamMarkService(session).get(scope, mark_id))


@router.patch("/exam-marks/{mark_id}", response_model=MarkRead, responses=ERRORS)
def correct_mark(
    mark_id: uuid.UUID,
    body: MarkCorrection,
    scope: ManageScope,
    session: DbSession,
    context: RequestCtx,
) -> MarkRead:
    """Correct an entry (``reason`` required). The previous values are appended to the
    mark's history. Only while the exam is in marks entry."""
    return MarkRead.model_validate(ExamMarkService(session, context).correct(scope, mark_id, body))


@router.get("/exam-marks/{mark_id}/history", response_model=list[MarkChangeRead], responses=ERRORS)
def get_mark_history(
    mark_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> list[MarkChangeRead]:
    """Every correction of the mark, oldest first (who, when, from, to, why)."""
    changes = ExamMarkService(session).history(scope, mark_id)
    return [MarkChangeRead.model_validate(change) for change in changes]


# ====================================================================== report cards
@router.get("/report-cards", response_model=Page[ReportCardRead], responses=ERRORS)
def list_report_cards(
    scope: ReadScope,
    session: DbSession,
    pagination: Pagination,
    student_id: IdQuery = None,
    academic_year_id: IdQuery = None,
    exam_id: IdQuery = None,
) -> Page[ReportCardRead]:
    """Published report cards: staff see the school's, parents their linked children's,
    students their own. Filters only narrow that set."""
    items, total = ExamResultService(session).report_cards(
        scope,
        student_id=student_id,
        academic_year_id=academic_year_id,
        exam_id=exam_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return _page(items, total, pagination)


@router.get("/report-cards/{report_card_id}", response_model=ReportCardRead, responses=ERRORS)
def get_report_card(
    report_card_id: uuid.UUID, scope: ReadScope, session: DbSession
) -> ReportCardRead:
    return ExamResultService(session).report_card(scope, report_card_id)
