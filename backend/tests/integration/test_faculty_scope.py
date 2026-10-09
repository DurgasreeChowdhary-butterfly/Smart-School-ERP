"""Direct tests of the reusable faculty-scope helpers (what later phases will call)."""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.academic import Section, Subject
from app.models.people import Student
from app.security.authorization import can_access_student, ensure_student_access
from app.security.faculty_scope import (
    faculty_can_access_class,
    faculty_can_access_section,
    faculty_can_access_student,
    faculty_can_access_subject,
    get_current_faculty,
    has_assignment,
    require_faculty_assignment_access,
    school_today,
    structure_visibility,
)
from app.security.people_scope import DbStudentAccessPolicy, student_visibility
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from tests.conftest import SCHOOL_A, SCHOOL_B

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]


def _insert(session: Session, sql: str, **params: object) -> uuid.UUID:
    new_id = uuid.uuid4()
    session.execute(text(sql), {"id": new_id, **params})
    return new_id


def _principal(
    user_id: uuid.UUID, *roles: RoleName, school_id: uuid.UUID | None = SCHOOL_A
) -> Principal:
    return Principal(
        user_id=user_id,
        session_id=uuid.uuid4(),
        school_id=school_id,
        roles=frozenset(role.value for role in roles),
        permissions=frozenset(p.value for role in roles for p in ROLE_PERMISSIONS[role]),
    )


class Ids(dict[str, uuid.UUID]):
    pass


def _school(session: Session, school: uuid.UUID, tag: str) -> Ids:
    ids = Ids()
    for name, start, end in (
        ("y", "2026-06-01", "2027-05-31"),
        ("y_prev", "2025-06-01", "2026-05-31"),
    ):
        ids[name] = _insert(
            session,
            "INSERT INTO academic_years (id, school_id, name, start_date, end_date) "
            "VALUES (:id, :s, :n, :a, :b)",
            s=school,
            n=f"{name}-{tag}",
            a=start,
            b=end,
        )
    for cls in ("c10", "c9"):
        ids[cls] = _insert(
            session,
            "INSERT INTO classes (id, school_id, name, code) VALUES (:id, :s, :n, :c)",
            s=school,
            n=cls,
            c=f"{cls}-{tag}".upper(),
        )
        for sec in ("a", "b"):
            ids[f"{cls}{sec}"] = _insert(
                session,
                "INSERT INTO sections (id, school_id, class_id, name, code) "
                "VALUES (:id, :s, :c, :n, :n)",
                s=school,
                c=ids[cls],
                n=sec.upper(),
            )
    for subject in ("math", "sci"):
        ids[subject] = _insert(
            session,
            "INSERT INTO subjects (id, school_id, name, code) VALUES (:id, :s, :n, :n)",
            s=school,
            n=subject.upper(),
        )
    ids["user"] = _insert(
        session,
        "INSERT INTO users (id, full_name, email, school_id) VALUES (:id, 'F', :e, :s)",
        e=f"{tag}-{uuid.uuid4().hex[:8]}@x.test",
        s=school,
    )
    ids["faculty"] = _insert(
        session,
        "INSERT INTO faculty (id, school_id, user_id, employee_id, first_name, last_name) "
        "VALUES (:id, :s, :u, :e, 'F', 'L')",
        s=school,
        u=ids["user"],
        e=f"EMP-{tag.upper()}",
    )
    return ids


def _assign(
    session: Session, school: uuid.UUID, ids: Ids, cls: str, sec: str, subject: str, year: str = "y"
) -> uuid.UUID:
    return _insert(
        session,
        "INSERT INTO faculty_assignments (id, school_id, faculty_id, academic_year_id, class_id, "
        "section_id, subject_id) VALUES (:id, :s, :f, :y, :c, :sec, :sub)",
        s=school,
        f=ids["faculty"],
        y=ids[year],
        c=ids[cls],
        sec=ids[f"{cls}{sec}"],
        sub=ids[subject],
    )


def _student(
    session: Session, school: uuid.UUID, ids: Ids, cls: str, sec: str, year: str = "y"
) -> uuid.UUID:
    student = _insert(
        session,
        "INSERT INTO students (id, school_id, admission_number, first_name, last_name) "
        "VALUES (:id, :s, :a, 'S', 'T')",
        s=school,
        a=f"ADM-{uuid.uuid4().hex[:8]}".upper(),
    )
    session.execute(
        text(
            "INSERT INTO student_enrollments (id, school_id, student_id, academic_year_id, "
            "class_id, section_id, enrollment_date) "
            "VALUES (gen_random_uuid(), :s, :st, :y, :c, :sec, '2026-06-10')"
        ),
        {"s": school, "st": student, "y": ids[year], "c": ids[cls], "sec": ids[f"{cls}{sec}"]},
    )
    return student


