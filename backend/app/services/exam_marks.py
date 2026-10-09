"""Marks entry, bulk entry and corrections.

Rules
-----
* Marks are accepted only while the exam is in ``marks_entry`` (also enforced by a
  trigger), for a component of that exam, by school staff (``marks.manage``) or by a
  faculty member whose effective assignment covers the exam's year + class + the
  student's section + the component's subject **on the assessment date** (the
  component's ``assessment_date``, else the exam's ``start_date``).
* A student is eligible when an enrollment placed them in the exam's year and class (and
  the exam's section, when it names one) on the assessment date. The mark is pinned to
  that enrollment (composite foreign key), so later moves never change past results.
* Every entry is ``present`` (with ``marks_obtained`` between 0 and the component's
  maximum, two decimals at most), ``absent`` or ``exempt``. Students without an entry
  are *pending*: never treated as zero, absent or failed.
* Bulk entry is all-or-nothing and create-only: one duplicate, ineligible, out-of-scope,
  over-maximum or already-entered student rejects the whole request.
* Changing an existing mark is a **correction**: it needs a reason, appends to
  ``exam_mark_changes`` (previous and new values, who, when, why) and updates
  ``corrected_*``. Published results are frozen; correcting them means reopening the
  exam first (see :mod:`app.services.exam_results`).

Concurrency: writes lock the exam row ``FOR SHARE`` (marks writers do not block each
other; publishing, which locks it ``FOR UPDATE``, waits for them and they wait for it),
then corrections lock the mark row. ``UNIQUE (exam_subject_id, student_id)`` and
``UNIQUE (mark_id, sequence)`` are the final authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import ColumnElement

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.models.exams import Exam, ExamMark, ExamMarkChange, ExamStatus, ExamSubject, MarkStatus
from app.models.people import Student, StudentEnrollment
from app.schemas.common import ErrorDetail
from app.schemas.exams import MarkBulkCreate, MarkCorrection, MarkEntry
from app.security.exam_scope import assessment_date, mark_visibility, sections_in_scope
from app.security.school_scope import SchoolScope
from app.services.exams import ExamBase, now
from app.services.students import body_error

M = ExamMark
NOT_ELIGIBLE = "Student is not eligible for this exam subject."
MARK_CONSTRAINTS = {
    "uq_exam_marks_exam_subject_id_student_id": (
        "student_id",
        "Marks are already recorded for this student; correct them instead.",
    ),
    "fk_exam_marks_enrollment_placement": ("student_id", NOT_ELIGIBLE),
    "exam_marks_section_in_exam": ("student_id", NOT_ELIGIBLE),
    "ck_exam_marks_marks_range": ("marks_obtained", "Marks must be between 0 and the maximum."),
    "exam_marks_exam_accepts_marks": ("status", "The exam is not open for marks entry."),
    "uq_exam_mark_changes_mark_id_sequence": (
        "marks_obtained",
        "The mark changed concurrently. Please retry.",
    ),
}

type EligibleRows = list[tuple[Student, StudentEnrollment, ExamMark | None]]


def _over_maximum(value: Decimal | None, component: ExamSubject) -> bool:
    return value is not None and value > component.max_marks


def _maximum_message(component: ExamSubject) -> str:
    return f"Must not exceed the maximum marks ({component.max_marks})."


class ExamMarkService(ExamBase):
    # ------------------------------------------------------------------ helpers
    def _open_exam(self, scope: SchoolScope, exam_id: uuid.UUID) -> Exam:
        """Share-lock the exam (publishing waits for us) and require marks entry."""
        exam = self.visible_exam(scope, exam_id, lock="share")
        if exam.status != ExamStatus.MARKS_ENTRY:
            raise ConflictError(
                f"Marks can only be entered while the exam is open for marks entry "
                f"(it is {exam.status})."
            )
        return exam

    def _writable_sections(
        self, scope: SchoolScope, exam: Exam, component: ExamSubject
    ) -> set[uuid.UUID] | None:
        sections = sections_in_scope(self.session, scope.principal, exam, component, write=True)
        if sections is not None and not sections:
            raise PermissionDeniedError(
                "You are not assigned to this subject for this exam's class and section."
            )
        return sections

    def _new_mark(
        self,
        scope: SchoolScope,
        exam: Exam,
        component: ExamSubject,
        entry: MarkEntry,
        placement: tuple[uuid.UUID, uuid.UUID],
    ) -> ExamMark:
        enrollment_id, section_id = placement
        return ExamMark(
            school_id=scope.school_id,
            exam_id=exam.id,
            exam_subject_id=component.id,
            max_marks=component.max_marks,
            student_id=entry.student_id,
            enrollment_id=enrollment_id,
            academic_year_id=exam.academic_year_id,
            class_id=exam.class_id,
            section_id=section_id,
            status=entry.status.value,
            marks_obtained=entry.marks_obtained,
            remarks=entry.remarks,
            entered_by_user_id=scope.actor_id,
            entered_at=now(),
        )

    def _audit_mark(
        self, action: AuditAction, scope: SchoolScope, mark: ExamMark, **details: object
    ) -> None:
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="exam_mark",
            resource_id=mark.id,
            school_id=scope.school_id,
            exam_id=str(mark.exam_id),
            exam_subject_id=str(mark.exam_subject_id),
            student_id=str(mark.student_id),
            **details,
        )

    # -------------------------------------------------------------------- reads
    def eligible_students(
        self,
        scope: SchoolScope,
        exam_id: uuid.UUID,
        component_id: uuid.UUID,
        *,
        unmarked_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[EligibleRows, int]:
        exam = self.visible_exam(scope, exam_id)
        component = self.component(exam, component_id)
        sections = sections_in_scope(self.session, scope.principal, exam, component, write=False)
        if sections is not None and not sections:
            return [], 0
        return self.marks.eligible_page(
            exam,
            component,
            assessment_date(exam, component),
            sections,
            unmarked_only=unmarked_only,
            offset=offset,
            limit=limit,
        )

    def list(
        self,
        scope: SchoolScope,
        exam_id: uuid.UUID,
        component_id: uuid.UUID,
        *,
        student_id: uuid.UUID | None,
        status: MarkStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[ExamMark], int]:
        exam = self.visible_exam(scope, exam_id)
        component = self.component(exam, component_id)
        filters: list[ColumnElement[bool]] = [M.exam_subject_id == component.id]
        if student_id is not None:
            filters.append(M.student_id == student_id)
        if status is not None:
            filters.append(M.status == status.value)
        return self.marks.list_visible(
            mark_visibility(scope.principal, scope.school_id), filters, offset=offset, limit=limit
        )

    def get(self, scope: SchoolScope, mark_id: uuid.UUID) -> ExamMark:
        row = self.marks.get_visible(mark_visibility(scope.principal, scope.school_id), mark_id)
        if row is None:
            raise NotFoundError("Exam mark not found.")
        return row[0]

    def history(self, scope: SchoolScope, mark_id: uuid.UUID) -> Sequence[ExamMarkChange]:
        return self.marks.changes(self.get(scope, mark_id).id)

    # ------------------------------------------------------------------- writes
    def create(
        self, scope: SchoolScope, exam_id: uuid.UUID, component_id: uuid.UUID, entry: MarkEntry
    ) -> ExamMark:
        exam = self._open_exam(scope, exam_id)
        component = self.component(exam, component_id)
        sections = self._writable_sections(scope, exam, component)
        if _over_maximum(entry.marks_obtained, component):
            raise body_error("marks_obtained", _maximum_message(component))
        if self.marks.students_with_marks(component.id, [entry.student_id]):
            raise ConflictError(
                "Marks are already recorded for this student; correct them instead.",
                details=[ErrorDetail(loc=["body", "student_id"], message="Already recorded.")],
            )
        eligible = self.marks.eligible(
            exam, assessment_date(exam, component), sections, [entry.student_id]
        )
        if entry.student_id not in eligible:
            raise body_error("student_id", NOT_ELIGIBLE)
        mark = self._new_mark(scope, exam, component, entry, eligible[entry.student_id])
        self.writer.flush(MARK_CONSTRAINTS, before=lambda: self.session.add(mark))
        self._audit_mark(
            AuditAction.EXAM_MARK_CREATED,
            scope,
            mark,
            status=mark.status,
            marks_obtained=str(mark.marks_obtained) if mark.marks_obtained is not None else None,
        )
        self.session.commit()
        return mark

    def bulk_create(
        self,
        scope: SchoolScope,
        exam_id: uuid.UUID,
        component_id: uuid.UUID,
        data: MarkBulkCreate,
    ) -> Sequence[ExamMark]:
        exam = self._open_exam(scope, exam_id)
        component = self.component(exam, component_id)
        sections = self._writable_sections(scope, exam, component)
        entries = data.marks
        student_ids = [entry.student_id for entry in entries]

        def problems(indexes: Sequence[int], field: str, message: str) -> list[ErrorDetail]:
            return [ErrorDetail(loc=["body", "marks", i, field], message=message) for i in indexes]

        first_index: dict[uuid.UUID, int] = {}
        repeated = [i for i, sid in enumerate(student_ids) if first_index.setdefault(sid, i) != i]
        if repeated:
            raise BusinessRuleError(
                "Each student may appear only once.",
                details=problems(repeated, "student_id", "Duplicate student in this request."),
            )
        over = [
            i for i, entry in enumerate(entries) if _over_maximum(entry.marks_obtained, component)
        ]
        if over:
            raise BusinessRuleError(
                "Some marks exceed the maximum.",
                details=problems(over, "marks_obtained", _maximum_message(component)),
            )
        eligible = self.marks.eligible(
            exam, assessment_date(exam, component), sections, student_ids
        )
        ineligible = [i for i, sid in enumerate(student_ids) if sid not in eligible]
        if ineligible:
            raise BusinessRuleError(
                "Some students are not eligible for this exam subject.",
                details=problems(ineligible, "student_id", NOT_ELIGIBLE),
            )
        existing = self.marks.students_with_marks(component.id, student_ids)
        if existing:
            raise ConflictError(
                "Marks are already recorded for some students; correct those instead.",
                details=problems(
                    [i for i, sid in enumerate(student_ids) if sid in existing],
                    "student_id",
                    "Already recorded.",
                ),
            )
        created = [
            self._new_mark(scope, exam, component, entry, eligible[entry.student_id])
            for entry in entries
        ]
        self.writer.flush(MARK_CONSTRAINTS, before=lambda: self.session.add_all(created))
        counts = {status.value: 0 for status in MarkStatus}
        for mark in created:
            counts[mark.status] += 1
        self.writer.audit(
            AuditAction.EXAM_MARK_BULK_CREATED,
            actor_id=scope.actor_id,
            resource_type="exam_subject",
            resource_id=component.id,
            school_id=scope.school_id,
            exam_id=str(exam.id),
            subject_id=str(component.subject_id),
            marks=len(created),
            status_counts=counts,
        )
        self.session.commit()
        return created

    def correct(self, scope: SchoolScope, mark_id: uuid.UUID, data: MarkCorrection) -> ExamMark:
        found = self.get(scope, mark_id)
        # Lock order: exam first (share), then the mark (same as create/bulk).
        exam = self._open_exam(scope, found.exam_id)
        component = self.component(exam, found.exam_subject_id)
        sections = self._writable_sections(scope, exam, component)
        if sections is not None and found.section_id not in sections:
            raise PermissionDeniedError("You may not change marks for this student.")
        row = self.marks.get_visible(
            mark_visibility(scope.principal, scope.school_id), mark_id, for_update=True
        )
        assert row is not None  # noqa: S101 - marks are never deleted
        mark = row[0]
        changes = data.changes()
        reason = str(changes.pop("reason"))
        new_status = str(changes.get("status", mark.status))
        new_marks: Decimal | None
        if "marks_obtained" in changes:
            new_marks = changes["marks_obtained"]  # type: ignore[assignment]
        elif new_status != mark.status:
            new_marks = None
        else:
            new_marks = mark.marks_obtained
        if new_status == MarkStatus.PRESENT and new_marks is None:
            raise body_error("marks_obtained", "Required when status is present.")
        if new_status != MarkStatus.PRESENT and new_marks is not None:
            raise body_error("marks_obtained", "Must be omitted when status is absent or exempt.")
        if _over_maximum(new_marks, component):
            raise body_error("marks_obtained", _maximum_message(component))
        new_remarks = changes.get("remarks", mark.remarks)
        if (new_status, new_marks, new_remarks) == (mark.status, mark.marks_obtained, mark.remarks):
            return mark
        at = now()
        change = ExamMarkChange(
            school_id=scope.school_id,
            mark_id=mark.id,
            sequence=self.marks.next_change_sequence(mark.id),
            previous_status=mark.status,
            new_status=new_status,
            previous_marks=mark.marks_obtained,
            new_marks=new_marks,
            previous_remarks=mark.remarks,
            new_remarks=new_remarks,
            reason=reason,
            changed_by_user_id=scope.actor_id,
            changed_at=at,
        )
        previous = (mark.status, mark.marks_obtained)

        def apply() -> None:
            self.session.add(change)
            mark.status = new_status
            mark.marks_obtained = new_marks
            mark.remarks = new_remarks  # type: ignore[assignment]
            mark.corrected_by_user_id = scope.actor_id
            mark.corrected_at = at
            mark.correction_reason = reason

        self.writer.flush(MARK_CONSTRAINTS, before=apply)
        self._audit_mark(
            AuditAction.EXAM_MARK_CORRECTED,
            scope,
            mark,
            change_id=str(change.id),
            sequence=change.sequence,
            previous_status=previous[0],
            new_status=new_status,
            previous_marks=str(previous[1]) if previous[1] is not None else None,
            new_marks=str(new_marks) if new_marks is not None else None,
            reason=reason,
            # Remarks can hold personal details: recorded by name only.
            remarks_changed=new_remarks != change.previous_remarks,
        )
        self.session.commit()
        self.session.refresh(mark)
        return mark
