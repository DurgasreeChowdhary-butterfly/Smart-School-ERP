"""Exam configuration, lifecycle transitions and subject components.

Rules
-----
* Exams are configured by **school staff** (``marks.manage`` and school-wide scope:
  SUPER_ADMIN, SCHOOL_ADMIN). Faculty enter marks but never configure exams; that is
  the same boundary as daily attendance (Phase 6).
* An exam's year, class and section must exist in the school and be active, the section
  must belong to the class, and its dates must lie inside the academic year. Placement
  is immutable (also enforced by a trigger).
* Editable fields depend on the status: ``draft``/``scheduled`` -> everything;
  ``marks_entry`` -> ``pass_percentage`` and ``grade_bands`` only (they do not change
  which marks exist, and published results are frozen snapshots anyway);
  ``published``/``cancelled`` -> nothing.
* Subjects (components) can be added or changed only while the exam is ``draft`` or
  ``scheduled``, and removed only while it is ``draft`` and no mark references them
  (triggers and ``ON DELETE RESTRICT`` enforce it too), so the maximum marks never
  change under existing marks and a scheduled exam always has subjects. A component's
  assessment date lies inside the exam's dates.
* Transitions: ``draft -> scheduled`` (needs a subject) ``-> marks_entry``;
  ``scheduled -> draft``; ``draft``/``scheduled``/``marks_entry -> cancelled`` (reason
  required, terminal). Publishing and reopening live in :mod:`app.services.exam_results`.

Concurrency: every configuration change and transition locks the exam row first
(``SELECT ... FOR UPDATE``), so changes to one exam are serialised and a component
cannot be added while the exam leaves ``scheduled``. The partial unique index on exam
names, ``UNIQUE (exam_id, subject_id)`` and the lifecycle triggers are the final
authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear
from app.models.exams import Exam, ExamStatus, ExamSubject, ExamType
from app.repositories.academic import (
    AcademicYearRepository,
    ClassRepository,
    SectionRepository,
    SubjectRepository,
)
from app.repositories.exams import ExamMarkRepository, ExamRepository, ExamSubjectRepository, Lock
from app.schemas.exams import (
    ExamCreate,
    ExamSubjectCreate,
    ExamSubjectUpdate,
    ExamUpdate,
    GradeBand,
)
from app.security.attendance_scope import is_school_wide
from app.security.exam_scope import exam_visibility
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.students import body_error

E = Exam

EXAM_CONSTRAINTS = {
    "uq_exams_name_per_placement": (
        "name",
        "An exam with this name already exists for this class/section and academic year.",
    ),
    "fk_exams_section_in_class": ("section_id", "Section does not belong to the class."),
    "exams_lifecycle": ("status", "This status change is not allowed."),
    "exams_scheduled_has_subjects": ("status", "Add at least one subject before scheduling."),
}
COMPONENT_CONSTRAINTS = {
    "uq_exam_subjects_exam_id_subject_id": ("subject_id", "This subject is already in the exam."),
    "fk_exam_subjects_subject_same_school": ("subject_id", "Subject not found."),
    "exam_subjects_config_frozen": (
        "subject_id",
        "Subjects can only change while the exam is draft or scheduled.",
    ),
    "fk_exam_marks_component": ("max_marks", "Marks already reference this maximum."),
    "exam_subjects_removal_draft_only": (
        "subject_id",
        "Subjects can only be removed while the exam is draft.",
    ),
}
STAFF_ONLY = "Exams are configured by school staff."
CONFIGURABLE = (ExamStatus.DRAFT, ExamStatus.SCHEDULED)
GRADING_FIELDS = frozenset({"pass_percentage", "grade_bands"})


def now() -> datetime:
    return datetime.now(UTC)


def bands_json(bands: Sequence[GradeBand] | None) -> list[dict[str, Any]] | None:
    """Stored form of grade bands: decimals as strings (JSONB never holds floats)."""
    if bands is None:
        return None
    return [{"grade": b.grade, "min_percentage": str(b.min_percentage)} for b in bands]


def exam_details(exam: Exam) -> dict[str, object]:
    return {
        "exam_id": str(exam.id),
        "academic_year_id": str(exam.academic_year_id),
        "class_id": str(exam.class_id),
        "section_id": str(exam.section_id) if exam.section_id else None,
        "name": exam.name,
        "status": exam.status,
    }


def require_school_staff(scope: SchoolScope, message: str) -> None:
    if not is_school_wide(scope.principal):
        raise PermissionDeniedError(message)


class ExamBase:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.exams = ExamRepository(session)
        self.subjects = ExamSubjectRepository(session)
        self.marks = ExamMarkRepository(session)
        self.writer = ScopedWriter(session, context)

    def visible_exam(self, scope: SchoolScope, exam_id: uuid.UUID, *, lock: Lock = None) -> Exam:
        exam = self.exams.get_visible(
            exam_visibility(scope.principal, scope.school_id), exam_id, lock=lock
        )
        if exam is None:
            raise NotFoundError("Exam not found.")
        return exam

    def component(self, exam: Exam, component_id: uuid.UUID) -> ExamSubject:
        component = self.subjects.get(exam.id, component_id)
        if component is None:
            raise NotFoundError("Exam subject not found.")
        return component

    def audit(self, action: AuditAction, scope: SchoolScope, exam: Exam, **details: object) -> None:
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="exam",
            resource_id=exam.id,
            school_id=scope.school_id,
            **{**exam_details(exam), **details},
        )


class ExamService(ExamBase):
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        super().__init__(session, context)
        self.years = AcademicYearRepository(session)
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.subject_catalog = SubjectRepository(session)

    # -------------------------------------------------------------------- reads
    def get(self, scope: SchoolScope, exam_id: uuid.UUID) -> Exam:
        return self.visible_exam(scope, exam_id)

    def list(
        self,
        scope: SchoolScope,
        *,
        academic_year_id: uuid.UUID | None,
        class_id: uuid.UUID | None,
        section_id: uuid.UUID | None,
        exam_type: ExamType | None,
        status: ExamStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Exam], int]:
        # Filters only narrow the authorised set; they are ANDed with visibility.
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (E.academic_year_id, academic_year_id),
                (E.class_id, class_id),
                (E.section_id, section_id),
                (E.exam_type, exam_type.value if exam_type else None),
                (E.status, status.value if status else None),
            )
            if value is not None
        ]
        return self.exams.list_visible(
            exam_visibility(scope.principal, scope.school_id), filters, offset=offset, limit=limit
        )

    # --------------------------------------------------------------- validation
    def _placement(self, scope: SchoolScope, data: ExamCreate) -> AcademicYear:
        year = self.years.get(scope.school_id, data.academic_year_id)
        if year is None:
            raise body_error("academic_year_id", "Academic year not found.")
        if not year.is_active:
            raise body_error("academic_year_id", "Academic year is inactive.")
        school_class = self.classes.get(scope.school_id, data.class_id)
        if school_class is None:
            raise body_error("class_id", "Class not found.")
        if not school_class.is_active:
            raise body_error("class_id", "Class is inactive.")
        if data.section_id is not None:
            section = self.sections.get(scope.school_id, data.section_id)
            if section is None:
                raise body_error("section_id", "Section not found.")
            if section.class_id != school_class.id:
                raise body_error("section_id", "Section does not belong to the class.")
            if not section.is_active:
                raise body_error("section_id", "Section is inactive.")
        return year

    @staticmethod
    def _dates_in_year(year: AcademicYear, start: date, end: date) -> None:
        if start > end:
            raise body_error("end_date", "Must not be before start_date.")
        if not (year.start_date <= start and end <= year.end_date):
            raise body_error("start_date", "Exam dates must fall within the academic year.")

    # ------------------------------------------------------------------- writes
    def create(self, scope: SchoolScope, data: ExamCreate) -> Exam:
        require_school_staff(scope, STAFF_ONLY)
        year = self._placement(scope, data)
        self._dates_in_year(year, data.start_date, data.end_date)
        exam = Exam(
            school_id=scope.school_id,
            academic_year_id=data.academic_year_id,
            class_id=data.class_id,
            section_id=data.section_id,
            name=data.name,
            description=data.description,
            exam_type=data.exam_type.value,
            start_date=data.start_date,
            end_date=data.end_date,
            pass_percentage=data.pass_percentage,
            grade_bands=bands_json(data.grade_bands),
            created_by_user_id=scope.actor_id,
        )
        self.writer.flush(EXAM_CONSTRAINTS, before=lambda: self.session.add(exam))
        self.audit(
            AuditAction.EXAM_CREATED,
            scope,
            exam,
            exam_type=exam.exam_type,
            start_date=str(exam.start_date),
            end_date=str(exam.end_date),
        )
        self.session.commit()
        return exam

    def update(self, scope: SchoolScope, exam_id: uuid.UUID, data: ExamUpdate) -> Exam:
        exam = self.visible_exam(scope, exam_id, lock="update")
        require_school_staff(scope, STAFF_ONLY)
        changes = data.changes()
        if exam.status == ExamStatus.MARKS_ENTRY and set(changes) - GRADING_FIELDS:
            raise ConflictError(
                "During marks entry only pass_percentage and grade_bands can change."
            )
        if exam.status not in (*CONFIGURABLE, ExamStatus.MARKS_ENTRY):
            raise ConflictError(f"A {exam.status} exam cannot be changed.")
        if "exam_type" in changes:
            changes["exam_type"] = str(changes["exam_type"])
        if "grade_bands" in changes:
            changes["grade_bands"] = bands_json(changes["grade_bands"])  # type: ignore[arg-type]
        start: date = changes.get("start_date", exam.start_date)  # type: ignore[assignment]
        end: date = changes.get("end_date", exam.end_date)  # type: ignore[assignment]
        if "start_date" in changes or "end_date" in changes:
            year = self.years.get(scope.school_id, exam.academic_year_id)
            assert year is not None  # noqa: S101 - restricted foreign key
            self._dates_in_year(year, start, end)
            if self.subjects.outside(exam.id, start, end):
                raise body_error(
                    "start_date", "Some subject assessment dates would fall outside the exam."
                )
        diff: list[dict[str, object]] = []
        self.writer.flush(
            EXAM_CONSTRAINTS, before=lambda: diff.extend(apply_changes(exam, changes))
        )
        if diff:
            self.audit(AuditAction.EXAM_UPDATED, scope, exam, changes=diff)
        self.session.commit()
        self.session.refresh(exam)
        return exam

    # -------------------------------------------------------------- transitions
    def _transition(
        self,
        scope: SchoolScope,
        exam_id: uuid.UUID,
        *,
        allowed: tuple[ExamStatus, ...],
        to: ExamStatus,
        verb: str,
        action: AuditAction,
        reason: str | None = None,
    ) -> Exam:
        exam = self.visible_exam(scope, exam_id, lock="update")
        require_school_staff(scope, STAFF_ONLY)
        if exam.status not in allowed:
            raise ConflictError(f"Cannot {verb} an exam that is {exam.status}.")
        if to is ExamStatus.SCHEDULED and not self.subjects.for_exam(exam.id):
            raise body_error("status", "Add at least one subject before scheduling.")
        previous = exam.status

        def apply() -> None:
            exam.status = to.value
            if to is ExamStatus.CANCELLED:
                exam.cancelled_by_user_id = scope.actor_id
                exam.cancelled_at = now()
                exam.cancellation_reason = reason

        self.writer.flush(EXAM_CONSTRAINTS, before=apply)
        self.audit(action, scope, exam, previous_status=previous, reason=reason)
        self.session.commit()
        self.session.refresh(exam)
        return exam

    def schedule(self, scope: SchoolScope, exam_id: uuid.UUID) -> Exam:
        return self._transition(
            scope,
            exam_id,
            allowed=(ExamStatus.DRAFT,),
            to=ExamStatus.SCHEDULED,
            verb="schedule",
            action=AuditAction.EXAM_SCHEDULED,
        )

    def return_to_draft(self, scope: SchoolScope, exam_id: uuid.UUID) -> Exam:
        return self._transition(
            scope,
            exam_id,
            allowed=(ExamStatus.SCHEDULED,),
            to=ExamStatus.DRAFT,
            verb="return to draft",
            action=AuditAction.EXAM_RETURNED_TO_DRAFT,
        )

    def open_marks_entry(self, scope: SchoolScope, exam_id: uuid.UUID) -> Exam:
        return self._transition(
            scope,
            exam_id,
            allowed=(ExamStatus.SCHEDULED,),
            to=ExamStatus.MARKS_ENTRY,
            verb="open marks entry for",
            action=AuditAction.EXAM_MARKS_ENTRY_OPENED,
        )

    def cancel(self, scope: SchoolScope, exam_id: uuid.UUID, reason: str) -> Exam:
        return self._transition(
            scope,
            exam_id,
            allowed=(*CONFIGURABLE, ExamStatus.MARKS_ENTRY),
            to=ExamStatus.CANCELLED,
            verb="cancel",
            action=AuditAction.EXAM_CANCELLED,
            reason=reason,
        )


class ExamSubjectService(ExamBase):
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        super().__init__(session, context)
        self.subject_catalog = SubjectRepository(session)

    def list(
        self, scope: SchoolScope, exam_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[Sequence[ExamSubject], int]:
        exam = self.visible_exam(scope, exam_id)
        return self.subjects.page(exam.id, offset=offset, limit=limit)

    def get(self, scope: SchoolScope, exam_id: uuid.UUID, component_id: uuid.UUID) -> ExamSubject:
        return self.component(self.visible_exam(scope, exam_id), component_id)

    def _configurable_exam(self, scope: SchoolScope, exam_id: uuid.UUID) -> Exam:
        exam = self.visible_exam(scope, exam_id, lock="update")
        require_school_staff(scope, STAFF_ONLY)
        if exam.status not in CONFIGURABLE:
            raise ConflictError("Subjects can only change while the exam is draft or scheduled.")
        return exam

    @staticmethod
    def _date_in_exam(exam: Exam, on: date | None) -> None:
        if on is not None and not exam.start_date <= on <= exam.end_date:
            raise body_error("assessment_date", "Must fall within the exam's dates.")

    def _audit(
        self,
        action: AuditAction,
        scope: SchoolScope,
        exam: Exam,
        item: ExamSubject,
        **extra: object,
    ) -> None:
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="exam_subject",
            resource_id=item.id,
            school_id=scope.school_id,
            exam_id=str(exam.id),
            subject_id=str(item.subject_id),
            **extra,
        )

    def create(
        self, scope: SchoolScope, exam_id: uuid.UUID, data: ExamSubjectCreate
    ) -> ExamSubject:
        exam = self._configurable_exam(scope, exam_id)
        subject = self.subject_catalog.get(scope.school_id, data.subject_id)
        if subject is None:
            raise body_error("subject_id", "Subject not found.")
        if not subject.is_active:
            raise body_error("subject_id", "Subject is inactive.")
        self._date_in_exam(exam, data.assessment_date)
        item = ExamSubject(
            school_id=scope.school_id,
            exam_id=exam.id,
            created_by_user_id=scope.actor_id,
            **data.model_dump(),
        )
        self.writer.flush(COMPONENT_CONSTRAINTS, before=lambda: self.session.add(item))
        self._audit(
            AuditAction.EXAM_SUBJECT_CREATED,
            scope,
            exam,
            item,
            max_marks=str(item.max_marks),
            qualifying_marks=str(item.pass_marks) if item.pass_marks is not None else None,
            assessment_date=str(item.assessment_date) if item.assessment_date else None,
        )
        self.session.commit()
        return item

    def update(
        self,
        scope: SchoolScope,
        exam_id: uuid.UUID,
        component_id: uuid.UUID,
        data: ExamSubjectUpdate,
    ) -> ExamSubject:
        exam = self._configurable_exam(scope, exam_id)
        item = self.component(exam, component_id)
        changes = data.changes()
        maximum = changes.get("max_marks", item.max_marks)
        passing = changes.get("pass_marks", item.pass_marks)
        if passing is not None and passing > maximum:  # type: ignore[operator]
            raise body_error("pass_marks", "Must not exceed max_marks.")
        start = changes.get("start_time", item.start_time)
        end = changes.get("end_time", item.end_time)
        if start is not None and end is not None and start >= end:  # type: ignore[operator]
            raise body_error("end_time", "Must be after start_time.")
        if "assessment_date" in changes:
            self._date_in_exam(exam, changes["assessment_date"])  # type: ignore[arg-type]
        diff: list[dict[str, object]] = []
        self.writer.flush(
            COMPONENT_CONSTRAINTS, before=lambda: diff.extend(apply_changes(item, changes))
        )
        if diff:
            self._audit(AuditAction.EXAM_SUBJECT_UPDATED, scope, exam, item, changes=diff)
        self.session.commit()
        self.session.refresh(item)
        return item

    def delete(self, scope: SchoolScope, exam_id: uuid.UUID, component_id: uuid.UUID) -> None:
        """Remove a subject from a **draft** exam that has no dependent records.

        Only draft configuration can be removed: a scheduled exam must return to draft
        first (so it can never be left scheduled without subjects), and anything from
        marks entry onwards is frozen history. A subject that any mark references is
        never removed (checked here, and ``ON DELETE RESTRICT`` in the database). The
        removal is audited with the removed values; a trigger enforces the same rules.
        """
        exam = self.visible_exam(scope, exam_id, lock="update")
        require_school_staff(scope, STAFF_ONLY)
        if exam.status != ExamStatus.DRAFT:
            raise ConflictError(
                "Subjects can only be removed while the exam is draft; "
                "return a scheduled exam to draft first."
            )
        item = self.component(exam, component_id)
        if self.marks.exists_for_component(item.id):
            raise ConflictError("Marks reference this subject; it cannot be removed.")
        snapshot = {
            "max_marks": str(item.max_marks),
            "qualifying_marks": str(item.pass_marks) if item.pass_marks is not None else None,
        }
        self.writer.flush(COMPONENT_CONSTRAINTS, before=lambda: self.session.delete(item))
        self._audit(AuditAction.EXAM_SUBJECT_DELETED, scope, exam, item, **snapshot)
        self.session.commit()