@pytest.fixture
def a(db_session: Session) -> Ids:
    """Faculty F (School A) teaches 10-A Maths, 10-B Maths and 9-A Maths (not 9-B)."""
    ids = _school(db_session, SCHOOL_A, "a")
    for cls, sec in (("c10", "a"), ("c10", "b"), ("c9", "a")):
        ids[f"assign_{cls}{sec}"] = _assign(db_session, SCHOOL_A, ids, cls, sec, "math")
    return ids


@pytest.fixture
def b(db_session: Session) -> Ids:
    ids = _school(db_session, SCHOOL_B, "b")
    ids["assign"] = _assign(db_session, SCHOOL_B, ids, "c10", "a", "math")
    return ids


@pytest.fixture
def f(a: Ids) -> Principal:
    return _principal(a["user"], RoleName.FACULTY)


# ===================================================================== placements
def test_section_scope_follows_every_assignment(db_session: Session, a: Ids, f: Principal) -> None:
    for key in ("c10a", "c10b", "c9a"):
        assert faculty_can_access_section(db_session, f, a[key]), key
    assert not faculty_can_access_section(db_session, f, a["c9b"])
    assert faculty_can_access_section(db_session, f, a["c10a"], academic_year_id=a["y"])
    assert not faculty_can_access_section(db_session, f, a["c10a"], academic_year_id=a["y_prev"])


def test_class_and_subject_scope(db_session: Session, a: Ids, f: Principal) -> None:
    assert faculty_can_access_class(db_session, f, a["c10"])
    assert faculty_can_access_class(db_session, f, a["c9"])
    assert faculty_can_access_subject(db_session, f, a["math"])
    assert faculty_can_access_subject(db_session, f, a["math"], section_id=a["c9a"])
    # Maths is taught, but not in 9-B: subject access is tied to the placement.
    assert not faculty_can_access_subject(db_session, f, a["math"], section_id=a["c9b"])
    assert not faculty_can_access_subject(db_session, f, a["sci"])
    assert not faculty_can_access_subject(db_session, f, a["sci"], section_id=a["c10a"])


def test_require_assignment_access(db_session: Session, a: Ids, f: Principal) -> None:
    require_faculty_assignment_access(db_session, f, section_id=a["c10a"], subject_id=a["math"])
    for kwargs in (
        {"section_id": a["c9b"]},
        {"section_id": a["c10a"], "subject_id": a["sci"]},
        {"class_id": a["c9"], "section_id": a["c10a"]},  # mismatched placement
        {"section_id": a["c10a"], "school_id": SCHOOL_B},
    ):
        with pytest.raises(NotFoundError):
            require_faculty_assignment_access(db_session, f, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="placement"):
        require_faculty_assignment_access(db_session, f)


def test_cross_school_ids_never_match(db_session: Session, a: Ids, b: Ids, f: Principal) -> None:
    assert not faculty_can_access_section(db_session, f, b["c10a"])
    assert not faculty_can_access_subject(db_session, f, b["math"])
    assert not has_assignment(db_session, f, school_id=SCHOOL_B, section_id=a["c10a"])
    # A School B principal holding School A's faculty user id still gets nothing.
    impostor = _principal(a["user"], RoleName.FACULTY, school_id=SCHOOL_B)
    assert not faculty_can_access_section(db_session, impostor, a["c10a"])
    assert not has_assignment(db_session, _principal(a["user"], RoleName.FACULTY, school_id=None))


# ======================================================================== students
def test_student_scope(db_session: Session, a: Ids, b: Ids, f: Principal) -> None:
    inside = _student(db_session, SCHOOL_A, a, "c10", "a")
    other_section = _student(db_session, SCHOOL_A, a, "c9", "b")
    other_year = _student(db_session, SCHOOL_A, a, "c10", "a", year="y_prev")
    other_school = _student(db_session, SCHOOL_B, b, "c10", "a")
    assert faculty_can_access_student(db_session, f, inside)
    for student in (other_section, other_year, other_school):
        assert not faculty_can_access_student(db_session, f, student)
    visible = set(db_session.scalars(select(Student.id).where(student_visibility(f, SCHOOL_A))))
    assert visible == {inside}
    # The Phase 2 hook used by ensure_student_access agrees.
    policy = DbStudentAccessPolicy(db_session)
    assert policy.is_assigned_student(a["user"], inside)
    assert not policy.is_assigned_student(a["user"], other_section)
    assert not policy.is_assigned_student(uuid.uuid4(), inside)  # not a faculty user
    ensure_student_access(f, student_id=inside, student_school_id=SCHOOL_A, policy=policy)
    with pytest.raises(NotFoundError):
        ensure_student_access(
            f, student_id=other_section, student_school_id=SCHOOL_A, policy=policy
        )
    no_school = _principal(a["user"], RoleName.FACULTY, school_id=None)
    assert not faculty_can_access_student(db_session, no_school, inside)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE faculty SET status = 'inactive'",
        "UPDATE faculty_assignments SET status = 'inactive'",
        "UPDATE student_enrollments SET status = 'withdrawn'",
        "UPDATE faculty_assignments SET effective_from = CURRENT_DATE + 2",
        "UPDATE faculty_assignments SET effective_from = CURRENT_DATE - 9, "
        "effective_to = CURRENT_DATE - 2",
    ],
)
def test_lifecycle_revokes_scope(db_session: Session, a: Ids, f: Principal, sql: str) -> None:
    inside = _student(db_session, SCHOOL_A, a, "c10", "a")
    assert faculty_can_access_student(db_session, f, inside)
    db_session.execute(text(sql))
    assert not faculty_can_access_student(db_session, f, inside)
    if "enrollments" not in sql:
        assert not faculty_can_access_section(db_session, f, a["c10a"])


