"""Assignment and submission authorization, composed from the existing scopes.

* **School-wide** callers (SUPER_ADMIN in the selected school, SCHOOL_ADMIN, MANAGEMENT;
  :func:`app.security.attendance_scope.is_school_wide`) read every assignment and
  submission of the school. Those holding ``assignment.manage`` (SUPER_ADMIN,
  SCHOOL_ADMIN) also manage and review them; MANAGEMENT only reads.
* **Faculty** reach an assignment only through an *effective* Phase 5 assignment for its
  **year + class + section + subject**, effective today and on the assignment's
  ``assigned_on`` date (the same rule as attendance session dates and exam assessment
  dates). That one condition governs reading, managing and reviewing.
* **Students / parents** (``personal_student_visibility``: self / linked children) see
  only *published or closed* assignments of a section the student was enrolled in during
  the assignment window (enrollment start on or before the due date, and no end before
  the assignment date), plus any assignment the student has a submission for. They see
  the student's own submissions. Drafts are never visible to them.

Everything is a SQL condition applied inside the query, so an ID swapped in a URL, body or
filter cannot escape it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, Date, Select, and_, cast, exists, false, or_, select

from app.models.assignments import Assignment, AssignmentLifecycle, AssignmentSubmission
from app.models.people import Student, StudentEnrollment
from app.security.attendance_scope import faculty_teaches, is_school_wide
from app.security.people_scope import personal_student_visibility
from app.security.permissions import PermissionName
from app.security.principal import Principal

P = PermissionName
A = Assignment
SUB = AssignmentSubmission
E = StudentEnrollment
VISIBLE_TO_STUDENTS = (AssignmentLifecycle.PUBLISHED.value, AssignmentLifecycle.CLOSED.value)


def is_faculty(principal: Principal) -> bool:
    return principal.has_permission(P.FACULTY_READ_ASSIGNED_STUDENTS)


def is_personal(principal: Principal) -> bool:
    return principal.has_permission(P.PARENT_READ_CHILD) or principal.has_permission(
        P.STUDENT_READ_SELF
    )


def teaches_assignment(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """The caller's effective assignment covers the outer ``Assignment``'s placement and
    subject today and on its ``assigned_on`` date."""
    return faculty_teaches(
        principal.user_id,
        school_id,
        academic_year_id=A.academic_year_id,
        class_id=A.class_id,
        section_id=A.section_id,
        subject_id=A.subject_id,
        on_date=A.assigned_on,
    )


def enrolled_during_window(students: Select[uuid.UUID]) -> ColumnElement[bool]:
    """One of ``students`` held an enrollment in the outer assignment's year/class/section
    at some point between its assignment date and its due date.

    A single subquery over enrollments, correlated only with the outer ``Assignment``:
    the student set is an uncorrelated ``IN (...)``, so it cannot widen to "any student".
    """
    return exists(
        select(E.id)
        .where(
            E.student_id.in_(students),
            E.school_id == A.school_id,
            E.academic_year_id == A.academic_year_id,
            E.class_id == A.class_id,
            E.section_id == A.section_id,
            E.enrollment_date <= cast(A.due_at, Date),
            or_(E.end_date.is_(None), E.end_date >= A.assigned_on),
        )
        .correlate(A)
    )


def assignment_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Assignments the caller may read (the outer query selects ``Assignment``)."""
    in_school = A.school_id == school_id
    if is_school_wide(principal):
        return in_school
    parts: list[ColumnElement[bool]] = []
    if is_faculty(principal):
        parts.append(teaches_assignment(principal, school_id))
    if is_personal(principal):
        students = select(Student.id).where(personal_student_visibility(principal, school_id))
        parts.append(
            and_(
                A.status.in_(VISIBLE_TO_STUDENTS),
                or_(
                    enrolled_during_window(students),
                    exists(
                        select(SUB.id)
                        .where(SUB.assignment_id == A.id, SUB.student_id.in_(students))
                        .correlate(A)
                    ),
                ),
            )
        )
    if not parts:
        return false()
    return and_(in_school, or_(*parts))


def assignment_staff_reach(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Assignments the caller reaches as school staff or as faculty teaching them, never
    through a parent or student relationship. Governs the roster and submitted text, so
    a faculty member who is also a parent gets no more than a parent for other work."""
    in_school = A.school_id == school_id
    if is_school_wide(principal):
        return in_school
    if is_faculty(principal):
        return and_(in_school, teaches_assignment(principal, school_id))
    return false()


def assignment_manageability(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Assignments the caller may change, publish, close and review submissions of."""
    if not principal.has_permission(P.ASSIGNMENT_MANAGE):
        return false()
    return assignment_staff_reach(principal, school_id)


def submission_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Submissions the caller may read (the outer query joins ``AssignmentSubmission``
    and ``Assignment``)."""
    in_school = SUB.school_id == school_id
    if is_school_wide(principal):
        return in_school
    parts: list[ColumnElement[bool]] = []
    if is_faculty(principal):
        parts.append(teaches_assignment(principal, school_id))
    if is_personal(principal):
        parts.append(
            SUB.student_id.in_(
                select(Student.id).where(personal_student_visibility(principal, school_id))
            )
        )
    if not parts:
        return false()
    return and_(in_school, or_(*parts))


def sees_submission_content(
    principal: Principal, submission_student_user: uuid.UUID | None, *, staff_reach: bool
) -> bool:
    """Callers with staff reach over the assignment (:func:`assignment_staff_reach`) read
    the content; among personal readers only the student themself does (parents see
    status, not the work)."""
    if staff_reach:
        return True
    return submission_student_user is not None and submission_student_user == principal.user_id
