"""Data access for exams, components, marks, publications and report cards.

Callers pass an authorization condition from :mod:`app.security.exam_scope`; these
repositories never return rows outside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models.exams import (
    Exam,
    ExamMark,
    ExamMarkChange,
    ExamPublication,
    ExamReportCard,
    ExamSubject,
    PublicationStatus,
)
from app.models.people import Student, StudentEnrollment
from app.repositories.attendance import enrollment_covers

E = Exam
C = ExamSubject
M = ExamMark
EN = StudentEnrollment

Lock = Literal["share", "update"] | None
type Placement = tuple[uuid.UUID, uuid.UUID]  # (enrollment_id, section_id)


class ExamRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(
        self, visible: ColumnElement[bool], exam_id: uuid.UUID, *, lock: Lock = None
    ) -> Exam | None:
        stmt = select(E).where(E.id == exam_id, visible)
        if lock is not None:
            # Re-read a row already in the identity map: the locked values are the truth.
            stmt = stmt.with_for_update(of=E, read=lock == "share").execution_options(
                populate_existing=True
            )
        return self.session.scalars(stmt).one_or_none()

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Exam], int]:
        conditions = [visible, *filters]
        items = self.session.scalars(
            select(E)
            .where(*conditions)
            .order_by(E.start_date.desc(), E.name, E.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(E).where(*conditions))
        return items, total or 0


class ExamSubjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, exam_id: uuid.UUID, component_id: uuid.UUID) -> ExamSubject | None:
        return self.session.scalars(
            select(C).where(C.id == component_id, C.exam_id == exam_id)
        ).one_or_none()

    def for_exam(self, exam_id: uuid.UUID) -> Sequence[ExamSubject]:
        return self.session.scalars(
            select(C).where(C.exam_id == exam_id).order_by(C.assessment_date, C.id)
        ).all()

    def page(
        self, exam_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[Sequence[ExamSubject], int]:
        items = self.session.scalars(
            select(C)
            .where(C.exam_id == exam_id)
            .order_by(C.assessment_date, C.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(C).where(C.exam_id == exam_id))
        return items, total or 0

    def outside(self, exam_id: uuid.UUID, start: date, end: date) -> bool:
        """Does any component's assessment date fall outside ``start..end``?"""
        return bool(
            self.session.scalar(
                select(func.count())
                .select_from(C)
                .where(
                    C.exam_id == exam_id,
                    C.assessment_date.is_not(None),
                    (C.assessment_date < start) | (C.assessment_date > end),
                )
            )
        )


def _eligible_conditions(
    exam: Exam, on_date: date, sections: set[uuid.UUID] | None
) -> list[ColumnElement[bool]]:
    """Enrollments placing a student in the exam's year/class (and section) on the date."""
    conditions = [
        EN.school_id == exam.school_id,
        EN.academic_year_id == exam.academic_year_id,
        EN.class_id == exam.class_id,
        enrollment_covers(on_date),
    ]
    if exam.section_id is not None:
        conditions.append(EN.section_id == exam.section_id)
    if sections is not None:
        conditions.append(EN.section_id.in_(sections))
    return conditions


class ExamMarkRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- eligibility
    def eligible(
        self,
        exam: Exam,
        on_date: date,
        sections: set[uuid.UUID] | None,
        student_ids: Sequence[uuid.UUID] | None = None,
    ) -> dict[uuid.UUID, Placement]:
        """student_id -> (enrollment, section) placing them in the exam on ``on_date``."""
        stmt = select(EN.student_id, EN.id, EN.section_id).where(
            *_eligible_conditions(exam, on_date, sections)
        )
        if student_ids is not None:
            stmt = stmt.where(EN.student_id.in_(student_ids))
        found: dict[uuid.UUID, Placement] = {}
        for student_id, enrollment_id, section_id in self.session.execute(
            stmt.order_by(EN.enrollment_date.desc())
        ):
            found.setdefault(student_id, (enrollment_id, section_id))
        return found

    def eligible_page(
        self,
        exam: Exam,
        component: ExamSubject,
        on_date: date,
        sections: set[uuid.UUID] | None,
        *,
        unmarked_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[Student, StudentEnrollment, ExamMark | None]], int]:
        conditions = _eligible_conditions(exam, on_date, sections)
        joined = (
            select(Student, EN, M)
            .join(EN, EN.student_id == Student.id)
            .outerjoin(M, (M.exam_subject_id == component.id) & (M.student_id == Student.id))
        )
        if unmarked_only:
            conditions.append(M.id.is_(None))
        rows = self.session.execute(
            joined.where(*conditions)
            .order_by(Student.last_name, Student.first_name, Student.admission_number, EN.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count())
            .select_from(EN)
            .outerjoin(M, (M.exam_subject_id == component.id) & (M.student_id == EN.student_id))
            .where(*conditions)
        )
        return [(row[0], row[1], row[2]) for row in rows], total or 0

    # ---------------------------------------------------------------------- marks
    def students_with_marks(
        self, component_id: uuid.UUID, student_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        return set(
            self.session.scalars(
                select(M.student_id).where(
                    M.exam_subject_id == component_id, M.student_id.in_(student_ids)
                )
            )
        )

    def get_visible(
        self, visible: ColumnElement[bool], mark_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[ExamMark, ExamSubject, Exam] | None:
        stmt = (
            select(M, C, E)
            .join(C, C.id == M.exam_subject_id)
            .join(E, E.id == M.exam_id)
            .where(M.id == mark_id, visible)
        )
        if for_update:
            stmt = stmt.with_for_update(of=M).execution_options(populate_existing=True)
        row = self.session.execute(stmt).one_or_none()
        return (row[0], row[1], row[2]) if row else None

    def list_visible(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[ExamMark], int]:
        conditions = [visible, *filters]
        base = select(M).join(C, C.id == M.exam_subject_id).join(E, E.id == M.exam_id)
        items = self.session.scalars(
            base.join(Student, Student.id == M.student_id)
            .where(*conditions)
            .order_by(Student.last_name, Student.first_name, Student.admission_number, M.id)
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count())
            .select_from(M)
            .join(C, C.id == M.exam_subject_id)
            .join(E, E.id == M.exam_id)
            .where(*conditions)
        )
        return items, total or 0

    def for_exam(self, exam_id: uuid.UUID) -> Sequence[ExamMark]:
        return self.session.scalars(select(M).where(M.exam_id == exam_id)).all()

    def exists_for_component(self, component_id: uuid.UUID) -> bool:
        return bool(
            self.session.scalar(
                select(func.count()).select_from(M).where(M.exam_subject_id == component_id)
            )
        )

    def next_change_sequence(self, mark_id: uuid.UUID) -> int:
        """Next history position; callers hold the mark lock (the unique key backs it)."""
        last = self.session.scalar(
            select(func.max(ExamMarkChange.sequence)).where(ExamMarkChange.mark_id == mark_id)
        )
        return (last or 0) + 1

    def changes(self, mark_id: uuid.UUID) -> Sequence[ExamMarkChange]:
        return self.session.scalars(
            select(ExamMarkChange)
            .where(ExamMarkChange.mark_id == mark_id)
            .order_by(ExamMarkChange.sequence)
        ).all()


class ExamResultRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def publications(self, exam_id: uuid.UUID) -> Sequence[ExamPublication]:
        return self.session.scalars(
            select(ExamPublication)
            .where(ExamPublication.exam_id == exam_id)
            .order_by(ExamPublication.version)
        ).all()

    def active_publication(self, exam_id: uuid.UUID) -> ExamPublication | None:
        return self.session.scalars(
            select(ExamPublication).where(
                ExamPublication.exam_id == exam_id,
                ExamPublication.status == PublicationStatus.ACTIVE.value,
            )
        ).one_or_none()

    def next_version(self, exam_id: uuid.UUID) -> int:
        last = self.session.scalar(
            select(func.max(ExamPublication.version)).where(ExamPublication.exam_id == exam_id)
        )
        return (last or 0) + 1

    def counts(self, publication_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
        """publication_id -> (report cards, incomplete report cards)."""
        rows = self.session.execute(
            select(
                ExamReportCard.publication_id,
                func.count(),
                func.count().filter(ExamReportCard.result == "incomplete"),
            )
            .where(ExamReportCard.publication_id.in_(publication_ids))
            .group_by(ExamReportCard.publication_id)
        ).all()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    def report_card(
        self, visible: ColumnElement[bool], card_id: uuid.UUID
    ) -> tuple[ExamReportCard, ExamPublication, Student] | None:
        row = self.session.execute(
            select(ExamReportCard, ExamPublication, Student)
            .join(ExamPublication, ExamPublication.id == ExamReportCard.publication_id)
            .join(Student, Student.id == ExamReportCard.student_id)
            .where(ExamReportCard.id == card_id, visible)
        ).one_or_none()
        return (row[0], row[1], row[2]) if row else None

    def report_cards(
        self,
        visible: ColumnElement[bool],
        filters: Sequence[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[ExamReportCard, ExamPublication, Student]], int]:
        conditions = [visible, *filters]
        rows = self.session.execute(
            select(ExamReportCard, ExamPublication, Student)
            .join(ExamPublication, ExamPublication.id == ExamReportCard.publication_id)
            .join(Student, Student.id == ExamReportCard.student_id)
            .where(*conditions)
            .order_by(
                ExamPublication.published_at.desc(),
                Student.last_name,
                Student.first_name,
                Student.admission_number,
                ExamReportCard.id,
            )
            .offset(offset)
            .limit(limit)
        ).all()
        total = self.session.scalar(
            select(func.count())
            .select_from(ExamReportCard)
            .join(ExamPublication, ExamPublication.id == ExamReportCard.publication_id)
            .where(*conditions)
        )
        return [(row[0], row[1], row[2]) for row in rows], total or 0
