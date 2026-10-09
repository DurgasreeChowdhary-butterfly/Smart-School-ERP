"""Attendance scope helpers fail closed for callers without a matching capability.

Routes already demand ``attendance.read``/``attendance.manage``; these tests pin the
defence-in-depth behaviour of the helpers themselves.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.sql.elements import False_

from app.security.attendance_scope import (
    can_create_session,
    record_visibility,
    session_manageability,
    session_visibility,
)
from app.security.permissions import ROLE_PERMISSIONS, PermissionName, RoleName
from app.security.principal import Principal

SCHOOL = uuid.uuid4()
P = PermissionName


def _principal(*permissions: PermissionName) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        school_id=SCHOOL,
        roles=frozenset(),
        permissions=frozenset(p.value for p in permissions),
    )


def _role(role: RoleName) -> Principal:
    return _principal(*ROLE_PERMISSIONS[role])


NOBODY = _principal()
# attendance.manage alone (no school-wide or faculty capability) reaches nothing.
MANAGE_ONLY = _principal(P.ATTENDANCE_MANAGE, P.ATTENDANCE_READ)


@pytest.mark.parametrize("principal", [NOBODY, MANAGE_ONLY])
def test_conditions_fail_closed(principal: Principal) -> None:
    assert isinstance(session_visibility(principal, SCHOOL), False_)
    assert isinstance(session_manageability(principal, SCHOOL), False_)
    assert isinstance(record_visibility(principal, SCHOOL), False_)


@pytest.mark.parametrize("role", [RoleName.MANAGEMENT, RoleName.PARENT, RoleName.STUDENT])
def test_readers_without_manage_never_manage(role: RoleName) -> None:
    principal = _role(role)
    assert isinstance(session_manageability(principal, SCHOOL), False_)
    allowed = can_create_session(
        None,  # type: ignore[arg-type]  # refused before any query
        principal,
        SCHOOL,
        academic_year_id=uuid.uuid4(),
        class_id=uuid.uuid4(),
        section_id=uuid.uuid4(),
        subject_id=uuid.uuid4(),
        session_date=date(2026, 7, 10),
    )
    assert allowed is False


def test_parents_and_students_never_see_sessions() -> None:
    for role in (RoleName.PARENT, RoleName.STUDENT):
        assert isinstance(session_visibility(_role(role), SCHOOL), False_)
        assert not isinstance(record_visibility(_role(role), SCHOOL), False_)
