"""Faculty resource scope: what a faculty member may reach, derived from assignments.

A faculty member's scope is **exactly** the set of their *effective* assignments:

* the assignment's ``status`` is ``active``;
* the faculty record is ``active`` (deactivating a faculty member suspends all of
  their assignments without rewriting them);
* today's date in the **school's time zone** lies within ``effective_from`` /
  ``effective_to`` when those are set (both inclusive);
* the assignment, the faculty record and the caller all belong to the school.

From that set:

==============  ===============================================================
Resource        Reachable when ...
==============  ===============================================================
student         the student has an *active* enrollment whose (year, class,
                section) equals an effective assignment's
enrollment      the enrollment itself is *active* and matches an effective
                assignment's (year, class, section); other history rows of the
                same student stay hidden
academic year   some effective assignment is in that year
class           some effective assignment is for that class
section         some effective assignment is for that section
subject         some effective assignment is for that subject
==============  ===============================================================

Access is never widened from "assigned section" to "school": a Mathematics
assignment for 10-A reaches 10-A's students only, not every Mathematics student.

Everything here is expressed as SQL conditions (for lists *and* single-record
reads, so swapping an ID in a URL or filter cannot escape them) plus boolean
helpers built on the same conditions for later phases (attendance, marks, ...).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, Date, and_, cast, exists, false, func, or_, select, true
from sqlalchemy.orm import Session, aliased

from app.core.errors import NotFoundError
from app.models.academic import AcademicYear, SchoolClass, Section, Subject
from app.models.faculty import AssignmentStatus, Faculty, FacultyAssignment, FacultyStatus
from app.models.people import EnrollmentStatus, StudentEnrollment
from app.models.school import School
from app.security.authorization import StudentScope, resolve_student_scope
from app.security.permissions import PermissionName
from app.security.principal import Principal

type StructureModel = type[AcademicYear] | type[SchoolClass] | type[Section] | type[Subject]


def school_today(school_id: uuid.UUID) -> ColumnElement[Any]:
    """Today's date in the school's configured IANA time zone (evaluated by PostgreSQL)."""
    return (
        select(cast(func.timezone(School.timezone, func.now()), Date))
        .where(School.id == school_id)
        .scalar_subquery()
    )


def effective_assignments(user_id: uuid.UUID, school_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """Conditions selecting the caller's effective ``FacultyAssignment`` rows.

    Used inside ``exists()``/``select()`` over :class:`FacultyAssignment`; joins
    :class:`Faculty` through the WHERE clause.
    """
    today = school_today(school_id)
    return [
        FacultyAssignment.school_id == school_id,
        FacultyAssignment.status == AssignmentStatus.ACTIVE.value,
        or_(FacultyAssignment.effective_from.is_(None), FacultyAssignment.effective_from <= today),
        or_(FacultyAssignment.effective_to.is_(None), FacultyAssignment.effective_to >= today),
        Faculty.id == FacultyAssignment.faculty_id,
        Faculty.school_id == school_id,
        Faculty.user_id == user_id,
        Faculty.status == FacultyStatus.ACTIVE.value,
    ]


def _covers(enrollment: Any, user_id: uuid.UUID, school_id: uuid.UUID) -> ColumnElement[bool]:
    """An active enrollment row whose placement matches an effective assignment."""
    return and_(
        enrollment.school_id == school_id,
        enrollment.status == EnrollmentStatus.ACTIVE.value,
        exists(
            select(FacultyAssignment.id)
            .select_from(FacultyAssignment, Faculty)
            .where(
                *effective_assignments(user_id, school_id),
                FacultyAssignment.academic_year_id == enrollment.academic_year_id,
                FacultyAssignment.class_id == enrollment.class_id,
                FacultyAssignment.section_id == enrollment.section_id,
            )
            # Never correlate these with an outer query that happens to select them.
            .correlate_except(FacultyAssignment, Faculty)
        ),
    )


def assigned_student_condition(
    student_id_column: ColumnElement[uuid.UUID] | Any, user_id: uuid.UUID, school_id: uuid.UUID
) -> ColumnElement[bool]:
    """Students (by id column) covered by the faculty member's effective assignments."""
    # Aliased so the subquery never correlates with an enrollment in the outer query.
    enrollment = aliased(StudentEnrollment)
    return exists(
        select(enrollment.id).where(
            enrollment.student_id == student_id_column, _covers(enrollment, user_id, school_id)
        )
    )


