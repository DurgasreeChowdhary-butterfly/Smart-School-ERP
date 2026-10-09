"""Result calculation, publication, reopening and report cards.

Publication model (snapshot)
----------------------------
Publishing freezes the results: one ``exam_publications`` row (numbered version, the
exam configuration used, who and when) plus one immutable ``exam_report_cards`` row per
student. Report cards are read from that snapshot, never recalculated, so a later
correction cannot silently change a published report card.

* **Publish** (``marks.publish``, school staff): exam ``marks_entry -> published``.
  Every eligible student needs an entry for every component they are eligible for;
  otherwise publishing is refused unless ``allow_incomplete`` is set, and those report
  cards say ``incomplete`` (missing marks are never treated as zero).
* **Reopen** (``marks.publish``, reason required): withdraws the active publication
  (kept as history, hidden from parents and students) and returns the exam to
  ``marks_entry`` for corrections. Publishing again creates the next version.
* Parents and students see only the **active** publication; staff see every version.
  Faculty do not read report cards (they span every subject).

All arithmetic lives in :mod:`app.services.result_calculation`; the live preview and the
publication use the same code.

Concurrency: publish and reopen lock the exam row ``FOR UPDATE``. Marks writers hold it
``FOR SHARE``, so a correction racing a publication either commits first (and is
included) or waits and is then refused because the exam is published.
``uq_exam_publications_one_active`` and ``UNIQUE (exam_id, version)`` are the final
authority; triggers keep ``published`` <=> an active publication.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear, SchoolClass, Section, Subject
from app.models.exams import (
    Exam,
    ExamPublication,
    ExamReportCard,
    ExamStatus,
    ExamSubject,
    PublicationStatus,
)
from app.models.people import Student
from app.models.school import School
from app.repositories.exams import ExamResultRepository, Placement
from app.schemas.common import ErrorDetail
from app.schemas.exams import (
    PublicationRead,
    ReportCardExam,
    ReportCardRead,
    StudentRef,
    StudentResult,
    SubjectResult,
)
from app.security.exam_scope import assessment_date, report_card_visibility
from app.security.school_scope import SchoolScope
from app.services.exams import ExamBase, now, require_school_staff
from app.services.result_calculation import (
    Component,
    Entry,
    Outcome,
    bands_from,
    calculate,
)

RC = ExamReportCard
PUB = ExamPublication
STAFF_ONLY = "Exam results are managed by school staff."
PUBLICATION_CONSTRAINTS = {
    "uq_exam_publications_one_active": ("status", "The exam's results are already published."),
    "uq_exam_publications_exam_id_version": (
        "status",
        "The exam was published concurrently. Please retry.",
    ),
    "exam_publications_lifecycle": ("status", "The exam is not open for publication."),
    "exams_published_has_publication": ("status", "The exam is not published."),
    "exams_lifecycle": ("status", "This status change is not allowed."),
}


@dataclass(frozen=True, slots=True)
class StudentOutcome:
    student_id: uuid.UUID
    placement: Placement
    outcome: Outcome


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class ExamResultService(ExamBase):
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        super().__init__(session, context)
        self.results = ExamResultRepository(session)

    # -------------------------------------------------------------- calculation
    def _components(self, exam: Exam) -> list[Component]:
        rows = self.session.execute(
            select(ExamSubject, Subject)
            .join(Subject, Subject.id == ExamSubject.subject_id)
            .where(ExamSubject.exam_id == exam.id)
        ).all()
        return [
            Component(
                id=item.id,
                subject_id=item.subject_id,
                subject_code=subject.code,
                subject_name=subject.name,
                max_marks=item.max_marks,
                pass_marks=item.pass_marks,
            )
            for item, subject in rows
        ]

    def calculate(self, exam: Exam) -> list[StudentOutcome]:
        """Every student's result from the current marks (eligible students and anyone
        holding a mark)."""
        components = self._components(exam)
        placements: dict[uuid.UUID, Placement] = {}
        applicable: dict[uuid.UUID, set[uuid.UUID]] = {}
        # Latest assessment last: a student who moved section keeps the newest placement.
        for item in self.subjects.for_exam(exam.id):
            on = assessment_date(exam, item)
            for student_id, placement in self.marks.eligible(exam, on, None).items():
                placements[student_id] = placement
                applicable.setdefault(student_id, set()).add(item.id)
        entries: dict[uuid.UUID, dict[uuid.UUID, Entry]] = {}
        for mark in self.marks.for_exam(exam.id):
            entries.setdefault(mark.student_id, {})[mark.exam_subject_id] = Entry(
                mark.status, mark.marks_obtained
            )
            placements.setdefault(mark.student_id, (mark.enrollment_id, mark.section_id))
        bands = bands_from(exam.grade_bands)
        return [
            StudentOutcome(
                student_id,
                placement,
                calculate(
                    components,
                    entries.get(student_id, {}),
                    applicable=applicable.get(student_id, set()),
                    pass_percentage=exam.pass_percentage,
                    bands=bands,
                ),
            )
            for student_id, placement in placements.items()
        ]

    def _students(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Student]:
        rows = self.session.scalars(select(Student).where(Student.id.in_(ids)))
        return {student.id: student for student in rows}

    def preview(
        self, scope: SchoolScope, exam_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[StudentResult], int]:
        """Live results from the current marks (staff only; not a publication)."""
        exam = self.visible_exam(scope, exam_id)
        require_school_staff(scope, STAFF_ONLY)
        outcomes = self.calculate(exam)
        students = self._students([o.student_id for o in outcomes])
        ordered = sorted(
            outcomes,
            key=lambda o: (
                students[o.student_id].last_name,
                students[o.student_id].first_name,
                students[o.student_id].admission_number,
            ),
        )
        page = ordered[offset : offset + limit]
        return [
            StudentResult(
                student=_student_ref(students[o.student_id]),
                enrollment_id=o.placement[0],
                section_id=o.placement[1],
                subjects=[SubjectResult.model_validate(line.as_json()) for line in o.outcome.lines],
                total_obtained=o.outcome.total_obtained,
                total_max=o.outcome.total_max,
                percentage=o.outcome.percentage,
                grade=o.outcome.grade,
                result=o.outcome.result,
            )
            for o in page
        ], len(ordered)

    # -------------------------------------------------------------- publication
    def _settings(self, exam: Exam) -> dict[str, Any]:
        """The configuration a publication was calculated with (frozen with it)."""
        school = self.session.get(School, exam.school_id)
        year = self.session.get(AcademicYear, exam.academic_year_id)
        school_class = self.session.get(SchoolClass, exam.class_id)
        # Restricted foreign keys: these rows always exist.
        assert school is not None  # noqa: S101
        assert year is not None  # noqa: S101
        assert school_class is not None  # noqa: S101
        sections = self.session.scalars(select(Section).where(Section.class_id == exam.class_id))
        return {
            "exam": {
                "id": str(exam.id),
                "name": exam.name,
                "exam_type": exam.exam_type,
                "start_date": str(exam.start_date),
                "end_date": str(exam.end_date),
                "section_id": str(exam.section_id) if exam.section_id else None,
                "pass_percentage": str(exam.pass_percentage)
                if exam.pass_percentage is not None
                else None,
                "grade_bands": exam.grade_bands,
            },
            "school_name": school.name,
            "academic_year_name": year.name,
            "class_name": school_class.name,
            "sections": {str(section.id): section.name for section in sections},
            "components": [
                {
                    "exam_subject_id": str(c.id),
                    "subject_id": str(c.subject_id),
                    "subject_code": c.subject_code,
                    "subject_name": c.subject_name,
                    "max_marks": str(c.max_marks),
                    "pass_marks": str(c.pass_marks) if c.pass_marks is not None else None,
                }
                for c in self._components(exam)
            ],
        }

    def publish(
        self, scope: SchoolScope, exam_id: uuid.UUID, *, allow_incomplete: bool
    ) -> PublicationRead:
        exam = self.visible_exam(scope, exam_id, lock="update")
        require_school_staff(scope, STAFF_ONLY)
        if exam.status != ExamStatus.MARKS_ENTRY:
            raise ConflictError(
                f"Only an exam in marks entry can be published (it is {exam.status})."
            )
        outcomes = self.calculate(exam)
        if not outcomes:
            raise BusinessRuleError("No students are eligible for this exam.")
        incomplete = [o for o in outcomes if o.outcome.pending]
        if incomplete and not allow_incomplete:
            missing = sum(o.outcome.pending for o in incomplete)
            raise BusinessRuleError(
                f"{len(incomplete)} student(s) have no entry for {missing} exam subject(s). "
                "Enter their marks, mark them absent or exempt, or publish with "
                "allow_incomplete.",
                details=[
                    ErrorDetail(
                        loc=["body", "allow_incomplete"],
                        message=f"{len(incomplete)} incomplete student result(s).",
                    )
                ],
            )
        at = now()
        publication = ExamPublication(
            school_id=scope.school_id,
            exam_id=exam.id,
            version=self.results.next_version(exam.id),
            status=PublicationStatus.ACTIVE.value,
            settings=self._settings(exam),
            published_by_user_id=scope.actor_id,
            published_at=at,
        )
        cards = [
            ExamReportCard(
                school_id=scope.school_id,
                exam_id=exam.id,
                student_id=o.student_id,
                enrollment_id=o.placement[0],
                academic_year_id=exam.academic_year_id,
                class_id=exam.class_id,
                section_id=o.placement[1],
                total_obtained=o.outcome.total_obtained,
                total_max=o.outcome.total_max,
                percentage=o.outcome.percentage,
                grade=o.outcome.grade,
                result=o.outcome.result,
                subjects=[line.as_json() for line in o.outcome.lines],
            )
            for o in outcomes
        ]

        def add() -> None:
            self.session.add(publication)
            self.session.flush()
            for card in cards:
                card.publication_id = publication.id
            self.session.add_all(cards)

        # Order matters for the triggers: publication and report cards first, then the
        # exam becomes published (it requires the active publication to exist).
        self.writer.flush(PUBLICATION_CONSTRAINTS, before=add)
        self.writer.flush(
            PUBLICATION_CONSTRAINTS,
            before=lambda: setattr(exam, "status", ExamStatus.PUBLISHED.value),
        )
        self.audit(
            AuditAction.EXAM_PUBLISHED,
            scope,
            exam,
            publication_id=str(publication.id),
            version=publication.version,
            report_cards=len(cards),
            incomplete=len(incomplete),
        )
        self.session.commit()
        return _publication_read(publication, len(cards), len(incomplete))

    def reopen(self, scope: SchoolScope, exam_id: uuid.UUID, reason: str) -> PublicationRead:
        exam = self.visible_exam(scope, exam_id, lock="update")
        require_school_staff(scope, STAFF_ONLY)
        if exam.status != ExamStatus.PUBLISHED:
            raise ConflictError(f"Only a published exam can be reopened (it is {exam.status}).")
        publication = self.results.active_publication(exam.id)
        assert publication is not None  # noqa: S101 - guaranteed by the lifecycle trigger

        def withdraw() -> None:
            publication.status = PublicationStatus.WITHDRAWN.value
            publication.withdrawn_by_user_id = scope.actor_id
            publication.withdrawn_at = now()
            publication.withdrawal_reason = reason

        # Withdraw first: the trigger refuses to leave "published" with an active one.
        self.writer.flush(PUBLICATION_CONSTRAINTS, before=withdraw)
        self.writer.flush(
            PUBLICATION_CONSTRAINTS,
            before=lambda: setattr(exam, "status", ExamStatus.MARKS_ENTRY.value),
        )
        self.audit(
            AuditAction.EXAM_REOPENED,
            scope,
            exam,
            previous_status=ExamStatus.PUBLISHED.value,
            publication_id=str(publication.id),
            version=publication.version,
            reason=reason,
        )
        self.session.commit()
        counts = self.results.counts([publication.id]).get(publication.id, (0, 0))
        return _publication_read(publication, *counts)

    # -------------------------------------------------------------------- reads
    def publications(self, scope: SchoolScope, exam_id: uuid.UUID) -> list[PublicationRead]:
        exam = self.visible_exam(scope, exam_id)
        require_school_staff(scope, STAFF_ONLY)
        items = self.results.publications(exam.id)
        counts = self.results.counts([item.id for item in items])
        return [_publication_read(item, *counts.get(item.id, (0, 0))) for item in items]

    def exam_report_cards(
        self,
        scope: SchoolScope,
        exam_id: uuid.UUID,
        *,
        version: int | None,
        offset: int,
        limit: int,
    ) -> tuple[list[ReportCardRead], int]:
        """One publication's report cards: the active one, or ``version``."""
        exam = self.visible_exam(scope, exam_id)
        require_school_staff(scope, STAFF_ONLY)
        filters: list[ColumnElement[bool]] = [RC.exam_id == exam.id]
        if version is None:
            filters.append(PUB.status == PublicationStatus.ACTIVE.value)
        else:
            filters.append(PUB.version == version)
        return self._cards(scope, filters, offset=offset, limit=limit)

    def report_cards(
        self,
        scope: SchoolScope,
        *,
        student_id: uuid.UUID | None,
        academic_year_id: uuid.UUID | None,
        exam_id: uuid.UUID | None,
        offset: int,
        limit: int,
    ) -> tuple[list[ReportCardRead], int]:
        """Currently published report cards visible to the caller. Filters only narrow."""
        filters: list[ColumnElement[bool]] = [PUB.status == PublicationStatus.ACTIVE.value]
        for column, value in (
            (RC.student_id, student_id),
            (RC.academic_year_id, academic_year_id),
            (RC.exam_id, exam_id),
        ):
            if value is not None:
                filters.append(column == value)
        return self._cards(scope, filters, offset=offset, limit=limit)

    def report_card(self, scope: SchoolScope, card_id: uuid.UUID) -> ReportCardRead:
        row = self.results.report_card(
            report_card_visibility(scope.principal, scope.school_id), card_id
        )
        if row is None:
            raise NotFoundError("Report card not found.")
        return _card_read(*row)

    def _cards(
        self,
        scope: SchoolScope,
        filters: list[ColumnElement[bool]],
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[ReportCardRead], int]:
        rows, total = self.results.report_cards(
            report_card_visibility(scope.principal, scope.school_id),
            filters,
            offset=offset,
            limit=limit,
        )
        return [_card_read(*row) for row in rows], total


