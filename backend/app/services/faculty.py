"""Faculty records and faculty assignments.

Faculty
-------
A faculty member is a domain record bound to a FACULTY user (the authentication
identity). Email and mobile live on ``users`` only; this service reads and writes
them there. Creating faculty creates the user, its FACULTY role and the faculty
row inside **one SAVEPOINT**, so a failure at any step (duplicate employee ID,
duplicate email, password policy...) leaves neither an orphaned account nor an
orphaned faculty record. Deactivating faculty deactivates the user, which Phase 2
authentication enforces at sign-in, refresh and on every request.

Assignments
-----------
* The faculty member, year, class, section and subject must belong to the school
  and be active; the section must belong to the class (also enforced by composite
  foreign keys); effective dates must fall within the academic year.
* Placement columns are immutable. Ending an assignment sets ``status`` to
  ``inactive``; rows are never deleted, so history is kept.
* At most one *active* assignment per (faculty, year, class, section, subject) -
  a partial unique index, so concurrent duplicates cannot both succeed.

Concurrency: assignment writes lock the faculty row first (``SELECT ... FOR
UPDATE``), as do faculty updates, so a faculty member's assignments and status
change one transaction at a time; the unique index is the final authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.config import Settings
from app.core.errors import NotFoundError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear
from app.models.faculty import AssignmentStatus, Faculty, FacultyAssignment, FacultyStatus
from app.models.user import User, UserStatus
from app.repositories.academic import (
    AcademicYearRepository,
    ClassRepository,
    SectionRepository,
    SubjectRepository,
)
from app.repositories.faculty import FacultyAssignmentRepository, FacultyRepository
from app.repositories.users import UserRepository
from app.schemas.faculty import (
    FacultyAssignmentCreate,
    FacultyAssignmentUpdate,
    FacultyCreate,
    FacultyUpdate,
)
from app.security.faculty_scope import (
    assignment_visibility,
    effective_assignments,
    faculty_visibility,
)
from app.security.permissions import RoleName
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.students import body_error
from app.services.users import create_user_account

FACULTY_CONSTRAINTS = {
    "uq_faculty_school_id_employee_id": (
        "employee_id",
        "A faculty member with this employee ID already exists.",
    ),
    "uq_faculty_user_id": ("user_id", "This user already has a faculty record."),
    "fk_faculty_user_same_school": ("user_id", "User not found."),
    "uq_users_phone": ("mobile", "A user with this mobile number already exists."),
    "uq_users_email": ("email", "A user with this email address already exists."),
}
ASSIGNMENT_CONSTRAINTS = {
    "uq_faculty_assignments_one_active": (
        "subject_id",
        "The faculty member already has an active assignment for this year, class, "
        "section and subject.",
    ),
    "fk_faculty_assignments_section_in_class": (
        "section_id",
        "Section does not belong to the class.",
    ),
}
# Contact details are audited by field name only.
PERSONAL_FIELDS = frozenset({"phone", "email"})
_USER_FIELDS = {"mobile": "phone", "email": "email"}


def _status_action(
    before: str,
    after: str,
    updated: AuditAction,
    deactivated: AuditAction,
    reactivated: AuditAction,
) -> AuditAction:
    if before == after:
        return updated
    return reactivated if after == "active" else deactivated


# =========================================================================== faculty
class FacultyService:
    def __init__(
        self, session: Session, settings: Settings, context: RequestContext | None = None
    ) -> None:
        self.session = session
        self.settings = settings
        self.faculty = FacultyRepository(session)
        self.assignments = FacultyAssignmentRepository(session)
        self.users = UserRepository(session)
        self.writer = ScopedWriter(session, context)

    @staticmethod
    def _visible(scope: SchoolScope) -> ColumnElement[bool]:
        return faculty_visibility(scope.principal, scope.school_id)

    def get(
        self, scope: SchoolScope, faculty_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[Faculty, User]:
        row = self.faculty.get_visible(self._visible(scope), faculty_id, for_update=for_update)
        if row is None:
            raise NotFoundError("Faculty member not found.")
        return row

    def list(
        self,
        scope: SchoolScope,
        *,
        status: FacultyStatus | None,
        employee_id: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[Faculty, User]], int]:
        filters: list[ColumnElement[bool]] = []
        if status is not None:
            filters.append(Faculty.status == status.value)
        if employee_id:
            filters.append(Faculty.employee_id == employee_id.strip().upper())
        return self.faculty.list_visible(self._visible(scope), filters, offset=offset, limit=limit)

    # -------------------------------------------------------------- self-service
    def me(self, scope: SchoolScope) -> tuple[Faculty, User]:
        row = self.faculty.of_user(scope.school_id, scope.actor_id)
        if row is None:
            raise NotFoundError("Faculty profile not found.")
        return row

    def my_assignments(
        self, scope: SchoolScope, *, offset: int, limit: int
    ) -> tuple[Sequence[FacultyAssignment], int]:
        """The caller's *effective* assignments: exactly what grants them access today."""
        faculty, _ = self.me(scope)
        return self.assignments.list_visible(
            FacultyAssignment.faculty_id == faculty.id,
            [
                FacultyAssignment.id.in_(
                    _effective_ids(scope.actor_id, scope.school_id),
                )
            ],
            offset=offset,
            limit=limit,
        )

    # ------------------------------------------------------------------- writes
    def _attachable_user(self, scope: SchoolScope, user_id: uuid.UUID) -> User:
        user = self.users.get(user_id)
        # Same answer for unknown ids and other schools' users.
        if user is None or user.school_id != scope.school_id:
            raise body_error("user_id", "User not found.")
        if RoleName.FACULTY.value not in self.users.role_names(user.id):
            raise body_error("user_id", "The user does not have the FACULTY role.")
        if user.status != UserStatus.ACTIVE:
            raise body_error("user_id", "The user is inactive.")
        return user

    def create(self, scope: SchoolScope, data: FacultyCreate) -> tuple[Faculty, User]:
        fields = data.model_dump(exclude={"email", "mobile", "password", "user_id"})
        created: list[tuple[Faculty, User]] = []

        def insert() -> None:
            faculty = Faculty(school_id=scope.school_id, **fields)
            if data.user_id is not None:
                user = self._attachable_user(scope, data.user_id)
                user.full_name = faculty.full_name
            else:
                assert data.password is not None  # noqa: S101 - guaranteed by the schema
                user = create_user_account(
                    self.session,
                    self.settings,
                    full_name=faculty.full_name,
                    email=data.email,
                    phone=data.mobile,
                    password=data.password.get_secret_value(),
                    roles=[RoleName.FACULTY],
                    school_id=scope.school_id,
                    created_by_id=scope.actor_id,
                    source="api",
                    extra_policy_inputs=(data.employee_id,),
                )
            faculty.user_id = user.id
            self.session.add(faculty)
            created.append((faculty, user))

        # User, role and faculty row commit together or not at all.
        self.writer.flush(FACULTY_CONSTRAINTS, before=insert)
        faculty, user = created[0]
        self.writer.audit(
            AuditAction.FACULTY_CREATED,
            actor_id=scope.actor_id,
            resource_type="faculty",
            resource_id=faculty.id,
            school_id=scope.school_id,
            user_id=str(user.id),
            employee_id=faculty.employee_id,
            attached_existing_user=data.user_id is not None,
        )
        self.session.commit()
        return faculty, user

    def update(
        self, scope: SchoolScope, faculty_id: uuid.UUID, data: FacultyUpdate
    ) -> tuple[Faculty, User]:
        changes = data.changes()
        faculty, user = self.get(scope, faculty_id, for_update=True)
        previous_status = faculty.status
        if isinstance(changes.get("status"), FacultyStatus):
            changes["status"] = str(changes["status"])
        user_changes = {
            _USER_FIELDS[key]: changes.pop(key) for key in list(changes) if key in _USER_FIELDS
        }
        email = user_changes.get("email", user.email)
        phone = user_changes.get("phone", user.phone)
        if email is None and phone is None:
            raise body_error("email", "An email address or mobile number is required to sign in.")
        diff = apply_changes(faculty, changes)
        diff += apply_changes(user, user_changes, redact=PERSONAL_FIELDS)
        if not diff:
            return faculty, user
        user.full_name = faculty.full_name
        user.status = (
            UserStatus.ACTIVE.value
            if faculty.status == FacultyStatus.ACTIVE
            else UserStatus.DEACTIVATED.value
        )
        self.writer.flush(FACULTY_CONSTRAINTS)
        self.writer.audit(
            _status_action(
                previous_status,
                faculty.status,
                AuditAction.FACULTY_UPDATED,
                AuditAction.FACULTY_DEACTIVATED,
                AuditAction.FACULTY_REACTIVATED,
            ),
            actor_id=scope.actor_id,
            resource_type="faculty",
            resource_id=faculty.id,
            school_id=scope.school_id,
            changes=diff,
        )
        self.session.commit()
        return faculty, user


