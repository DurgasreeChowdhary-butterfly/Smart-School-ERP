"""Resource-level authorization framework.

RBAC (``require_permission``) answers "may this caller perform this *kind* of
action?". Resource policies answer "on *which* records?". Phase 2 provides the
scope model and the tenant (school) boundary; the relationship checks are
implemented against the database by
:class:`app.security.people_scope.DbStudentAccessPolicy`:

* PARENT  -> linked children only        (``student_parents``, Phase 4)
* FACULTY -> assigned classes/students   (``faculty_assignments``, Phase 5)
* STUDENT -> own record only             (``students.user_id``, Phase 4)

:class:`DenyAllStudentAccessPolicy` remains the fail-closed default.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Protocol

from app.core.errors import NotFoundError
from app.security.permissions import PermissionName
from app.security.principal import Principal


class StudentScope(StrEnum):
    """Breadth of student data a principal may reach, from widest to narrowest."""

    ALL_SCHOOLS = "all_schools"  # SUPER_ADMIN
    SCHOOL = "school"  # holds student.read within their school
    ASSIGNED = "assigned"  # faculty: students in assigned classes
    LINKED_CHILDREN = "linked_children"  # parent: linked children
    SELF = "self"  # student: own record
    NONE = "none"


def resolve_student_scope(principal: Principal) -> StudentScope:
    if principal.is_super_admin:
        return StudentScope.ALL_SCHOOLS
    if principal.has_permission(PermissionName.STUDENT_READ):
        return StudentScope.SCHOOL if principal.school_id else StudentScope.NONE
    if principal.has_permission(PermissionName.FACULTY_READ_ASSIGNED_STUDENTS):
        return StudentScope.ASSIGNED
    if principal.has_permission(PermissionName.PARENT_READ_CHILD):
        return StudentScope.LINKED_CHILDREN
    if principal.has_permission(PermissionName.STUDENT_READ_SELF):
        return StudentScope.SELF
    return StudentScope.NONE


def ensure_school_access(principal: Principal, resource_school_id: uuid.UUID | None) -> None:
    """Tenant boundary: non-super-admins only reach records of their own school.

    Raises 404 (not 403) so callers cannot probe for records in other schools.
    """
    if principal.is_super_admin:
        return
    if principal.school_id is None or resource_school_id != principal.school_id:
        raise NotFoundError()


class StudentAccessPolicy(Protocol):
    """Relationship checks implemented by the students/parents/faculty modules."""

    def is_linked_child(self, parent_user_id: uuid.UUID, student_id: uuid.UUID) -> bool: ...

    def is_assigned_student(self, faculty_user_id: uuid.UUID, student_id: uuid.UUID) -> bool: ...

    def is_self(self, student_user_id: uuid.UUID, student_id: uuid.UUID) -> bool: ...


class DenyAllStudentAccessPolicy:
    """Fail-closed default: grants no relationship-based access."""

    def is_linked_child(self, parent_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return False

    def is_assigned_student(self, faculty_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return False

    def is_self(self, student_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return False


def can_access_student(
    principal: Principal,
    *,
    student_id: uuid.UUID,
    student_school_id: uuid.UUID | None,
    policy: StudentAccessPolicy,
) -> bool:
    """Single entry point for "may this principal see this student?".

    School-wide scopes win; otherwise every persona relationship the caller holds is
    tried (a FACULTY member who is also a PARENT reaches assigned students *and* their
    own children).
    """
    scope = resolve_student_scope(principal)
    if scope is StudentScope.ALL_SCHOOLS:
        return True
    if scope is StudentScope.NONE:
        return False
    if principal.school_id is None or principal.school_id != student_school_id:
        return False
    if scope is StudentScope.SCHOOL:
        return True
    user_id = principal.user_id
    return (
        (
            principal.has_permission(PermissionName.FACULTY_READ_ASSIGNED_STUDENTS)
            and policy.is_assigned_student(user_id, student_id)
        )
        or (
            principal.has_permission(PermissionName.PARENT_READ_CHILD)
            and policy.is_linked_child(user_id, student_id)
        )
        or (
            principal.has_permission(PermissionName.STUDENT_READ_SELF)
            and policy.is_self(user_id, student_id)
        )
    )


def ensure_student_access(
    principal: Principal,
    *,
    student_id: uuid.UUID,
    student_school_id: uuid.UUID | None,
    policy: StudentAccessPolicy,
) -> None:
    """Raise 404 unless the principal may access the student (no existence leak)."""
    if not can_access_student(
        principal, student_id=student_id, student_school_id=student_school_id, policy=policy
    ):
        raise NotFoundError()