def _student_ref(student: Student) -> StudentRef:
    return StudentRef(
        id=student.id,
        admission_number=student.admission_number,
        first_name=student.first_name,
        last_name=student.last_name,
    )


def _publication_read(
    publication: ExamPublication, report_cards: int, incomplete: int
) -> PublicationRead:
    read = PublicationRead.model_validate(publication)
    return read.model_copy(update={"report_cards": report_cards, "incomplete": incomplete})


def _card_read(
    card: ExamReportCard, publication: ExamPublication, student: Student
) -> ReportCardRead:
    settings = publication.settings
    exam = settings["exam"]
    return ReportCardRead(
        id=card.id,
        school_id=card.school_id,
        school_name=settings["school_name"],
        publication_id=publication.id,
        publication_version=publication.version,
        publication_status=publication.status,
        published_at=publication.published_at,
        exam=ReportCardExam(
            id=card.exam_id,
            name=exam["name"],
            exam_type=exam["exam_type"],
            start_date=exam["start_date"],
            end_date=exam["end_date"],
            section_id=exam["section_id"],
            pass_percentage=_decimal(exam["pass_percentage"]),
            grade_bands=exam["grade_bands"],
        ),
        student=_student_ref(student),
        enrollment_id=card.enrollment_id,
        academic_year_id=card.academic_year_id,
        academic_year_name=settings["academic_year_name"],
        class_id=card.class_id,
        class_name=settings["class_name"],
        section_id=card.section_id,
        section_name=settings["sections"].get(str(card.section_id)),
        subjects=[SubjectResult.model_validate(line) for line in card.subjects],
        total_obtained=card.total_obtained,
        total_max=card.total_max,
        percentage=card.percentage,
        grade=card.grade,
        result=card.result,
    )
