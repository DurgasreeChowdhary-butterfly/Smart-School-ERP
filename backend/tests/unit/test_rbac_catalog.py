"""The role-permission matrix encodes the product's least-privilege rules."""

from __future__ import annotations

import pytest

from app.security.permissions import (
    ASSIGNABLE_ROLES,
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    PermissionName,
    RoleName,
)

P = PermissionName
AUTH_BASE = {P.AUTH_LOGIN, P.AUTH_REFRESH, P.AUTH_LOGOUT, P.AUTH_ME}


def test_exactly_the_initial_roles_exist() -> None:
    assert {r.value for r in RoleName} == {
        "SUPER_ADMIN",
        "SCHOOL_ADMIN",
        "MANAGEMENT",
        "FACULTY",
        "PARENT",
        "STUDENT",
    }


def test_every_role_and_permission_is_described() -> None:
    assert set(ROLE_DESCRIPTIONS) == set(RoleName)
    assert set(PERMISSION_DESCRIPTIONS) == set(PermissionName)
    assert set(ROLE_PERMISSIONS) == set(RoleName)


@pytest.mark.parametrize("role", list(RoleName))
def test_every_role_can_authenticate(role: RoleName) -> None:
    assert ROLE_PERMISSIONS[role] >= AUTH_BASE


@pytest.mark.parametrize(
    "forbidden",
    [
        P.STUDENT_CREATE,
        P.STUDENT_UPDATE,
        P.STUDENT_DEACTIVATE,
        P.MARKS_MANAGE,
        P.MARKS_PUBLISH,
        P.ATTENDANCE_MANAGE,
        P.USER_CREATE,
        P.USER_UPDATE,
        P.USER_ASSIGN_ROLE,
        P.ROLE_MANAGE,
        P.SCHOOL_MANAGE,
        P.STUDENT_READ,
        P.FACULTY_READ_ASSIGNED_STUDENTS,
    ],
)
def test_parent_and_student_cannot_write_or_read_broadly(forbidden: PermissionName) -> None:
    assert forbidden not in ROLE_PERMISSIONS[RoleName.PARENT]
    assert forbidden not in ROLE_PERMISSIONS[RoleName.STUDENT]


@pytest.mark.parametrize(
    "forbidden",
    [
        P.USER_ASSIGN_ROLE,
        P.USER_CREATE,
        P.STUDENT_CREATE,
        P.MARKS_PUBLISH,
        P.SCHOOL_MANAGE,
        P.ROLE_MANAGE,
        P.PERMISSION_MANAGE,
        P.STUDENT_READ,  # faculty read only *assigned* students
        P.ANALYTICS_READ,
    ],
)
def test_faculty_restrictions(forbidden: PermissionName) -> None:
    assert forbidden not in ROLE_PERMISSIONS[RoleName.FACULTY]


def test_faculty_core_capabilities() -> None:
    assert {
        P.ATTENDANCE_MANAGE,
        P.MARKS_MANAGE,
        P.ASSIGNMENT_MANAGE,
        P.FACULTY_READ_ASSIGNED_STUDENTS,
    } <= ROLE_PERMISSIONS[RoleName.FACULTY]


def test_persona_scopes_are_exclusive() -> None:
    assert P.PARENT_READ_CHILD in ROLE_PERMISSIONS[RoleName.PARENT]
    assert P.STUDENT_READ_SELF in ROLE_PERMISSIONS[RoleName.STUDENT]
    assert P.PARENT_READ_CHILD not in ROLE_PERMISSIONS[RoleName.STUDENT]
    assert P.STUDENT_READ_SELF not in ROLE_PERMISSIONS[RoleName.PARENT]


def test_only_super_admin_manages_the_rbac_catalog() -> None:
    for role, granted in ROLE_PERMISSIONS.items():
        has_catalog_admin = bool({P.ROLE_MANAGE, P.PERMISSION_MANAGE} & granted)
        assert has_catalog_admin == (role is RoleName.SUPER_ADMIN)


def test_management_publishes_results_but_does_not_enter_marks() -> None:
    assert P.MARKS_PUBLISH in ROLE_PERMISSIONS[RoleName.MANAGEMENT]
    assert P.MARKS_MANAGE not in ROLE_PERMISSIONS[RoleName.MANAGEMENT]


def test_only_super_admin_can_assign_super_admin() -> None:
    for role, assignable in ASSIGNABLE_ROLES.items():
        assert (RoleName.SUPER_ADMIN in assignable) == (role is RoleName.SUPER_ADMIN)
    for role in (RoleName.MANAGEMENT, RoleName.FACULTY, RoleName.PARENT, RoleName.STUDENT):
        assert role not in ASSIGNABLE_ROLES
        assert P.USER_ASSIGN_ROLE not in ROLE_PERMISSIONS[role]