def _effective_ids(user_id: uuid.UUID, school_id: uuid.UUID) -> Select[uuid.UUID]:
    return (
        select(FacultyAssignment.id)
        .select_from(FacultyAssignment, Faculty)
        .where(*effective_assignments(user_id, school_id))
    )


# ======================================================================= assignments
class FacultyAssignmentService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.assignments = FacultyAssignmentRepository(session)
        self.faculty = FacultyRepository(session)
        self.years = AcademicYearRepository(session)
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.subjects = SubjectRepository(session)
        self.writer = ScopedWriter(session, context)

    @staticmethod
    def _visible(scope: SchoolScope) -> ColumnElement[bool]:
        return assignment_visibility(scope.principal, scope.school_id)

    def get(
        self, scope: SchoolScope, assignment_id: uuid.UUID, *, for_update: bool = False
    ) -> FacultyAssignment:
        item = self.assignments.get_visible(
            self._visible(scope), assignment_id, for_update=for_update
        )
        if item is None:
            raise NotFoundError("Faculty assignment not found.")
        return item

    def list(
        self,
        scope: SchoolScope,
        *,
        faculty_id: uuid.UUID | None,
        academic_year_id: uuid.UUID | None,
        class_id: uuid.UUID | None,
        section_id: uuid.UUID | None,
        subject_id: uuid.UUID | None,
        status: AssignmentStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[FacultyAssignment], int]:
        # Filters only narrow the authorised set; they are ANDed with visibility.
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (FacultyAssignment.faculty_id, faculty_id),
                (FacultyAssignment.academic_year_id, academic_year_id),
                (FacultyAssignment.class_id, class_id),
                (FacultyAssignment.section_id, section_id),
                (FacultyAssignment.subject_id, subject_id),
                (FacultyAssignment.status, status.value if status else None),
            )
            if value is not None
        ]
        return self.assignments.list_visible(
            self._visible(scope), filters, offset=offset, limit=limit
        )

    # --------------------------------------------------------------- validation
    def _lock_faculty(self, scope: SchoolScope, faculty_id: uuid.UUID) -> Faculty:
        faculty = self.faculty.in_school(scope.school_id, faculty_id, for_update=True)
        if faculty is None:
            # Same answer for unknown ids and other schools' faculty.
            raise body_error("faculty_id", "Faculty member not found.")
        return faculty

    def _validate_placement(
        self,
        scope: SchoolScope,
        *,
        academic_year_id: uuid.UUID,
        class_id: uuid.UUID,
        section_id: uuid.UUID,
        subject_id: uuid.UUID,
    ) -> AcademicYear:
        """Every referenced record must exist in this school and be active."""
        year = self.years.get(scope.school_id, academic_year_id)
        if year is None:
            raise body_error("academic_year_id", "Academic year not found.")
        if not year.is_active:
            raise body_error("academic_year_id", "Academic year is inactive.")
        school_class = self.classes.get(scope.school_id, class_id)
        if school_class is None:
            raise body_error("class_id", "Class not found.")
        if not school_class.is_active:
            raise body_error("class_id", "Class is inactive.")
        section = self.sections.get(scope.school_id, section_id)
        if section is None:
            raise body_error("section_id", "Section not found.")
        if section.class_id != school_class.id:
            raise body_error("section_id", "Section does not belong to the class.")
        if not section.is_active:
            raise body_error("section_id", "Section is inactive.")
        subject = self.subjects.get(scope.school_id, subject_id)
        if subject is None:
            raise body_error("subject_id", "Subject not found.")
        if not subject.is_active:
            raise body_error("subject_id", "Subject is inactive.")
        return year

    @staticmethod
    def _validate_dates(year: AcademicYear, start: date | None, end: date | None) -> None:
        for field, value in (("effective_from", start), ("effective_to", end)):
            if value is not None and not year.start_date <= value <= year.end_date:
                raise body_error(field, "Must fall within the academic year.")
        if start is not None and end is not None and start >= end:
            raise body_error("effective_to", "Must be after effective_from.")

    @staticmethod
    def _scope_details(item: FacultyAssignment) -> dict[str, object]:
        return {
            "faculty_id": str(item.faculty_id),
            "academic_year_id": str(item.academic_year_id),
            "class_id": str(item.class_id),
            "section_id": str(item.section_id),
            "subject_id": str(item.subject_id),
        }

    # ------------------------------------------------------------------- writes
    def create(self, scope: SchoolScope, data: FacultyAssignmentCreate) -> FacultyAssignment:
        faculty = self._lock_faculty(scope, data.faculty_id)
        if faculty.status != FacultyStatus.ACTIVE:
            raise body_error("faculty_id", "Faculty member is inactive.")
        year = self._validate_placement(
            scope,
            academic_year_id=data.academic_year_id,
            class_id=data.class_id,
            section_id=data.section_id,
            subject_id=data.subject_id,
        )
        self._validate_dates(year, data.effective_from, data.effective_to)
        item = FacultyAssignment(school_id=scope.school_id, **data.model_dump())
        self.writer.flush(ASSIGNMENT_CONSTRAINTS, before=lambda: self.session.add(item))
        self.writer.audit(
            AuditAction.FACULTY_ASSIGNMENT_CREATED,
            actor_id=scope.actor_id,
            resource_type="faculty_assignment",
            resource_id=item.id,
            school_id=scope.school_id,
            effective_from=str(item.effective_from) if item.effective_from else None,
            effective_to=str(item.effective_to) if item.effective_to else None,
            **self._scope_details(item),
        )
        self.session.commit()
        return item

    def update(
        self, scope: SchoolScope, assignment_id: uuid.UUID, data: FacultyAssignmentUpdate
    ) -> FacultyAssignment:
        found = self.get(scope, assignment_id)
        # Lock order: faculty row first, then the assignment (same as create).
        faculty = self._lock_faculty(scope, found.faculty_id)
        item = self.get(scope, assignment_id, for_update=True)
        changes = data.changes()
        if isinstance(changes.get("status"), AssignmentStatus):
            changes["status"] = str(changes["status"])
        previous_status = item.status
        reactivating = (
            changes.get("status") == AssignmentStatus.ACTIVE
            and item.status != AssignmentStatus.ACTIVE
        )
        if reactivating:
            if faculty.status != FacultyStatus.ACTIVE:
                raise body_error("status", "Reactivate the faculty member first.")
            self._validate_placement(
                scope,
                academic_year_id=item.academic_year_id,
                class_id=item.class_id,
                section_id=item.section_id,
                subject_id=item.subject_id,
            )
        year = self.years.get(scope.school_id, item.academic_year_id)
        assert year is not None  # noqa: S101 - guaranteed by the composite foreign key
        start = changes.get("effective_from", item.effective_from)
        end = changes.get("effective_to", item.effective_to)
        self._validate_dates(year, start, end)  # type: ignore[arg-type]

        diff: list[dict[str, object]] = []
        self.writer.flush(
            ASSIGNMENT_CONSTRAINTS, before=lambda: diff.extend(apply_changes(item, changes))
        )
        if diff:
            self.writer.audit(
                _status_action(
                    previous_status,
                    item.status,
                    AuditAction.FACULTY_ASSIGNMENT_UPDATED,
                    AuditAction.FACULTY_ASSIGNMENT_DEACTIVATED,
                    AuditAction.FACULTY_ASSIGNMENT_REACTIVATED,
                ),
                actor_id=scope.actor_id,
                resource_type="faculty_assignment",
                resource_id=item.id,
                school_id=scope.school_id,
                changes=diff,
                **self._scope_details(item),
            )
        self.session.commit()
        self.session.refresh(item)
        return item