def test_effective_window_is_inclusive(db_session: Session, a: Ids, f: Principal) -> None:
    db_session.execute(
        text(
            "UPDATE faculty_assignments SET effective_from = CURRENT_DATE - 1, "
            "effective_to = CURRENT_DATE + 1"
        )
    )
    assert faculty_can_access_section(db_session, f, a["c10a"])


def test_school_today_uses_school_time_zone(db_session: Session) -> None:
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC"):
        db_session.execute(
            text("UPDATE schools SET timezone = :z WHERE id = :s"), {"z": zone, "s": SCHOOL_A}
        )
        today = db_session.scalar(select(school_today(SCHOOL_A)))
        assert today == datetime.now(ZoneInfo(zone)).date(), zone


# ================================================================ structure / misc
def test_structure_visibility(db_session: Session, a: Ids, f: Principal) -> None:
    sections = set(
        db_session.scalars(select(Section.id).where(structure_visibility(f, SCHOOL_A, Section)))
    )
    assert sections == {a["c10a"], a["c10b"], a["c9a"]}
    subjects = set(
        db_session.scalars(select(Subject.id).where(structure_visibility(f, SCHOOL_A, Subject)))
    )
    assert subjects == {a["math"]}
    admin = _principal(uuid.uuid4(), RoleName.SCHOOL_ADMIN)
    assert (
        len(
            db_session.scalars(
                select(Section.id).where(
                    Section.school_id == SCHOOL_A, structure_visibility(admin, SCHOOL_A, Section)
                )
            ).all()
        )
        == 4
    )


def test_faculty_who_is_also_a_parent_gets_the_union(db_session: Session, a: Ids) -> None:
    """Combined personas: assigned students *and* own children; school-wide structure."""
    taught = _student(db_session, SCHOOL_A, a, "c10", "a")
    child = _student(db_session, SCHOOL_A, a, "c9", "b")
    parent = _insert(
        db_session,
        "INSERT INTO parents (id, school_id, user_id, first_name, last_name) "
        "VALUES (:id, :s, :u, 'P', 'Q')",
        s=SCHOOL_A,
        u=a["user"],
    )
    db_session.execute(
        text(
            "INSERT INTO student_parents (student_id, parent_id, school_id, relationship_type) "
            "VALUES (:st, :p, :s, 'father')"
        ),
        {"st": child, "p": parent, "s": SCHOOL_A},
    )
    both = _principal(a["user"], RoleName.FACULTY, RoleName.PARENT)
    visible = set(db_session.scalars(select(Student.id).where(student_visibility(both, SCHOOL_A))))
    assert visible == {taught, child}
    policy = DbStudentAccessPolicy(db_session)
    for student in (taught, child):
        assert can_access_student(
            both, student_id=student, student_school_id=SCHOOL_A, policy=policy
        )
    stranger = _student(db_session, SCHOOL_A, a, "c9", "a", year="y_prev")
    assert not can_access_student(
        both, student_id=stranger, student_school_id=SCHOOL_A, policy=policy
    )
    assert (
        len(
            db_session.scalars(
                select(Section.id).where(
                    Section.school_id == SCHOOL_A, structure_visibility(both, SCHOOL_A, Section)
                )
            ).all()
        )
        == 4
    )


def test_get_current_faculty(db_session: Session, a: Ids, f: Principal) -> None:
    faculty = get_current_faculty(db_session, f)
    assert faculty is not None
    assert faculty.id == a["faculty"]
    assert get_current_faculty(db_session, _principal(uuid.uuid4(), RoleName.FACULTY)) is None
