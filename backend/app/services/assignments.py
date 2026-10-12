"""Assignments: creation, editing, publication, closure and the student roster.

Rules
-----
* An assignment names a year, class, section and subject of the school, all active, with
  the section in the class. ``assigned_on`` (school-local date) lies in the academic
  year; ``due_at`` is an absolute instant whose **school-local** date is on or after
  ``assigned_on`` and inside the academic year. Placement is immutable.
* Who manages: school staff with ``assignment.manage`` (SUPER_ADMIN, SCHOOL_ADMIN) for the
  whole school; faculty only with an effective assignment for that year + class +
  section + subject, today and on ``assigned_on``. A refused create returns 403 before
  any record is looked up, so it reveals nothing.
* Lifecycle: ``draft -> published -> closed``. Publishing needs a future ``due_at``.
  Draft: every field editable. Published: title, instructions, ``due_at``,
  ``allow_late``, ``allow_resubmission``. ``max_marks`` and ``assigned_on`` are fixed
  once published (a trigger enforces it too). Closed: read-only.

Concurrency: every write locks the assignment row (``FOR UPDATE``); submissions hold it
``FOR SHARE``, so closing waits for in-flight submissions and they then see it closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear
from app.models.assignments import Assignment, AssignmentLifecycle
from app.models.school import School
from app.repositories.academic import (
    AcademicYearRepository,
    ClassRepository,
    SectionRepository,
    SubjectRepository,
)
from app.repositories.assignments import (
    AssignmentRepository,
    Lock,
    StudentRows,
    SubmissionRepository,
)
from app.schemas.assignments import AssignmentCreate, AssignmentUpdate
from app.security.assignment_scope import (
    assignment_manageability,
    assignment_staff_reach,
    assignment_visibility,
)
from app.security.attendance_scope import faculty_teaches, is_school_wide
from app.security.permissions import PermissionName
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.students import body_error

A = Assignment
ASSIGNMENT_CONSTRAINTS = {
    "fk_assignments_section_in_class": ("section_id", "Section does not belong to the class."),
    "assignments_lifecycle": ("status", "This status change is not allowed."),
    "assignments_settings_frozen": (
        "max_marks",
        "Grading settings and the assignment date are fixed once published.",
    ),
}
PUBLISHED_EDITABLE = frozenset(
    {"title", "instructions", "due_at", "allow_late", "allow_resubmission"}
)


def now() -> datetime:
    return datetime.now(UTC)


def details(item: Assignment) -> dict[str, object]:
    return {
        "assignment_id": str(item.id),
        "academic_year_id": str(item.academic_year_id),
        "class_id": str(item.class_id),
        "section_id": str(item.section_id),
        "subject_id": str(item.subject_id),
        "status": item.status,
    }


class AssignmentBase:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.assignments = AssignmentRepository(session)
        self.submissions = SubmissionRepository(session)
        self.writer = ScopedWriter(session, context)

    def visible_assignment(
        self, scope: SchoolScope, assignment_id: uuid.UUID, *, lock: Lock = None
    ) -> Assignment:
        item = self.assignments.get_visible(
            assignment_visibility(scope.principal, scope.school_id), assignment_id, lock=lock
        )
        if item is None:
            raise NotFoundError("Assignment not found.")
        return item

    def require_manageable(self, scope: SchoolScope, item: Assignment) -> None:
        if not self.assignments.is_visible(
            assignment_manageability(scope.principal, scope.school_id), item.id
        ):
            raise PermissionDeniedError("You may not manage this assignment.")

    def school_zone(self, school_id: uuid.UUID) -> ZoneInfo:
        school = self.session.get(School, school_id)
        assert school is not None  # noqa: S101 - resolved by the school scope
        return ZoneInfo(school.timezone)

    def audit(
        self, action: AuditAction, scope: SchoolScope, item: Assignment, **extra: object
    ) -> None:
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="assignment",
            resource_id=item.id,
            school_id=scope.school_id,
            **{**details(item), **extra},
        )


class AssignmentService(AssignmentBase):
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        super().__init__(session, context)
        self.years = AcademicYearRepository(session)
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.subjects = SubjectRepository(session)

    # -------------------------------------------------------------------- reads
    def get(self, scope: SchoolScope, assignment_id: uuid.UUID) -> Assignment:
        return self.visible_assignment(scope, assignment_id)

    def list(
        self,
        scope: SchoolScope,
        *,
        academic_year_id: uuid.UUID | None,
        class_id: uuid.UUID | None,
        section_id: uuid.UUID | None,
        subject_id: uuid.UUID | None,
        status: AssignmentLifecycle | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Assignment], int]:
        # Filters only narrow the authorised set; they are ANDed with visibility.
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (A.academic_year_id, academic_year_id),
                (A.class_id, class_id),
                (A.section_id, section_id),
                (A.subject_id, subject_id),
                (A.status, status.value if status else None),
            )
            if value is not None
        ]
        return self.assignments.list_visible(
            assignment_visibility(scope.principal, scope.school_id),
            filters,
            offset=offset,
            limit=limit,
        )

    def students(
        self,
        scope: SchoolScope,
        assignment_id: uuid.UUID,
        *,
        outstanding_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[StudentRows, int]:
        """The roster with submission state: school staff and faculty teaching it only
        (never through a parent or student relationship)."""
        item = self.visible_assignment(scope, assignment_id)
        if not self.assignments.is_visible(
            assignment_staff_reach(scope.principal, scope.school_id), item.id
        ):
            raise PermissionDeniedError("The roster is available to school staff and faculty.")
        return self.assignments.students(
            item, outstanding_only=outstanding_only, offset=offset, limit=limit
        )

    # --------------------------------------------------------------- validation
    def _authorize_create(self, scope: SchoolScope, data: AssignmentCreate) -> None:
        if is_school_wide(scope.principal):
            return
        allowed = self.session.scalar(
            select(
                faculty_teaches(
                    scope.principal.user_id,
                    scope.school_id,
                    academic_year_id=data.academic_year_id,
                    class_id=data.class_id,
                    section_id=data.section_id,
                    subject_id=data.subject_id,
                    on_date=data.assigned_on,
                )
            )
        )
        if not (
            allowed
            and scope.principal.has_permission(PermissionName.FACULTY_READ_ASSIGNED_STUDENTS)
        ):
            raise PermissionDeniedError(
                "You are not assigned to this section and subject on that date."
            )

    def _placement(self, scope: SchoolScope, data: AssignmentCreate) -> AcademicYear:
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
        subject = self.subjects.get(scope.school_id, data.subject_id)
        if subject is None:
            raise body_error("subject_id", "Subject not found.")
        if not subject.is_active:
            raise body_error("subject_id", "Subject is inactive.")
        return year

    def _dates(
        self, scope: SchoolScope, year: AcademicYear, assigned_on: date, due_at: datetime
    ) -> None:
        if not year.start_date <= assigned_on <= year.end_date:
            raise body_error("assigned_on", "Must fall within the academic year.")
        due_local = due_at.astimezone(self.school_zone(scope.school_id)).date()
        if due_local < assigned_on:
            raise body_error("due_at", "Must not be before assigned_on (school time zone).")
        if due_local > year.end_date:
            raise body_error("due_at", "Must fall within the academic year (school time zone).")

    # ------------------------------------------------------------------- writes
    def create(self, scope: SchoolScope, data: AssignmentCreate) -> Assignment:
        self._authorize_create(scope, data)
        year = self._placement(scope, data)
        self._dates(scope, year, data.assigned_on, data.due_at)
        item = Assignment(
            school_id=scope.school_id, created_by_user_id=scope.actor_id, **data.model_dump()
        )
        self.writer.flush(ASSIGNMENT_CONSTRAINTS, before=lambda: self.session.add(item))
        self.audit(
            AuditAction.ASSIGNMENT_CREATED,
            scope,
            item,
            assigned_on=str(item.assigned_on),
            due_at=item.due_at.isoformat(),
            graded=item.max_marks is not None,
        )
        self.session.commit()
        return item

    def update(
        self, scope: SchoolScope, assignment_id: uuid.UUID, data: AssignmentUpdate
    ) -> Assignment:
        item = self.visible_assignment(scope, assignment_id, lock="update")
        self.require_manageable(scope, item)
        changes = data.changes()
        if item.status == AssignmentLifecycle.CLOSED:
            raise ConflictError("A closed assignment cannot be changed.")
        if item.status == AssignmentLifecycle.PUBLISHED and set(changes) - PUBLISHED_EDITABLE:
            raise ConflictError(
                "Once published, only title, instructions, due_at, allow_late and "
                "allow_resubmission can change."
            )
        assigned_on: date = changes.get("assigned_on", item.assigned_on)  # type: ignore[assignment]
        due_at: datetime = changes.get("due_at", item.due_at)  # type: ignore[assignment]
        if "assigned_on" in changes or "due_at" in changes:
            year = self.years.get(scope.school_id, item.academic_year_id)
            assert year is not None  # noqa: S101 - restricted foreign key
            self._dates(scope, year, assigned_on, due_at)
        diff: list[dict[str, object]] = []
        self.writer.flush(
            ASSIGNMENT_CONSTRAINTS,
            before=lambda: diff.extend(
                apply_changes(item, changes, redact=frozenset({"instructions"}))
            ),
        )
        if diff:
            self.audit(AuditAction.ASSIGNMENT_UPDATED, scope, item, changes=_jsonable(diff))
        self.session.commit()
        self.session.refresh(item)
        return item

    def publish(self, scope: SchoolScope, assignment_id: uuid.UUID) -> Assignment:
        item = self.visible_assignment(scope, assignment_id, lock="update")
        self.require_manageable(scope, item)
        if item.status != AssignmentLifecycle.DRAFT:
            raise ConflictError(f"Only a draft assignment can be published (it is {item.status}).")
        if item.due_at <= now():
            raise BusinessRuleError("The due time has already passed; move due_at first.")

        def apply() -> None:
            item.status = AssignmentLifecycle.PUBLISHED.value
            item.published_by_user_id = scope.actor_id
            item.published_at = now()

        self.writer.flush(ASSIGNMENT_CONSTRAINTS, before=apply)
        self.audit(AuditAction.ASSIGNMENT_PUBLISHED, scope, item, due_at=item.due_at.isoformat())
        self.session.commit()
        self.session.refresh(item)
        return item

    def close(self, scope: SchoolScope, assignment_id: uuid.UUID) -> Assignment:
        item = self.visible_assignment(scope, assignment_id, lock="update")
        self.require_manageable(scope, item)
        if item.status != AssignmentLifecycle.PUBLISHED:
            raise ConflictError(f"Only a published assignment can be closed (it is {item.status}).")

        def apply() -> None:
            item.status = AssignmentLifecycle.CLOSED.value
            item.closed_by_user_id = scope.actor_id
            item.closed_at = now()

        self.writer.flush(ASSIGNMENT_CONSTRAINTS, before=apply)
        self.audit(AuditAction.ASSIGNMENT_CLOSED, scope, item)
        self.session.commit()
        self.session.refresh(item)
        return item


def _jsonable(diff: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in entry.items()
        }
        for entry in diff
    ]
