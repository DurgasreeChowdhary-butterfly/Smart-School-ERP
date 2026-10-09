"""Data access for faculty and faculty assignments.

Callers pass an authorization condition from :mod:`app.security.faculty_scope`;
these repositories never return rows outside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models.faculty import Faculty, FacultyAssignment
from app.models.user import User


class FacultyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], faculty_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[Faculty, User] | None:
        stmt = (
            select(Faculty, User)
            .join(User, User.id == Faculty.user_id)
            .where(Faculty.id == faculty_id, visible)
        )
        if for_update:
            stmt = stmt.with_for_update(of=Faculty)
        row = self.session.execute(stmt).one_or_none()
        return (row[0], row[1]) if row else None

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[Faculty, User]], int]:
        conditions = [visible, *filters]
        rows = self.session.execute(
            select(Faculty, User)
            .join(User, User.id == Faculty.user_id)
            .where(*conditions)
            .order_by(Faculty.last_name, Faculty.first_name, Faculty.employee_id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(Faculty).where(*conditions))
        return [(row[0], row[1]) for row in rows], total or 0

    def in_school(
        self, school_id: uuid.UUID, faculty_id: uuid.UUID, *, for_update: bool = False
    ) -> Faculty | None:
        stmt = select(Faculty).where(Faculty.id == faculty_id, Faculty.school_id == school_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).one_or_none()

    def of_user(self, school_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Faculty, User] | None:
        row = self.session.execute(
            select(Faculty, User)
            .join(User, User.id == Faculty.user_id)
            .where(Faculty.user_id == user_id, Faculty.school_id == school_id)
        ).one_or_none()
        return (row[0], row[1]) if row else None


class FacultyAssignmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], assignment_id: uuid.UUID, *, for_update: bool = False
    ) -> FacultyAssignment | None:
        stmt = select(FacultyAssignment).where(FacultyAssignment.id == assignment_id, visible)
        if for_update:
            stmt = stmt.with_for_update(of=FacultyAssignment)
        return self.session.scalars(stmt).one_or_none()

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[FacultyAssignment], int]:
        conditions = [visible, *filters]
        items = self.session.scalars(
            select(FacultyAssignment)
            .where(*conditions)
            .order_by(FacultyAssignment.created_at.desc(), FacultyAssignment.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count()).select_from(FacultyAssignment).where(*conditions)
        )
        return items, total or 0
