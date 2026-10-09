"""Data access for students, parents, links and enrollments.

Callers pass an authorization condition from :mod:`app.security.people_scope`;
these repositories never return rows outside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import Session

from app.models.people import Parent, Student, StudentEnrollment, StudentParent
from app.models.user import User


class StudentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], student_id: uuid.UUID, *, for_update: bool = False
    ) -> Student | None:
        stmt = select(Student).where(Student.id == student_id, visible)
        if for_update:
            stmt = stmt.with_for_update(of=Student)
        return self.session.scalars(stmt).one_or_none()

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Student], int]:
        conditions = [visible, *filters]
        items = self.session.scalars(
            select(Student)
            .where(*conditions)
            .order_by(Student.last_name, Student.first_name, Student.admission_number)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(Student).where(*conditions))
        return items, total or 0


class ParentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], parent_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[Parent, User] | None:
        stmt = (
            select(Parent, User)
            .join(User, User.id == Parent.user_id)
            .where(Parent.id == parent_id, visible)
        )
        if for_update:
            stmt = stmt.with_for_update(of=Parent)
        row = self.session.execute(stmt).one_or_none()
        return (row[0], row[1]) if row else None

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[Parent, User]], int]:
        conditions = [visible, *filters]
        rows = self.session.execute(
            select(Parent, User)
            .join(User, User.id == Parent.user_id)
            .where(*conditions)
            .order_by(Parent.last_name, Parent.first_name)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count())
            .select_from(Parent)
            .join(User, User.id == Parent.user_id)
            .where(*conditions)
        )
        return [(row[0], row[1]) for row in rows], total or 0

    def in_school(self, school_id: uuid.UUID, parent_id: uuid.UUID) -> Parent | None:
        return self.session.scalars(
            select(Parent).where(Parent.id == parent_id, Parent.school_id == school_id)
        ).one_or_none()


class StudentParentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self, student_id: uuid.UUID, parent_id: uuid.UUID, *, for_update: bool = False
    ) -> StudentParent | None:
        return self.session.get(StudentParent, (student_id, parent_id), with_for_update=for_update)

    def links_of_student(
        self, student_id: uuid.UUID, visible: ColumnElement[bool]
    ) -> list[tuple[StudentParent, Parent, User]]:
        rows = self.session.execute(
            select(StudentParent, Parent, User)
            .join(Parent, Parent.id == StudentParent.parent_id)
            .join(User, User.id == Parent.user_id)
            .where(StudentParent.student_id == student_id, visible)
            .order_by(StudentParent.is_primary.desc(), Parent.last_name, Parent.first_name)
        ).all()
        return [(r[0], r[1], r[2]) for r in rows]

    def links_of_parent(
        self, parent_id: uuid.UUID, visible: ColumnElement[bool]
    ) -> list[tuple[StudentParent, Student]]:
        rows = self.session.execute(
            select(StudentParent, Student)
            .join(Student, Student.id == StudentParent.student_id)
            .where(StudentParent.parent_id == parent_id, visible)
            .order_by(Student.last_name, Student.first_name)
        ).all()
        return [(r[0], r[1]) for r in rows]


class EnrollmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], enrollment_id: uuid.UUID, *, for_update: bool = False
    ) -> StudentEnrollment | None:
        stmt = select(StudentEnrollment).where(StudentEnrollment.id == enrollment_id, visible)
        if for_update:
            stmt = stmt.with_for_update(of=StudentEnrollment)
        return self.session.scalars(stmt).one_or_none()

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[StudentEnrollment], int]:
        conditions = [visible, *filters]
        items = self.session.scalars(
            select(StudentEnrollment)
            .where(*conditions)
            .order_by(StudentEnrollment.enrollment_date.desc(), StudentEnrollment.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count()).select_from(StudentEnrollment).where(*conditions)
        )
        return items, total or 0

    def active_in_year(
        self, student_id: uuid.UUID, academic_year_id: uuid.UUID
    ) -> StudentEnrollment | None:
        return self.session.scalars(
            select(StudentEnrollment)
            .where(
                StudentEnrollment.student_id == student_id,
                StudentEnrollment.academic_year_id == academic_year_id,
                StudentEnrollment.status == "active",
            )
            .with_for_update()
        ).one_or_none()

    def clear_current(self, student_id: uuid.UUID, *, except_id: uuid.UUID | None = None) -> None:
        stmt = update(StudentEnrollment).where(
            StudentEnrollment.student_id == student_id, StudentEnrollment.is_current.is_(True)
        )
        if except_id is not None:
            stmt = stmt.where(StudentEnrollment.id != except_id)
        self.session.execute(stmt.values(is_current=False, updated_at=func.now()))

    def current_id(self, student_id: uuid.UUID) -> uuid.UUID | None:
        return self.session.scalar(
            select(StudentEnrollment.id).where(
                StudentEnrollment.student_id == student_id, StudentEnrollment.is_current.is_(True)
            )
        )
