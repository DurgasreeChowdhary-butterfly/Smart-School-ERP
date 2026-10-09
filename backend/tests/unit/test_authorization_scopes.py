"""Resource-scope framework: tenant boundary, scope resolution, fail-closed defaults."""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import NotFoundError
from app.security.authorization import (
    DenyAllStudentAccessPolicy,
    StudentScope,
    can_access_student,
    ensure_school_access,
    ensure_student_access,
    resolve_student_scope,
)
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal

SCHOOL = uuid.uuid4()
OTHER_SCHOOL = uuid.uuid4()


def principal(role: RoleName, school_id: uuid.UUID | None = SCHOOL) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        school_id=None if role is RoleName.SUPER_ADMIN else school_id,
        roles=frozenset({role.value}),
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[role]),
    )


@pytest.mark.parametrize(
    ("role", "scope"),
    [
        (RoleName.SUPER_ADMIN, StudentScope.ALL_SCHOOLS),
        (RoleName.SCHOOL_ADMIN, StudentScope.SCHOOL),
        (RoleName.MANAGEMENT, StudentScope.SCHOOL),
        (RoleName.FACULTY, StudentScope.ASSIGNED),
        (RoleName.PARENT, StudentScope.LINKED_CHILDREN),
        (RoleName.STUDENT, StudentScope.SELF),
    ],
)
def test_scope_resolution(role: RoleName, scope: StudentScope) -> None:
    assert resolve_student_scope(principal(role)) is scope


def test_school_wide_permission_without_school_has_no_scope() -> None:
    assert (
        resolve_student_scope(principal(RoleName.SCHOOL_ADMIN, school_id=None)) is StudentScope.NONE
    )


def test_no_permissions_means_no_scope() -> None:
    nobody = Principal(uuid.uuid4(), uuid.uuid4(), SCHOOL, frozenset(), frozenset())
    assert resolve_student_scope(nobody) is StudentScope.NONE
    assert not can_access_student(
        nobody,
        student_id=uuid.uuid4(),
        student_school_id=SCHOOL,
        policy=DenyAllStudentAccessPolicy(),
    )


def test_school_boundary() -> None:
    ensure_school_access(principal(RoleName.SCHOOL_ADMIN), SCHOOL)
    ensure_school_access(principal(RoleName.SUPER_ADMIN), OTHER_SCHOOL)
    with pytest.raises(NotFoundError):
        ensure_school_access(principal(RoleName.SCHOOL_ADMIN), OTHER_SCHOOL)
    with pytest.raises(NotFoundError):
        ensure_school_access(principal(RoleName.SCHOOL_ADMIN), None)


@pytest.mark.parametrize("role", [RoleName.FACULTY, RoleName.PARENT, RoleName.STUDENT])
def test_relationship_scopes_fail_closed_until_phase_4(role: RoleName) -> None:
    with pytest.raises(NotFoundError):
        ensure_student_access(
            principal(role),
            student_id=uuid.uuid4(),
            student_school_id=SCHOOL,
            policy=DenyAllStudentAccessPolicy(),
        )


class _LinkedPolicy(DenyAllStudentAccessPolicy):
    def __init__(self, allowed: uuid.UUID) -> None:
        self.allowed = allowed

    def is_linked_child(self, parent_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return student_id == self.allowed


def test_parent_reaches_only_linked_child_in_same_school() -> None:
    parent, child = principal(RoleName.PARENT), uuid.uuid4()
    policy = _LinkedPolicy(child)
    assert can_access_student(parent, student_id=child, student_school_id=SCHOOL, policy=policy)
    # Changing the student id in the URL does not grant access to another student.
    assert not can_access_student(
        parent, student_id=uuid.uuid4(), student_school_id=SCHOOL, policy=policy
    )
    assert not can_access_student(
        parent, student_id=child, student_school_id=OTHER_SCHOOL, policy=policy
    )


def test_admin_scope_is_school_wide_but_tenant_bound() -> None:
    admin = principal(RoleName.SCHOOL_ADMIN)
    policy = DenyAllStudentAccessPolicy()
    assert can_access_student(
        admin, student_id=uuid.uuid4(), student_school_id=SCHOOL, policy=policy
    )
    assert not can_access_student(
        admin, student_id=uuid.uuid4(), student_school_id=OTHER_SCHOOL, policy=policy
    )
    assert can_access_student(
        principal(RoleName.SUPER_ADMIN),
        student_id=uuid.uuid4(),
        student_school_id=OTHER_SCHOOL,
        policy=policy,
    )


class _RelationshipPolicy(DenyAllStudentAccessPolicy):
    def __init__(self, *, assigned: uuid.UUID, child: uuid.UUID) -> None:
        self.assigned = assigned
        self.child = child

    def is_assigned_student(self, faculty_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return student_id == self.assigned

    def is_linked_child(self, parent_user_id: uuid.UUID, student_id: uuid.UUID) -> bool:
        return student_id == self.child


def test_faculty_reaches_only_assigned_students() -> None:
    assigned, child = uuid.uuid4(), uuid.uuid4()
    policy = _RelationshipPolicy(assigned=assigned, child=child)
    faculty = principal(RoleName.FACULTY)
    assert can_access_student(faculty, student_id=assigned, student_school_id=SCHOOL, policy=policy)
    # A relationship another persona would grant does not leak to faculty.
    assert not can_access_student(
        faculty, student_id=child, student_school_id=SCHOOL, policy=policy
    )
    assert not can_access_student(
        faculty, student_id=assigned, student_school_id=OTHER_SCHOOL, policy=policy
    )


def test_combined_personas_get_the_union_of_their_scopes() -> None:
    assigned, child, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    policy = _RelationshipPolicy(assigned=assigned, child=child)
    both = Principal(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        school_id=SCHOOL,
        roles=frozenset({RoleName.FACULTY.value, RoleName.PARENT.value}),
        permissions=frozenset(
            p.value for r in (RoleName.FACULTY, RoleName.PARENT) for p in ROLE_PERMISSIONS[r]
        ),
    )
    for student in (assigned, child):
        assert can_access_student(both, student_id=student, student_school_id=SCHOOL, policy=policy)
    assert not can_access_student(
        both, student_id=stranger, student_school_id=SCHOOL, policy=policy
    )
