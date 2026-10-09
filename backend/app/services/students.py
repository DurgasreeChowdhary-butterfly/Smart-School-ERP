"""Students and their parent/guardian links.

Every read and write goes through :func:`student_visibility`, so parents reach
only linked children, students only themselves, staff only their school.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, exists
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.config import Settings
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.people import (
    Parent,
    Student,
    StudentEnrollment,
    StudentParent,
    StudentStatus,
)
from app.models.user import User, UserStatus
from app.repositories.people import ParentRepository, StudentParentRepository, StudentRepository
from app.schemas.common import ErrorDetail
from app.schemas.people import (
    StudentCreate,
    StudentParentCreate,
    StudentParentUpdate,
    StudentUpdate,
)
from app.security.people_scope import link_visibility, student_visibility
from app.security.permissions import PermissionName, RoleName
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.users import create_user_account

# Personal data is audited by field name only.
PERSONAL_FIELDS = frozenset(
    {"date_of_birth", "email", "phone", "address", "city", "state", "postal_code", "country"}
)
STUDENT_CONSTRAINTS = {
    "uq_students_school_id_admission_number": (
        "admission_number",
        "A student with this admission number already exists.",
    ),
}
LINK_CONSTRAINTS = {
    "pk_student_parents": ("parent_id", "This parent is already linked to the student."),
    "uq_student_parents_one_primary": (
        "is_primary",
        "The student already has a primary contact.",
    ),
    "fk_student_parents_parent_same_school": ("parent_id", "Parent not found."),
}


def body_error(field: str, message: str) -> BusinessRuleError:
    return BusinessRuleError(message, details=[ErrorDetail(loc=["body", field], message=message)])


def sync_student_user(session: Session, student: Student) -> None:
    """Keep the login identity consistent with the student record.

    A non-active student's user is deactivated, which Phase 2 authentication
    enforces at sign-in, refresh and on every request.
    """
    if student.user_id is None:
        return
    user = session.get(User, student.user_id)
    if user is None:  # pragma: no cover - guaranteed by the foreign key
        return
    user.full_name = student.full_name
    user.status = (
        UserStatus.ACTIVE.value
        if student.status == StudentStatus.ACTIVE
        else UserStatus.DEACTIVATED.value
    )


class StudentService:
    def __init__(
        self, session: Session, settings: Settings, context: RequestContext | None = None
    ) -> None:
        self.session = session
        self.settings = settings
        self.students = StudentRepository(session)
        self.parents = ParentRepository(session)
        self.links = StudentParentRepository(session)
        self.writer = ScopedWriter(session, context)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _visible(scope: SchoolScope) -> ColumnElement[bool]:
        return student_visibility(scope.principal, scope.school_id)

    def get(
        self, scope: SchoolScope, student_id: uuid.UUID, *, for_update: bool = False
    ) -> Student:
        student = self.students.get_visible(self._visible(scope), student_id, for_update=for_update)
        if student is None:
            raise NotFoundError("Student not found.")
        return student

    # ----------------------------------------------------------------- students
    def create(self, scope: SchoolScope, data: StudentCreate) -> Student:
        student = Student(school_id=scope.school_id, **data.model_dump(exclude={"password"}))

        def insert() -> None:
            if data.password is not None:
                user = create_user_account(
                    self.session,
                    self.settings,
                    full_name=student.full_name,
                    roles=[RoleName.STUDENT],
                    password=data.password.get_secret_value(),
                    school_id=scope.school_id,
                    created_by_id=scope.actor_id,
                    source="api",
                    require_contact=False,
                    extra_policy_inputs=(data.admission_number,),
                )
                student.user_id = user.id
            self.session.add(student)

        self.writer.flush(STUDENT_CONSTRAINTS, before=insert)
        self.writer.audit(
            AuditAction.STUDENT_CREATED,
            actor_id=scope.actor_id,
            resource_type="student",
            resource_id=student.id,
            school_id=scope.school_id,
            admission_number=student.admission_number,
            has_login=student.user_id is not None,
        )
        self.session.commit()
        return student

    def list(
        self,
        scope: SchoolScope,
        *,
        status: StudentStatus | None,
        admission_number: str | None,
        academic_year_id: uuid.UUID | None,
        class_id: uuid.UUID | None,
        section_id: uuid.UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Student], int]:
        # Filters only narrow the authorised set; they are ANDed with visibility.
        filters: list[ColumnElement[bool]] = []
        if status is not None:
            filters.append(Student.status == status.value)
        if admission_number:
            filters.append(Student.admission_number == admission_number.strip().upper())
        placement = [
            column == value
            for column, value in (
                (StudentEnrollment.academic_year_id, academic_year_id),
                (StudentEnrollment.class_id, class_id),
                (StudentEnrollment.section_id, section_id),
            )
            if value is not None
        ]
        if placement:
            filters.append(exists().where(StudentEnrollment.student_id == Student.id, *placement))
        return self.students.list_visible(self._visible(scope), filters, offset=offset, limit=limit)

    def update(self, scope: SchoolScope, student_id: uuid.UUID, data: StudentUpdate) -> Student:
        changes = data.changes()
        if "status" in changes and not scope.principal.has_permission(
            PermissionName.STUDENT_DEACTIVATE
        ):
            raise PermissionDeniedError("Changing a student's status requires student.deactivate.")
        student = self.get(scope, student_id, for_update=True)
        previous_status = student.status
        new_status = changes.get("status")
        if isinstance(new_status, StudentStatus):
            changes["status"] = new_status.value
        diff = apply_changes(student, changes, redact=PERSONAL_FIELDS)
        if not diff:
            return student
        sync_student_user(self.session, student)
        self.writer.flush(STUDENT_CONSTRAINTS)
        action = AuditAction.STUDENT_UPDATED
        if previous_status != student.status:
            action = (
                AuditAction.STUDENT_REACTIVATED
                if student.status == StudentStatus.ACTIVE
                else AuditAction.STUDENT_DEACTIVATED
            )
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="student",
            resource_id=student.id,
            school_id=scope.school_id,
            changes=diff,
        )
        self.session.commit()
        return student

    # ------------------------------------------------------------ parent links
    def list_parents(
        self, scope: SchoolScope, student_id: uuid.UUID
    ) -> Sequence[tuple[StudentParent, Parent, User]]:
        self.get(scope, student_id)
        return self.links.links_of_student(student_id, link_visibility(scope.principal))

    def link_parent(
        self, scope: SchoolScope, student_id: uuid.UUID, data: StudentParentCreate
    ) -> tuple[StudentParent, Parent, User]:
        self.get(scope, student_id, for_update=True)
        parent = self.parents.in_school(scope.school_id, data.parent_id)
        if parent is None:
            # Same answer for unknown IDs and other schools' parents.
            raise body_error("parent_id", "Parent not found.")
        if not parent.is_active:
            raise body_error("parent_id", "Parent is inactive.")
        existing = self.links.get(student_id, data.parent_id)
        if existing is not None:
            hint = "" if existing.is_active else " It is inactive; reactivate it with PATCH."
            raise ConflictError(f"This parent is already linked to the student.{hint}")
        link = StudentParent(
            student_id=student_id,
            parent_id=parent.id,
            school_id=scope.school_id,
            relationship_type=data.relationship_type.value,
            is_primary=data.is_primary,
            is_guardian=data.is_guardian,
        )
        self.writer.flush(LINK_CONSTRAINTS, before=lambda: self.session.add(link))
        self.writer.audit(
            AuditAction.STUDENT_PARENT_CREATED,
            actor_id=scope.actor_id,
            resource_type="student",
            resource_id=student_id,
            school_id=scope.school_id,
            parent_id=str(parent.id),
            relationship_type=link.relationship_type,
            is_primary=link.is_primary,
        )
        self.session.commit()
        user = self.session.get(User, parent.user_id)
        assert user is not None  # noqa: S101 - guaranteed by the foreign key
        return link, parent, user

    def update_link(
        self,
        scope: SchoolScope,
        student_id: uuid.UUID,
        parent_id: uuid.UUID,
        data: StudentParentUpdate,
    ) -> tuple[StudentParent, Parent, User]:
        self.get(scope, student_id, for_update=True)
        link = self.links.get(student_id, parent_id, for_update=True)
        if link is None:
            raise NotFoundError("Relationship not found.")
        parent = self.session.get(Parent, parent_id)
        assert parent is not None  # noqa: S101 - guaranteed by the foreign key
        changes = data.changes()
        if changes.get("is_active") is True and not link.is_active and not parent.is_active:
            raise BusinessRuleError("Reactivate the parent before this relationship.")
        if "relationship_type" in changes:
            changes["relationship_type"] = str(changes["relationship_type"])
        was_active = link.is_active
        diff = apply_changes(link, changes)
        if diff:
            self.writer.flush(LINK_CONSTRAINTS)
            action = AuditAction.STUDENT_PARENT_UPDATED
            if was_active != link.is_active:
                action = (
                    AuditAction.STUDENT_PARENT_REACTIVATED
                    if link.is_active
                    else AuditAction.STUDENT_PARENT_DEACTIVATED
                )
            self.writer.audit(
                action,
                actor_id=scope.actor_id,
                resource_type="student",
                resource_id=student_id,
                school_id=scope.school_id,
                parent_id=str(parent_id),
                changes=diff,
            )
            self.session.commit()
        user = self.session.get(User, parent.user_id)
        assert user is not None  # noqa: S101
        return link, parent, user
