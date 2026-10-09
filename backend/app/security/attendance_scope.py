"""Attendance authorization, composed from the existing scopes (no parallel system).

* **School-wide** callers (SUPER_ADMIN, SCHOOL_ADMIN, MANAGEMENT: ``student.read`` per
  :func:`app.security.authorization.resolve_student_scope`) read every session and
  record of the selected school. Those also holding ``attendance.manage``
  (SUPER_ADMIN, SCHOOL_ADMIN) manage them; MANAGEMENT cannot.
* **Faculty** reach attendance only through their *effective* assignments
  (:func:`app.security.faculty_scope.effective_assignments`), and only for sessions
  whose date lies inside the assignment's effective window:

  ===============  ============================================  =====================
  Session          Read                                          Manage (write)
  ===============  ============================================  =====================
  subject session  assignment for the same year + class +        same as read
                   section + **subject**
  daily session    any assignment for the same year + class +    never (school staff
                   section                                       only, see README)
  ===============  ============================================  =====================

  A faculty member sees records only through sessions they can read, never "all
  attendance of an assigned student".
* **Parents / students** see records of linked children / themselves
  (:func:`app.security.people_scope.personal_student_visibility`) in *active*
  sessions only. They never see sessions directly and never write.

Everything is a SQL condition applied inside the query (lists, details, filters and
aggregates alike), so an ID swapped in a URL, body or filter cannot escape it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, exists, false, or_, select
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceRecord, AttendanceSession, SessionStatus
from app.models.faculty import Faculty, FacultyAssignment
from app.models.people import Student
from app.security.authorization import StudentScope, resolve_student_scope
from app.security.faculty_scope import effective_assignments, school_today
from app.security.people_scope import personal_student_visibility
from app.security.permissions import PermissionName
from app.security.principal import Principal

P = PermissionName
S = AttendanceSession
R = AttendanceRecord


def is_school_wide(principal: Principal) -> bool:
    return resolve_student_scope(principal) in (StudentScope.ALL_SCHOOLS, StudentScope.SCHOOL)


def _is_faculty(principal: Principal) -> bool:
    return principal.has_permission(P.FACULTY_READ_ASSIGNED_STUDENTS)


def faculty_teaches(
    user_id: uuid.UUID,
    school_id: uuid.UUID,
    *,
    academic_year_id: Any,
    class_id: Any,
    section_id: Any,
    subject_id: Any | None,
    on_date: Any,
) -> ColumnElement[bool]:
    """An effective assignment of the caller covers this placement on ``on_date``.

    Arguments are columns (correlated with an outer ``AttendanceSession``) or plain
    values. ``subject_id=None`` means "any subject in that section".
    """
    conditions = [
        *effective_assignments(user_id, school_id),
        FacultyAssignment.academic_year_id == academic_year_id,
        FacultyAssignment.class_id == class_id,
        FacultyAssignment.section_id == section_id,
        or_(
            FacultyAssignment.effective_from.is_(None),
            FacultyAssignment.effective_from <= on_date,
        ),
        or_(FacultyAssignment.effective_to.is_(None), FacultyAssignment.effective_to >= on_date),
    ]
    if subject_id is not None:
        conditions.append(FacultyAssignment.subject_id == subject_id)
    return exists(
        select(FacultyAssignment.id)
        .select_from(FacultyAssignment, Faculty)
        .where(*conditions)
        .correlate_except(FacultyAssignment, Faculty)
    )


def _session_teaches(principal: Principal, school_id: uuid.UUID, *, any_subject: bool) -> Any:
    return faculty_teaches(
        principal.user_id,
        school_id,
        academic_year_id=S.academic_year_id,
        class_id=S.class_id,
        section_id=S.section_id,
        subject_id=None if any_subject else S.subject_id,
        on_date=S.session_date,
    )


def session_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Sessions the caller may read."""
    in_school = S.school_id == school_id
    if is_school_wide(principal):
        return in_school
    if _is_faculty(principal):
        return and_(
            in_school,
            or_(
                and_(
                    S.subject_id.is_not(None),
                    _session_teaches(principal, school_id, any_subject=False),
                ),
                and_(
                    S.subject_id.is_(None), _session_teaches(principal, school_id, any_subject=True)
                ),
            ),
        )
    return false()


def session_manageability(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Sessions the caller may change (and record attendance in)."""
    if not principal.has_permission(P.ATTENDANCE_MANAGE):
        return false()
    in_school = S.school_id == school_id
    if is_school_wide(principal):
        return in_school
    if _is_faculty(principal):
        return and_(
            in_school,
            S.subject_id.is_not(None),
            _session_teaches(principal, school_id, any_subject=False),
        )
    return false()


def can_create_session(
    session: Session,
    principal: Principal,
    school_id: uuid.UUID,
    *,
    academic_year_id: uuid.UUID,
    class_id: uuid.UUID,
    section_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    session_date: date,
) -> bool:
    """Same rule as :func:`session_manageability`, for a session not yet stored."""
    if not principal.has_permission(P.ATTENDANCE_MANAGE):
        return False
    if is_school_wide(principal):
        return True
    if not _is_faculty(principal) or subject_id is None:
        return False
    return bool(
        session.scalar(
            select(
                faculty_teaches(
                    principal.user_id,
                    school_id,
                    academic_year_id=academic_year_id,
                    class_id=class_id,
                    section_id=section_id,
                    subject_id=subject_id,
                    on_date=session_date,
                )
            )
        )
    )


def record_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Records the caller may read (the outer query selects ``AttendanceRecord``)."""
    in_school = R.school_id == school_id
    if is_school_wide(principal):
        return in_school
    parts: list[ColumnElement[bool]] = []
    if _is_faculty(principal):
        parts.append(R.session_id.in_(select(S.id).where(session_visibility(principal, school_id))))
    personal = personal_student_visibility(principal, school_id)
    if principal.has_permission(P.PARENT_READ_CHILD) or principal.has_permission(
        P.STUDENT_READ_SELF
    ):
        parts.append(
            and_(
                R.student_id.in_(select(Student.id).where(personal)),
                R.session_id.in_(
                    select(S.id).where(
                        S.school_id == school_id, S.status == SessionStatus.ACTIVE.value
                    )
                ),
            )
        )
    if not parts:
        return false()
    return and_(in_school, or_(*parts))


def today_in_school(session: Session, school_id: uuid.UUID) -> date:
    """Today's date in the school's IANA time zone (PostgreSQL clock, shared with
    :func:`app.security.faculty_scope.school_today`)."""
    return cast(date, session.scalar(select(school_today(school_id))))
