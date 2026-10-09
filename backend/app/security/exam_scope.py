"""Exam and marks authorization, composed from the existing scopes (no parallel system).

* **School-wide** callers (SUPER_ADMIN in the selected school, SCHOOL_ADMIN, MANAGEMENT;
  see :func:`app.security.attendance_scope.is_school_wide`) read every exam, mark, live
  result and report card of the school. Configuring exams and entering marks also needs
  ``marks.manage`` (SUPER_ADMIN, SCHOOL_ADMIN); publishing needs ``marks.publish``
  (SUPER_ADMIN, SCHOOL_ADMIN, MANAGEMENT).
* **Faculty** reach exams and marks only through their *effective* Phase 5 assignments
  (:func:`app.security.faculty_scope.effective_assignments`):

  =============  ==============================================================
  Exams          an assignment for the exam's year + class, and for the exam's
                 section when it names one (any subject: they see the timetable)
  Marks (read    an assignment for the exam's year + class + the student's
  and write)     section + the component's **subject**, effective on the
                 component's assessment date (default: the exam's start date)
  =============  ==============================================================

  Faculty never configure or publish exams and never read whole report cards: a
  report card spans every subject, and an assignment covers one.
* **Parents / students** read only *published* report cards (the active publication)
  of linked children / themselves
  (:func:`app.security.people_scope.personal_student_visibility`). They never see
  exams, marks, live results or withdrawn publications.

Everything is a SQL condition applied inside the query, so an ID swapped in a URL, body
or filter cannot escape it.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import ColumnElement, and_, exists, false, func, or_, select
from sqlalchemy.orm import Session

from app.models.academic import Section
from app.models.exams import (
    Exam,
    ExamMark,
    ExamPublication,
    ExamReportCard,
    ExamSubject,
    PublicationStatus,
)
from app.models.faculty import Faculty, FacultyAssignment
from app.models.people import Student
from app.security.attendance_scope import faculty_teaches, is_school_wide
from app.security.faculty_scope import effective_assignments
from app.security.people_scope import personal_student_visibility
from app.security.permissions import PermissionName
from app.security.principal import Principal

P = PermissionName
E = Exam
C = ExamSubject
M = ExamMark
FA = FacultyAssignment


def _is_faculty(principal: Principal) -> bool:
    return principal.has_permission(P.FACULTY_READ_ASSIGNED_STUDENTS)


def assessment_date(exam: Exam, component: ExamSubject) -> date:
    """The date eligibility and assignment effectiveness are judged on."""
    return component.assessment_date or exam.start_date


def exam_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Exams the caller may read (the outer query selects ``Exam``)."""
    in_school = E.school_id == school_id
    if is_school_wide(principal):
        return in_school
    if _is_faculty(principal):
        teaches_class = exists(
            select(FA.id)
            .select_from(FA, Faculty)
            .where(
                *effective_assignments(principal.user_id, school_id),
                FA.academic_year_id == E.academic_year_id,
                FA.class_id == E.class_id,
                or_(E.section_id.is_(None), FA.section_id == E.section_id),
            )
            .correlate_except(FA, Faculty)
        )
        return and_(in_school, teaches_class)
    return false()


def mark_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Marks the caller may read (the outer query joins ``ExamMark``, ``ExamSubject``
    and ``Exam``)."""
    in_school = M.school_id == school_id
    if is_school_wide(principal):
        return in_school
    if _is_faculty(principal):
        return and_(
            in_school,
            faculty_teaches(
                principal.user_id,
                school_id,
                academic_year_id=E.academic_year_id,
                class_id=E.class_id,
                section_id=M.section_id,
                subject_id=C.subject_id,
                on_date=func.coalesce(C.assessment_date, E.start_date),
            ),
        )
    return false()


def sections_in_scope(
    session: Session,
    principal: Principal,
    exam: Exam,
    component: ExamSubject,
    *,
    write: bool,
) -> set[uuid.UUID] | None:
    """Sections whose students' marks for ``component`` the caller may read or write.

    ``None`` means every section of the exam (school-wide callers). Faculty get the
    sections they teach the component's subject in on its assessment date, limited to
    the exam's section when it names one; everyone else gets none.
    """
    if write and not principal.has_permission(P.MARKS_MANAGE):
        return set()
    if is_school_wide(principal):
        return None
    if not _is_faculty(principal):
        return set()
    stmt = select(Section.id).where(
        Section.school_id == exam.school_id,
        Section.class_id == exam.class_id,
        faculty_teaches(
            principal.user_id,
            exam.school_id,
            academic_year_id=exam.academic_year_id,
            class_id=exam.class_id,
            section_id=Section.id,
            subject_id=component.subject_id,
            on_date=assessment_date(exam, component),
        ),
    )
    if exam.section_id is not None:
        stmt = stmt.where(Section.id == exam.section_id)
    return set(session.scalars(stmt))


def report_card_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Report cards the caller may read (the outer query joins ``ExamReportCard`` and
    ``ExamPublication``). Staff see every version; parents and students only the
    active publication of their children / themselves."""
    in_school = ExamReportCard.school_id == school_id
    if is_school_wide(principal):
        return in_school
    personal = personal_student_visibility(principal, school_id)
    if not (
        principal.has_permission(P.PARENT_READ_CHILD)
        or principal.has_permission(P.STUDENT_READ_SELF)
    ):
        return false()
    return and_(
        in_school,
        ExamPublication.status == PublicationStatus.ACTIVE.value,
        ExamReportCard.student_id.in_(select(Student.id).where(personal)),
    )
