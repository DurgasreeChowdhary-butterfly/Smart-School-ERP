"""Shared permission sets and response builders for people endpoints."""

from __future__ import annotations

from app.models.people import Parent, Student, StudentParent
from app.models.user import User
from app.schemas.people import (
    ParentRead,
    ParentSummary,
    StudentParentRead,
    StudentRead,
    StudentSummary,
)
from app.security.authorization import StudentScope, resolve_student_scope
from app.security.permissions import PermissionName
from app.security.principal import Principal

P = PermissionName

# Each role reaches student data through its own capability; the service then
# narrows results to that capability's scope (school / assigned / linked children / self).
STUDENT_READERS = (
    P.STUDENT_READ,
    P.FACULTY_READ_ASSIGNED_STUDENTS,
    P.STUDENT_READ_SELF,
    P.PARENT_READ_CHILD,
)
# Staff read the school's parents; a parent reads only themselves.
PARENT_READERS = (P.USER_READ, P.PARENT_READ_SELF)


def parent_read(parent: Parent, user: User) -> ParentRead:
    return ParentRead(
        id=parent.id,
        school_id=parent.school_id,
        first_name=parent.first_name,
        last_name=parent.last_name,
        mobile=user.phone,
        email=user.email,
        occupation=parent.occupation,
        address=parent.address,
        city=parent.city,
        state=parent.state,
        postal_code=parent.postal_code,
        country=parent.country,
        is_active=parent.is_active,
        created_at=parent.created_at,
        updated_at=parent.updated_at,
    )


def _link_fields(link: StudentParent) -> dict[str, object]:
    return {
        "student_id": link.student_id,
        "parent_id": link.parent_id,
        "relationship_type": link.relationship_type,
        "is_primary": link.is_primary,
        "is_guardian": link.is_guardian,
        "is_active": link.is_active,
        "created_at": link.created_at,
        "updated_at": link.updated_at,
    }


def link_with_parent(link: StudentParent, parent: Parent, user: User) -> StudentParentRead:
    return StudentParentRead.model_validate(
        {
            **_link_fields(link),
            "parent": ParentSummary(
                id=parent.id,
                first_name=parent.first_name,
                last_name=parent.last_name,
                mobile=user.phone,
                is_active=parent.is_active,
            ),
        }
    )


def link_with_student(link: StudentParent, student: Student) -> StudentParentRead:
    return StudentParentRead.model_validate(
        {
            **_link_fields(link),
            "student": StudentSummary(
                id=student.id,
                admission_number=student.admission_number,
                first_name=student.first_name,
                last_name=student.last_name,
                status=student.status,
            ),
        }
    )


# Faculty reach students for teaching, not contact: home address and contact details
# are withheld from assignment-scoped callers.
FACULTY_HIDDEN_STUDENT_FIELDS = (
    "email",
    "phone",
    "address",
    "city",
    "state",
    "postal_code",
    "country",
)


def student_read(student: Student, principal: Principal) -> StudentRead:
    read = StudentRead.model_validate(student)
    if resolve_student_scope(principal) is StudentScope.ASSIGNED:
        return read.model_copy(update=dict.fromkeys(FACULTY_HIDDEN_STUDENT_FIELDS))
    return read
