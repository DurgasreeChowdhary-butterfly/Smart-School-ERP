"""Resource-level authorization for students, parents and enrollments.

One rule per resource, expressed as a SQL condition and applied to *every*
query (lists and single-record reads alike). A caller can therefore never reach
a record by guessing or swapping an ID: if the condition excludes it, the
answer is 404, exactly as for an unknown ID.

Student visibility follows :func:`app.security.authorization.resolve_student_scope`:

==================  =========================================================
Scope               Students visible (always within the selected school)
==================  =========================================================
ALL_SCHOOLS/SCHOOL  every student of the school (SUPER_ADMIN, SCHOOL_ADMIN,
                    MANAGEMENT via ``student.read``)
ASSIGNED            students with an *active* enrollment matching one of the
                    caller's effective faculty assignments (FACULTY, see
                    :mod:`app.security.faculty_scope`)
LINKED_CHILDREN     students with an *active* link to the caller's *active*
                    parent record (PARENT)
SELF                the student whose ``user_id`` is the caller (STUDENT)
NONE                nothing
==================  =========================================================

A caller holding several persona permissions (e.g. a FACULTY member who is also a
PARENT at the school) sees the union of those scopes.

Parent visibility: staff holding ``user.read`` see the school's parents; a PARENT
sees only their own record; everyone else sees none.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, and_, exists, false, or_, select, true
from sqlalchemy.orm import Session

from app.models.faculty import Faculty
from app.models.people import Parent, Student, StudentEnrollment, StudentParent
from app.security.authorization import StudentScope, resolve_student_scope
from app.security.faculty_scope import (
    assigned_enrollment_condition,
    assigned_student_condition,
    is_assigned_student,
    is_assignment_scoped,
)
from app.security.permissions import PermissionName
from app.security.principal import Principal


def _linked_children_of(user_id: uuid.UUID) -> ColumnElement[bool]:
    return exists().where(
        StudentParent.student_id == Student.id,
        StudentParent.is_active.is_(True),
        StudentParent.parent_id == Parent.id,
        Parent.user_id == user_id,
        Parent.is_active.is_(True),
    )


def student_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """SQL condition selecting the students ``principal`` may see in ``school_id``."""
    in_school = Student.school_id == school_id
    scope = resolve_student_scope(principal)
    if scope in (StudentScope.ALL_SCHOOLS, StudentScope.SCHOOL):
        return in_school
    persona: list[ColumnElement[bool]] = []
    if principal.has_permission(PermissionName.FACULTY_READ_ASSIGNED_STUDENTS):
        persona.append(assigned_student_condition(Student.id, principal.user_id, school_id))
    if principal.has_permission(PermissionName.PARENT_READ_CHILD):
        persona.append(_linked_children_of(principal.user_id))
    if principal.has_permission(PermissionName.STUDENT_READ_SELF):
        persona.append(Student.user_id == principal.user_id)
    if not persona:
        return false()  # NONE fails closed
    return and_(in_school, or_(*persona))


def personal_student_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Students reached through the caller's *own* relationships only: linked children
    (PARENT) and self (STUDENT). Never school-wide and never assignment-based.

    Used where faculty reach is narrower than "every record of an assigned student"
    (e.g. attendance, which faculty reach through their sessions instead).
    """
    persona: list[ColumnElement[bool]] = []
    if principal.has_permission(PermissionName.PARENT_READ_CHILD):
        persona.append(_linked_children_of(principal.user_id))
    if principal.has_permission(PermissionName.STUDENT_READ_SELF):
        persona.append(Student.user_id == principal.user_id)
    if not persona:
        return false()
    return and_(Student.school_id == school_id, or_(*persona))


def enrollment_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Enrollments are visible exactly when their student is.

    Exception: faculty see only the enrollment rows that match their assignments, not
    the rest of an assigned student's placement history.
    """
    if is_assignment_scoped(principal):
        return assigned_enrollment_condition(principal.user_id, school_id)
    return and_(
        StudentEnrollment.school_id == school_id,
        StudentEnrollment.student_id.in_(
            select(Student.id).where(student_visibility(principal, school_id))
        ),
    )


def parent_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    in_school = Parent.school_id == school_id
    if principal.is_super_admin or principal.has_permission(PermissionName.USER_READ):
        return in_school
    if principal.has_permission(PermissionName.PARENT_READ_SELF):
        return and_(in_school, Parent.user_id == principal.user_id)
    return false()


def link_visibility(principal: Principal) -> ColumnElement[bool]:
    """Which links of an already-visible student/parent the caller may see.

    Staff see all links (including deactivated history). A parent sees only their
    own links, never other parents of the same child. A student sees their active
    parent links. Faculty see none: reaching a student never exposes the parents'
    personal data.
    """
    if principal.is_super_admin or principal.has_permission(PermissionName.STUDENT_READ):
        return true()
    if principal.has_permission(PermissionName.PARENT_READ_CHILD):
        return and_(
            StudentParent.is_active.is_(True),
            StudentParent.parent_id.in_(
                select(Parent.id).where(Parent.user_id == principal.user_id)
            ),
        )
    if principal.has_permission(PermissionName.STUDENT_READ_SELF):
        return StudentParent.is_active.is_(True)
    return false()


def get_current_parent(session: Session, principal: Principal) -> Parent | None:
    """The caller's parent record (None if the caller is not a parent)."""
    return session.scalars(select(Parent).where(Parent.user_id == principal.user_id)).one_or_none()


def get_current_student(session: Session, principal: Principal) -> Student | None:
    """The caller's student record (None if the caller is not a student)."""
    return session.scalars(
        select(Student).where(Student.user_id == principal.user_id)
    ).one_or_none()


class DbStudentAccessPolicy:
    """Implements the Phase 2 :class:`StudentAccessPolicy` hook against the database.

    Used by :func:`app.security.authorization.ensure_student_access` (later phases:
    attendance, marks...).
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def is_linked_child(self, parent_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        StudentParent.student_id == student_id,
                        StudentParent.is_active.is_(True),
                        StudentParent.parent_id == Parent.id,
                        Parent.user_id == parent_user_id,
                        Parent.is_active.is_(True),
                    )
                )
            )
        )

    def is_assigned_student(self, faculty_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        faculty_school = self.session.scalar(
            select(Faculty.school_id).where(Faculty.user_id == faculty_user_id)
        )
        if faculty_school is None:
            return False
        return is_assigned_student(self.session, faculty_user_id, faculty_school, student_id)

    def is_self(self, student_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return bool(
            self.session.scalar(
                select(exists().where(Student.id == student_id, Student.user_id == student_user_id))
            )
        )