def assigned_enrollment_condition(user_id: uuid.UUID, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Enrollment rows (the outer ``StudentEnrollment``) inside the faculty scope."""
    return _covers(StudentEnrollment, user_id, school_id)


_STRUCTURE_COLUMN = {
    AcademicYear: FacultyAssignment.academic_year_id,
    SchoolClass: FacultyAssignment.class_id,
    Section: FacultyAssignment.section_id,
    Subject: FacultyAssignment.subject_id,
}


def is_assignment_scoped(principal: Principal) -> bool:
    """True for callers whose only route to school data is faculty assignments.

    A pure FACULTY user. Staff with ``student.read`` (and SUPER_ADMIN) are school-wide;
    a user who is also a PARENT or STUDENT keeps Phase 3's school-wide structure reads.
    """
    return resolve_student_scope(principal) is StudentScope.ASSIGNED and not (
        principal.has_permission(PermissionName.PARENT_READ_CHILD)
        or principal.has_permission(PermissionName.STUDENT_READ_SELF)
    )


def structure_visibility(
    principal: Principal, school_id: uuid.UUID, model: StructureModel
) -> ColumnElement[bool]:
    """Which academic-structure rows (year/class/section/subject) the caller may read.

    Faculty see only the structure their effective assignments name; everyone else
    keeps Phase 3 behaviour (the whole school's structure via ``academic.read``).
    """
    if not is_assignment_scoped(principal):
        return true()
    column = _STRUCTURE_COLUMN[model]
    return model.id.in_(
        select(column)
        .select_from(FacultyAssignment, Faculty)
        .where(*effective_assignments(principal.user_id, school_id))
        .correlate_except(FacultyAssignment, Faculty)
    )


# ------------------------------------------------------------------ record lookups
def get_current_faculty(session: Session, principal: Principal) -> Faculty | None:
    """The caller's faculty record (None if the caller is not a faculty member)."""
    return session.scalars(
        select(Faculty).where(Faculty.user_id == principal.user_id)
    ).one_or_none()


def _faculty_school(principal: Principal, school_id: uuid.UUID | None) -> uuid.UUID | None:
    if principal.school_id is None or (school_id is not None and school_id != principal.school_id):
        return None
    return principal.school_id


def has_assignment(
    session: Session,
    principal: Principal,
    *,
    school_id: uuid.UUID | None = None,
    academic_year_id: uuid.UUID | None = None,
    class_id: uuid.UUID | None = None,
    section_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
) -> bool:
    """Does the caller hold an effective assignment matching **all** given ids?

    Fails closed for callers without a school, or asking about another school.
    """
    school = _faculty_school(principal, school_id)
    if school is None:
        return False
    filters = [
        column == value
        for column, value in (
            (FacultyAssignment.academic_year_id, academic_year_id),
            (FacultyAssignment.class_id, class_id),
            (FacultyAssignment.section_id, section_id),
            (FacultyAssignment.subject_id, subject_id),
        )
        if value is not None
    ]
    query = (
        select(FacultyAssignment.id)
        .select_from(FacultyAssignment, Faculty)
        .where(*effective_assignments(principal.user_id, school), *filters)
    )
    return bool(session.scalar(select(exists(query))))


def faculty_can_access_class(
    session: Session,
    principal: Principal,
    class_id: uuid.UUID,
    *,
    academic_year_id: uuid.UUID | None = None,
) -> bool:
    return has_assignment(session, principal, class_id=class_id, academic_year_id=academic_year_id)


def faculty_can_access_section(
    session: Session,
    principal: Principal,
    section_id: uuid.UUID,
    *,
    academic_year_id: uuid.UUID | None = None,
) -> bool:
    return has_assignment(
        session, principal, section_id=section_id, academic_year_id=academic_year_id
    )


def faculty_can_access_subject(
    session: Session,
    principal: Principal,
    subject_id: uuid.UUID,
    *,
    section_id: uuid.UUID | None = None,
    academic_year_id: uuid.UUID | None = None,
) -> bool:
    """Subject access is always tied to placements: pass ``section_id`` to ask about
    "this subject *in this section*" (what later phases need for marks/attendance)."""
    return has_assignment(
        session,
        principal,
        subject_id=subject_id,
        section_id=section_id,
        academic_year_id=academic_year_id,
    )


def is_assigned_student(
    session: Session, user_id: uuid.UUID, school_id: uuid.UUID, student_id: uuid.UUID
) -> bool:
    return bool(session.scalar(select(assigned_student_condition(student_id, user_id, school_id))))


def faculty_can_access_student(
    session: Session, principal: Principal, student_id: uuid.UUID
) -> bool:
    if principal.school_id is None:
        return False
    return is_assigned_student(session, principal.user_id, principal.school_id, student_id)


def require_faculty_assignment_access(
    session: Session,
    principal: Principal,
    *,
    school_id: uuid.UUID | None = None,
    academic_year_id: uuid.UUID | None = None,
    class_id: uuid.UUID | None = None,
    section_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
) -> None:
    """Raise 404 unless the caller teaches the given placement (no existence leak).

    The single guard later phases call before faculty-scoped writes, e.g.
    ``require_faculty_assignment_access(s, p, section_id=..., subject_id=...)``.
    """
    if not any((academic_year_id, class_id, section_id, subject_id)):
        raise ValueError("At least one placement id is required.")
    if not has_assignment(
        session,
        principal,
        school_id=school_id,
        academic_year_id=academic_year_id,
        class_id=class_id,
        section_id=section_id,
        subject_id=subject_id,
    ):
        raise NotFoundError()


def faculty_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Which faculty records the caller may read.

    ``faculty.manage`` / ``user.read`` holders see the school's faculty; a FACULTY
    member (``faculty.read_self``) sees only their own record; others see none.
    """
    in_school = Faculty.school_id == school_id
    if (
        principal.is_super_admin
        or principal.has_permission(PermissionName.FACULTY_MANAGE)
        or principal.has_permission(PermissionName.USER_READ)
    ):
        return in_school
    if principal.has_permission(PermissionName.FACULTY_READ_SELF):
        return and_(in_school, Faculty.user_id == principal.user_id)
    return false()


def assignment_visibility(principal: Principal, school_id: uuid.UUID) -> ColumnElement[bool]:
    """Assignments are visible exactly when their faculty record is (incl. history)."""
    return and_(
        FacultyAssignment.school_id == school_id,
        FacultyAssignment.faculty_id.in_(
            select(Faculty.id).where(faculty_visibility(principal, school_id))
        ),
    )
