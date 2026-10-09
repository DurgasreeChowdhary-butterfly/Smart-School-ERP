"""Student enrollments: append-only academic placement history.

Rules
-----
* Year, class and section must be active and belong to the school; the section
  must belong to the class (also enforced by composite foreign keys).
* At most one *active* enrollment per student per academic year, and at most one
  *current* enrollment per student (partial unique indexes).
* Moving a student within a year never rewrites history: the new enrollment names
  the one it ``supersedes``; the old row is closed (status ``superseded``,
  ``end_date`` set) and stays in the history.
* Year/class/section/student of an existing enrollment are immutable.

Concurrency: every write locks the student row first (``SELECT ... FOR UPDATE``),
so concurrent changes for one student are serialised; the unique indexes catch
anything that bypasses the lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear
from app.models.people import EnrollmentStatus, Student, StudentEnrollment, StudentStatus
from app.repositories.academic import (
    AcademicYearRepository,
    ClassRepository,
    SectionRepository,
)
from app.repositories.people import EnrollmentRepository, StudentRepository
from app.schemas.people import EnrollmentCreate, EnrollmentUpdate
from app.security.people_scope import enrollment_visibility, student_visibility
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.students import body_error

ENROLLMENT_CONSTRAINTS = {
    "uq_student_enrollments_one_active_per_year": (
        "academic_year_id",
        "The student already has an active enrollment in this academic year.",
    ),
    "uq_student_enrollments_one_current": (
        "is_current",
        "The student's current enrollment changed concurrently. Please retry.",
    ),
    "fk_enrollments_section_in_class": ("section_id", "Section does not belong to the class."),
}


class EnrollmentService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.enrollments = EnrollmentRepository(session)
        self.students = StudentRepository(session)
        self.years = AcademicYearRepository(session)
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.writer = ScopedWriter(session, context)

    def _lock_student(self, scope: SchoolScope, student_id: uuid.UUID) -> Student | None:
        return self.students.get_visible(
            student_visibility(scope.principal, scope.school_id), student_id, for_update=True
        )

    def get(self, scope: SchoolScope, enrollment_id: uuid.UUID) -> StudentEnrollment:
        item = self.enrollments.get_visible(
            enrollment_visibility(scope.principal, scope.school_id), enrollment_id
        )
        if item is None:
            raise NotFoundError("Enrollment not found.")
        return item

    def list(
        self,
        scope: SchoolScope,
        *,
        student_id: uuid.UUID | None,
        academic_year_id: uuid.UUID | None,
        class_id: uuid.UUID | None,
        section_id: uuid.UUID | None,
        is_current: bool | None,
        status: EnrollmentStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[StudentEnrollment], int]:
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (StudentEnrollment.student_id, student_id),
                (StudentEnrollment.academic_year_id, academic_year_id),
                (StudentEnrollment.class_id, class_id),
                (StudentEnrollment.section_id, section_id),
                (StudentEnrollment.is_current, is_current),
                (StudentEnrollment.status, status.value if status else None),
            )
            if value is not None
        ]
        return self.enrollments.list_visible(
            enrollment_visibility(scope.principal, scope.school_id),
            filters,
            offset=offset,
            limit=limit,
        )

    # ------------------------------------------------------------------- create
    def _validate_placement(self, scope: SchoolScope, data: EnrollmentCreate) -> AcademicYear:
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
        section = self.sections.get(scope.school_id, data.section_id)
        if section is None:
            raise body_error("section_id", "Section not found.")
        if section.class_id != school_class.id:
            raise body_error("section_id", "Section does not belong to the class.")
        if not section.is_active:
            raise body_error("section_id", "Section is inactive.")
        if not year.start_date <= data.enrollment_date <= year.end_date:
            raise body_error("enrollment_date", "Must fall within the academic year.")
        return year

    def create(self, scope: SchoolScope, data: EnrollmentCreate) -> StudentEnrollment:
        student = self._lock_student(scope, data.student_id)
        if student is None:
            raise body_error("student_id", "Student not found.")
        if student.status != StudentStatus.ACTIVE:
            raise body_error("student_id", "Only active students can be enrolled.")
        self._validate_placement(scope, data)

        existing = self.enrollments.active_in_year(student.id, data.academic_year_id)
        was_current = False
        if existing is not None:
            if data.supersedes_enrollment_id not in (None, existing.id):
                raise body_error(
                    "supersedes_enrollment_id",
                    "Does not match the student's active enrollment for this academic year.",
                )
            if data.supersedes_enrollment_id is None:
                raise ConflictError(
                    "The student already has an active enrollment in this academic year. "
                    "To move them, pass supersedes_enrollment_id."
                )
            if data.enrollment_date < existing.enrollment_date:
                raise body_error(
                    "enrollment_date", "Must not be before the superseded enrollment's start."
                )
        elif data.supersedes_enrollment_id is not None:
            raise body_error(
                "supersedes_enrollment_id",
                "Does not match the student's active enrollment for this academic year.",
            )

        previous_current = self.enrollments.current_id(student.id)
        enrollment = StudentEnrollment(
            school_id=scope.school_id,
            student_id=student.id,
            academic_year_id=data.academic_year_id,
            class_id=data.class_id,
            section_id=data.section_id,
            enrollment_number=data.enrollment_number,
            enrollment_date=data.enrollment_date,
        )

        def insert() -> None:
            nonlocal was_current
            if existing is not None:
                was_current = existing.is_current
                existing.status = EnrollmentStatus.SUPERSEDED.value
                existing.end_date = data.enrollment_date
                existing.is_current = False
                self.session.flush()  # close the old row before the new active one exists
            enrollment.is_current = data.is_current or was_current
            if enrollment.is_current:
                self.enrollments.clear_current(student.id)
            self.session.add(enrollment)

        self.writer.flush(ENROLLMENT_CONSTRAINTS, before=insert)
        self.writer.audit(
            AuditAction.ENROLLMENT_CREATED,
            actor_id=scope.actor_id,
            resource_type="enrollment",
            resource_id=enrollment.id,
            school_id=scope.school_id,
            student_id=str(student.id),
            academic_year_id=str(enrollment.academic_year_id),
            class_id=str(enrollment.class_id),
            section_id=str(enrollment.section_id),
            superseded_enrollment_id=str(existing.id) if existing else None,
        )
        if enrollment.is_current and previous_current != enrollment.id:
            self._audit_current_changed(scope, enrollment, previous_current)
        self.session.commit()
        return enrollment

    # ------------------------------------------------------------------- update
    def update(
        self, scope: SchoolScope, enrollment_id: uuid.UUID, data: EnrollmentUpdate
    ) -> StudentEnrollment:
        found = self.get(scope, enrollment_id)
        # Lock order: student row first, then the enrollment (same as create).
        self._lock_student(scope, found.student_id)
        enrollment = self.enrollments.get_visible(
            enrollment_visibility(scope.principal, scope.school_id), enrollment_id, for_update=True
        )
        assert enrollment is not None  # noqa: S101 - visible a moment ago, row now locked
        changes = data.changes()

        if enrollment.status == EnrollmentStatus.SUPERSEDED and set(changes) - {
            "enrollment_number"
        }:
            raise BusinessRuleError(
                "Superseded enrollments are history; only enrollment_number can change."
            )
        new_status = changes.get("status", enrollment.status)
        if new_status != EnrollmentStatus.ACTIVE:
            if changes.get("is_current") is True:
                raise body_error("is_current", "Only active enrollments can be current.")
            changes["is_current"] = False  # leaving 'active' always ends currency
        start = data.enrollment_date or enrollment.enrollment_date
        end = changes.get("end_date", enrollment.end_date)
        year = self.years.get(scope.school_id, enrollment.academic_year_id)
        assert year is not None  # noqa: S101 - guaranteed by the composite foreign key
        if not year.start_date <= start <= year.end_date:
            raise body_error("enrollment_date", "Must fall within the academic year.")
        if end is not None and end < start:  # type: ignore[operator]
            raise body_error("end_date", "Must not be before enrollment_date.")

        becomes_current = changes.get("is_current") is True and not enrollment.is_current
        previous_current = self.enrollments.current_id(enrollment.student_id)

        def apply() -> list[dict[str, object]]:
            if becomes_current:
                self.enrollments.clear_current(enrollment.student_id, except_id=enrollment.id)
            return apply_changes(enrollment, changes)

        diff: list[dict[str, object]] = []

        def before() -> None:
            diff.extend(apply())

        self.writer.flush(ENROLLMENT_CONSTRAINTS, before=before)
        if diff:
            self.writer.audit(
                AuditAction.ENROLLMENT_UPDATED,
                actor_id=scope.actor_id,
                resource_type="enrollment",
                resource_id=enrollment.id,
                school_id=scope.school_id,
                student_id=str(enrollment.student_id),
                changes=diff,
            )
            if becomes_current:
                self._audit_current_changed(scope, enrollment, previous_current)
        self.session.commit()
        self.session.refresh(enrollment)
        return enrollment

    def _audit_current_changed(
        self,
        scope: SchoolScope,
        enrollment: StudentEnrollment,
        previous_current: uuid.UUID | None,
    ) -> None:
        self.writer.audit(
            AuditAction.ENROLLMENT_CURRENT_CHANGED,
            actor_id=scope.actor_id,
            resource_type="enrollment",
            resource_id=enrollment.id,
            school_id=scope.school_id,
            student_id=str(enrollment.student_id),
            previous_current_id=str(previous_current) if previous_current else None,
        )
