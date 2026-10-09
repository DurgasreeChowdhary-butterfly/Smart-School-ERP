"""Direct tests of the people-scope helpers, including the fail-closed branches."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.people import Parent, Student
from app.security.people_scope import (
    get_current_parent,
    get_current_student,
    parent_visibility,
    student_visibility,
)
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from tests.conftest import SCHOOL_A
from tests.integration.conftest import STUDENT_PASSWORD, World

pytestmark = pytest.mark.db


def _principal(role: RoleName | None, user_id: uuid.UUID | None = None) -> Principal:
    permissions = frozenset(p.value for p in ROLE_PERMISSIONS[role]) if role else frozenset()
    return Principal(
        user_id=user_id or uuid.uuid4(),
        session_id=uuid.uuid4(),
        school_id=SCHOOL_A,
        roles=frozenset({role.value}) if role else frozenset(),
        permissions=permissions,
    )


@pytest.mark.parametrize("role", [RoleName.FACULTY, None])
def test_faculty_and_roleless_callers_see_no_students_or_parents(
    world: World, db_session: Session, role: RoleName | None
) -> None:
    world.student("S1")
    world.parent()
    principal = _principal(role)
    assert (
        db_session.scalars(select(Student).where(student_visibility(principal, SCHOOL_A))).all()
        == []
    )
    assert (
        db_session.scalars(select(Parent).where(parent_visibility(principal, SCHOOL_A))).all() == []
    )


def test_current_parent_and_student_helpers(world: World, db_session: Session) -> None:
    student = world.student("S1", password=STUDENT_PASSWORD).json()
    parent = world.parent(mobile="+919844400001").json()
    student_row = db_session.get(Student, uuid.UUID(student["id"]))
    parent_row = db_session.get(Parent, uuid.UUID(parent["id"]))
    assert student_row is not None
    assert parent_row is not None
    assert student_row.user_id is not None

    as_student = _principal(RoleName.STUDENT, student_row.user_id)
    as_parent = _principal(RoleName.PARENT, parent_row.user_id)
    assert get_current_student(db_session, as_student) == student_row
    assert get_current_parent(db_session, as_parent) == parent_row
    assert get_current_parent(db_session, as_student) is None
    assert get_current_student(db_session, as_parent) is None
