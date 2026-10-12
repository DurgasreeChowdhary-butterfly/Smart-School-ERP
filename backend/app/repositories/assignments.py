"""Data access for assignments, submissions, revisions and review history.

Callers pass an authorization condition from :mod:`app.security.assignment_scope`; these
repositories never return rows outside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import date
from typing import Literal

from sqlalchemy import ColumnElement, Date, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models.assignments import (
    Assignment,
    AssignmentReviewChange,
    AssignmentSubmission,
    AssignmentSubmissionRevision,
)
from app.models.people import Student, StudentEnrollment
from app.repositories.attendance import enrollment_covers

A = Assignment
SUB = AssignmentSubmission
REV = AssignmentSubmissionRevision
E = StudentEnrollment
Lock = Literal["share", "update"] | None
type StudentRow = tuple[Student, StudentEnrollment, AssignmentSubmission | None]
type StudentRows = list[StudentRow]


class AssignmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], assignment_id: uuid.UUID, *, lock: Lock = None
    ) -> Assignment | None:
        stmt = select(A).where(A.id == assignment_id, visible)
        if lock is not None:
            # Re-read a row already in the identity map: the locked values are the truth.
            stmt = stmt.with_for_update(of=A, read=lock == "share").execution_options(
                populate_existing=True
            )
        return self.session.scalars(stmt).one_or_none()

    def is_visible(self, visible: ColumnElement[bool], assignment_id: uuid.UUID) -> bool:
        return bool(
            self.session.scalar(
                select(func.count()).select_from(A).where(A.id == assignment_id, visible)
            )
        )

    def visible_ids(
        self, visible: ColumnElement[bool], assignment_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not assignment_ids:
            return set()
        return set(self.session.scalars(select(A.id).where(A.id.in_(assignment_ids), visible)))

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Assignment], int]:
        conditions = [visible, *filters]
        items = self.session.scalars(
            select(A)
            .where(*conditions)
            .order_by(A.due_at.desc(), A.title, A.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(A).where(*conditions))
        return items, total or 0

    def students(
        self,
        assignment: Assignment,
        *,
        outstanding_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[list[StudentRow], int]:
        """Students enrolled in the assignment's section during its window, with their
        submission if any (``outstanding_only``: those without one)."""
        conditions: list[ColumnElement[bool]] = [
            E.school_id == assignment.school_id,
            E.academic_year_id == assignment.academic_year_id,
            E.class_id == assignment.class_id,
            E.section_id == assignment.section_id,
            E.enrollment_date <= cast(assignment.due_at, Date),
            or_(E.end_date.is_(None), E.end_date >= assignment.assigned_on),
        ]
        if outstanding_only:
            conditions.append(SUB.id.is_(None))
        joined = (
            select(Student, E, SUB)
            .join(E, E.student_id == Student.id)
            .outerjoin(SUB, (SUB.assignment_id == assignment.id) & (SUB.student_id == Student.id))
            .where(*conditions)
        )
        rows = self.session.execute(
            joined.order_by(Student.last_name, Student.first_name, Student.admission_number, E.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count())
            .select_from(E)
            .outerjoin(SUB, (SUB.assignment_id == assignment.id) & (SUB.student_id == E.student_id))
            .where(*conditions)
        )
        return [(row[0], row[1], row[2]) for row in rows], total or 0


class SubmissionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def eligible_enrollment(
        self, assignment: Assignment, student_id: uuid.UUID, on: date
    ) -> StudentEnrollment | None:
        """The enrollment placing the student in the assignment's section on ``on``."""
        return self.session.scalars(
            select(E)
            .where(
                E.student_id == student_id,
                E.school_id == assignment.school_id,
                E.academic_year_id == assignment.academic_year_id,
                E.class_id == assignment.class_id,
                E.section_id == assignment.section_id,
                enrollment_covers(on),
            )
            .order_by(E.enrollment_date.desc())
            .limit(1)
        ).first()

    def find(self, assignment_id: uuid.UUID, student_id: uuid.UUID) -> AssignmentSubmission | None:
        return self.session.scalars(
            select(SUB).where(SUB.assignment_id == assignment_id, SUB.student_id == student_id)
        ).one_or_none()

    def get_visible(
        self, visible: ColumnElement[bool], submission_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[AssignmentSubmission, Assignment] | None:
        stmt = (
            select(SUB, A)
            .join(A, A.id == SUB.assignment_id)
            .where(SUB.id == submission_id, visible)
        )
        if for_update:
            stmt = stmt.with_for_update(of=SUB).execution_options(populate_existing=True)
        row = self.session.execute(stmt).one_or_none()
        return (row[0], row[1]) if row else None

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[AssignmentSubmission, Assignment]], int]:
        conditions = [visible, *filters]
        rows = self.session.execute(
            select(SUB, A)
            .join(A, A.id == SUB.assignment_id)
            .join(Student, Student.id == SUB.student_id)
            .where(*conditions)
            .order_by(
                A.due_at.desc(),
                Student.last_name,
                Student.first_name,
                Student.admission_number,
                SUB.id,
            )
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count())
            .select_from(SUB)
            .join(A, A.id == SUB.assignment_id)
            .where(*conditions)
        )
        return [(row[0], row[1]) for row in rows], total or 0

    def latest_content(self, submission_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        rows = self.session.execute(
            select(REV.submission_id, REV.content)
            .join(SUB, SUB.id == REV.submission_id)
            .where(REV.submission_id.in_(submission_ids), REV.revision == SUB.revision_count)
        ).all()
        return {row[0]: row[1] for row in rows}

    def student_user(self, student_id: uuid.UUID) -> uuid.UUID | None:
        return self.session.scalar(select(Student.user_id).where(Student.id == student_id))

    def revisions(self, submission_id: uuid.UUID) -> Sequence[AssignmentSubmissionRevision]:
        return self.session.scalars(
            select(REV).where(REV.submission_id == submission_id).order_by(REV.revision)
        ).all()

    def review_changes(self, submission_id: uuid.UUID) -> Sequence[AssignmentReviewChange]:
        return self.session.scalars(
            select(AssignmentReviewChange)
            .where(AssignmentReviewChange.submission_id == submission_id)
            .order_by(AssignmentReviewChange.sequence)
        ).all()

    def next_review_sequence(self, submission_id: uuid.UUID) -> int:
        last = self.session.scalar(
            select(func.max(AssignmentReviewChange.sequence)).where(
                AssignmentReviewChange.submission_id == submission_id
            )
        )
        return (last or 0) + 1
