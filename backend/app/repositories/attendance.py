"""Data access for attendance.

Callers pass an authorization condition from :mod:`app.security.attendance_scope`;
these repositories never return rows outside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.attendance import (
    AttendanceRecord,
    AttendanceRecordChange,
    AttendanceSession,
    AttendanceStatus,
)
from app.models.people import EnrollmentStatus, Student, StudentEnrollment

S = AttendanceSession
R = AttendanceRecord
E = StudentEnrollment


def enrollment_covers(on_date: Any) -> ColumnElement[bool]:
    """The enrollment placed the student in its section on ``on_date``.

    ``end_date`` of a *superseded* row is the move date, when the student is already in
    the new section (exclusive); for every other status it is the last day (inclusive).
    """
    return and_(
        E.enrollment_date <= on_date,
        or_(
            E.end_date.is_(None),
            and_(E.status == EnrollmentStatus.SUPERSEDED.value, on_date < E.end_date),
            and_(E.status != EnrollmentStatus.SUPERSEDED.value, on_date <= E.end_date),
        ),
    )


def _placement_of(session: AttendanceSession) -> list[ColumnElement[bool]]:
    return [
        E.school_id == session.school_id,
        E.academic_year_id == session.academic_year_id,
        E.class_id == session.class_id,
        E.section_id == session.section_id,
        enrollment_covers(session.session_date),
    ]


class AttendanceSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], session_id: uuid.UUID, *, for_update: bool = False
    ) -> AttendanceSession | None:
        stmt = select(S).where(S.id == session_id, visible)
        if for_update:
            # Re-read a row already in the identity map: the locked values are the truth.
            stmt = stmt.with_for_update(of=S).execution_options(populate_existing=True)
        return self.session.scalars(stmt).one_or_none()

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[AttendanceSession], int]:
        conditions = [visible, *filters]
        items = self.session.scalars(
            select(S)
            .where(*conditions)
            .order_by(S.session_date.desc(), S.section_id, S.period, S.subject_id, S.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(S).where(*conditions))
        return items, total or 0


class AttendanceRecordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], record_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[AttendanceRecord, AttendanceSession] | None:
        stmt = select(R, S).join(S, S.id == R.session_id).where(R.id == record_id, visible)
        if for_update:
            stmt = stmt.with_for_update(of=R).execution_options(populate_existing=True)
        row = self.session.execute(stmt).one_or_none()
        return (row[0], row[1]) if row else None

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[AttendanceRecord, AttendanceSession]], int]:
        conditions = [visible, *filters]
        rows = self.session.execute(
            select(R, S)
            .join(S, S.id == R.session_id)
            .join(Student, Student.id == R.student_id)
            .where(*conditions)
            .order_by(
                S.session_date.desc(),
                S.period,
                S.subject_id,
                Student.last_name,
                Student.first_name,
                Student.admission_number,
                R.id,
            )
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count()).select_from(R).join(S, S.id == R.session_id).where(*conditions)
        )
        return [(row[0], row[1]) for row in rows], total or 0

    def students_with_records(
        self, session_id: uuid.UUID, student_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        return set(
            self.session.scalars(
                select(R.student_id).where(
                    R.session_id == session_id, R.student_id.in_(student_ids)
                )
            )
        )

    def eligible_enrollments(
        self, session: AttendanceSession, student_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """student_id -> the enrollment placing them in the session's section that day."""
        rows = self.session.execute(
            select(E.student_id, E.id)
            .where(E.student_id.in_(student_ids), *_placement_of(session))
            .order_by(E.enrollment_date.desc())
        ).all()
        found: dict[uuid.UUID, uuid.UUID] = {}
        for student_id, enrollment_id in rows:
            found.setdefault(student_id, enrollment_id)
        return found

    def unmarked(
        self, session: AttendanceSession, *, offset: int, limit: int
    ) -> tuple[list[tuple[Student, uuid.UUID]], int]:
        """Students eligible for the session (enrolled in its section that day) without a
        record in it."""
        conditions = [
            *_placement_of(session),
            ~select(R.id).where(R.session_id == session.id, R.student_id == E.student_id).exists(),
        ]
        rows = self.session.execute(
            select(Student, E.id)
            .join(E, E.student_id == Student.id)
            .where(*conditions)
            .order_by(Student.last_name, Student.first_name, Student.admission_number)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(E).where(*conditions))
        return [(row[0], row[1]) for row in rows], total or 0

    def next_change_sequence(self, record_id: uuid.UUID) -> int:
        """Next history position; callers hold the record lock (the unique key backs it)."""
        last = self.session.scalar(
            select(func.max(AttendanceRecordChange.sequence)).where(
                AttendanceRecordChange.record_id == record_id
            )
        )
        return (last or 0) + 1

    def changes(self, record_id: uuid.UUID) -> Sequence[AttendanceRecordChange]:
        return self.session.scalars(
            select(AttendanceRecordChange)
            .where(AttendanceRecordChange.record_id == record_id)
            .order_by(AttendanceRecordChange.sequence)
        ).all()

    def status_counts(
        self, visible: ColumnElement[bool], filters: Sequence[ColumnElement[bool]]
    ) -> dict[str, int]:
        columns = [
            func.count().filter(R.status == status.value).label(status.value)
            for status in AttendanceStatus
        ]
        row = self.session.execute(
            select(*columns).select_from(R).join(S, S.id == R.session_id).where(visible, *filters)
        ).one()
        return {status.value: int(row._mapping[status.value]) for status in AttendanceStatus}


def date_range(
    column: Any, *, on: date | None = None, start: date | None = None, end: date | None = None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if on is not None:
        conditions.append(column == on)
    if start is not None:
        conditions.append(column >= start)
    if end is not None:
        conditions.append(column <= end)
    return conditions
