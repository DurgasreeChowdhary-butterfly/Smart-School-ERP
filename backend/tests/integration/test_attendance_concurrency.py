"""Real concurrency for attendance.

Same method as test_faculty_concurrency.py: committed rows, separate connections on
separate threads (no mocks), deterministic "second writer blocks" proofs, and
barrier-released races whose final state must satisfy the invariant.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.errors import BusinessRuleError, ConflictError
from app.database import Database
from app.models.academic import AcademicYear, SchoolClass, Section, Subject
from app.models.people import Student, StudentEnrollment
from app.models.school import School
from app.schemas.attendance import (
    AttendanceBulkCreate,
    AttendanceRecordCreate,
    AttendanceRecordUpdate,
    AttendanceSessionCreate,
    AttendanceSessionUpdate,
)
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.attendance import AttendanceRecordService, AttendanceSessionService
from app.services.users import create_user_account

pytestmark = pytest.mark.db
TIMEOUT = 30
STUDENTS = 6
SESSION_DAY = date(2026, 7, 10)


@dataclass
class Env:
    db: Database
    scope: SchoolScope
    ids: dict[str, uuid.UUID]
    students: list[uuid.UUID]

    def run(self, fn: Callable[[Session], Any]) -> Any:
        with self.db.session() as session:
            return fn(session)

    def scalar(self, sql: str, **params: object) -> Any:
        return self.run(lambda s: s.execute(text(sql), params).scalar_one())


@pytest.fixture
def env(db_engine: object, test_settings: Settings) -> Iterator[Env]:
    db = Database(test_settings.model_copy(update={"db_pool_size": 12}))
    tag = uuid.uuid4().hex[:8].upper()
    with db.session() as s:
        school = School(name=f"Attendance Race {tag}", code=f"AR-{tag}")
        s.add(school)
        s.flush()
        year = AcademicYear(
            school_id=school.id, name="Y", start_date=date(2026, 6, 1), end_date=date(2027, 5, 31)
        )
        cls = SchoolClass(school_id=school.id, name="C", code="C")
        subject = Subject(school_id=school.id, name="M", code="M")
        s.add_all([year, cls, subject])
        s.flush()
        section = Section(school_id=school.id, class_id=cls.id, name="A", code="A")
        students = [
            Student(school_id=school.id, admission_number=f"R{i}", first_name="S", last_name=f"{i}")
            for i in range(STUDENTS)
        ]
        s.add_all([section, *students])
        s.flush()
        s.add_all(
            StudentEnrollment(
                school_id=school.id,
                student_id=student.id,
                academic_year_id=year.id,
                class_id=cls.id,
                section_id=section.id,
                enrollment_date=date(2026, 6, 10),
            )
            for student in students
        )
        actor = create_user_account(
            s,
            test_settings,
            full_name="Race Root",
            email=f"arace-{tag.lower()}@x.test",
            password=None,
            roles=[RoleName.SUPER_ADMIN],
            source="test",
        )
        s.commit()
        ids = {
            "school": school.id,
            "year": year.id,
            "class": cls.id,
            "section": section.id,
            "subject": subject.id,
            "actor": actor.id,
        }
        student_ids = [student.id for student in students]
    principal = Principal(
        user_id=ids["actor"],
        session_id=uuid.uuid4(),
        school_id=None,
        roles=frozenset({RoleName.SUPER_ADMIN.value}),
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[RoleName.SUPER_ADMIN]),
    )
    env = Env(db, SchoolScope(school_id=ids["school"], principal=principal), ids, student_ids)
    env.ids["session"] = _create_session(env)()
    try:
        yield env
    finally:
        with db.session() as s:
            p = {"s": ids["school"], "ss": str(ids["school"]), "u": ids["actor"]}
            s.execute(text("SET LOCAL app.attendance_history_purge = 'on'"))
            for table in (
                "attendance_record_changes",
                "attendance_records",
                "attendance_sessions",
                "student_enrollments",
                "students",
                "sections",
                "subjects",
                "classes",
                "academic_years",
            ):
                s.execute(text(f"DELETE FROM {table} WHERE school_id = :s"), p)  # noqa: S608
            s.execute(
                text("DELETE FROM audit_logs WHERE user_id = :u OR metadata->>'school_id' = :ss"), p
            )
            s.execute(text("DELETE FROM users WHERE id = :u"), p)
            s.execute(text("DELETE FROM schools WHERE id = :s"), p)
            s.commit()
        db.dispose()


def _create_session(env: Env, period: int = 1) -> Callable[[], uuid.UUID]:
    data = AttendanceSessionCreate(
        academic_year_id=env.ids["year"],
        class_id=env.ids["class"],
        section_id=env.ids["section"],
        subject_id=env.ids["subject"],
        session_date=SESSION_DAY,
        period=period,
    )
    return lambda: env.run(lambda s: AttendanceSessionService(s).create(env.scope, data).id)


def _bulk(env: Env, students: list[uuid.UUID], status: str = "present") -> Callable[[], int]:
    data = AttendanceBulkCreate(
        records=[AttendanceRecordCreate(student_id=sid, status=status) for sid in students]  # type: ignore[arg-type]
    )
    return lambda: env.run(
        lambda s: len(
            AttendanceRecordService(s).bulk_create(env.scope, env.ids["session"], data)[0]
        )
    )


def _patch(env: Env, record_id: uuid.UUID, **changes: Any) -> Callable[[], str]:
    data = AttendanceRecordUpdate(**changes)
    return lambda: env.run(
        lambda s: AttendanceRecordService(s).update(env.scope, record_id, data)[0].status
    )


def _deactivate(env: Env) -> Callable[[], str]:
    data = AttendanceSessionUpdate(status="inactive")  # type: ignore[arg-type]
    return lambda: env.run(
        lambda s: AttendanceSessionService(s).update(env.scope, env.ids["session"], data).status
    )


def run_together(fns: list[Callable[[], Any]]) -> list[Any]:
    barrier = threading.Barrier(len(fns))

    def wrapped(fn: Callable[[], Any]) -> Any:
        barrier.wait()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            return exc

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return [f.result(timeout=TIMEOUT) for f in [pool.submit(wrapped, fn) for fn in fns]]


def assert_blocked(future: Future[Any], seconds: float = 0.7) -> None:
    time.sleep(seconds)
    assert not future.done(), "second writer did not wait for the first transaction"


def single_winner(results: list[Any]) -> None:
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1, results
    assert all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception)), results


def _records(env: Env) -> int:
    return int(
        env.scalar(
            "SELECT count(*) FROM attendance_records WHERE session_id = :s", s=env.ids["session"]
        )
    )


# ========================================================================== sessions
def test_concurrent_duplicate_session_creation(env: Env) -> None:
    single_winner(run_together([_create_session(env, period=2) for _ in range(6)]))
    assert (
        env.scalar(
            "SELECT count(*) FROM attendance_sessions WHERE school_id = :s AND period = 2",
            s=env.ids["school"],
        )
        == 1
    )


def test_concurrent_distinct_sessions_all_succeed(env: Env) -> None:
    results = run_together([_create_session(env, period=p) for p in range(2, 7)])
    assert all(isinstance(r, uuid.UUID) for r in results), results


# ============================================================================= bulk
def test_concurrent_bulk_for_the_same_students(env: Env) -> None:
    results = run_together([_bulk(env, env.students, s) for s in ("present", "absent", "late")])
    single_winner(results)
    assert _records(env) == STUDENTS
    winner = next(r for r in results if not isinstance(r, Exception))
    assert winner == STUDENTS


def test_concurrent_disjoint_bulks_all_succeed(env: Env) -> None:
    groups = [env.students[i::3] for i in range(3)]
    results = run_together([_bulk(env, group) for group in groups])
    assert results == [len(group) for group in groups], results
    assert _records(env) == STUDENTS


def test_overlapping_bulks_never_half_write(env: Env) -> None:
    """Two requests share one student: exactly one request is written, entirely."""
    first, second = env.students[:4], env.students[3:]
    results = run_together([_bulk(env, first), _bulk(env, second)])
    single_winner(results)
    assert _records(env) in (len(first), len(second))


def test_bulk_waits_for_the_session_lock(env: Env) -> None:
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            text("SELECT id FROM attendance_sessions WHERE id = :s FOR UPDATE"),
            {"s": env.ids["session"]},
        )
        future = pool.submit(_bulk(env, env.students))
        assert_blocked(future)
        holder.commit()
        assert future.result(timeout=TIMEOUT) == STUDENTS


# ====================================================================== corrections
def test_concurrent_corrections_keep_a_consistent_history(env: Env) -> None:
    _bulk(env, env.students[:1], "present")()
    record_id = env.scalar(
        "SELECT id FROM attendance_records WHERE session_id = :s", s=env.ids["session"]
    )
    statuses = ["absent", "late", "excused", "present", "absent"]
    results = run_together(
        [
            _patch(env, record_id, status=status, remarks=f"change {i}", reason=f"fix {i}")
            for i, status in enumerate(statuses)
        ]
    )
    assert not [r for r in results if isinstance(r, Exception)], results
    rows = env.run(
        lambda s: s.execute(
            text(
                "SELECT previous_status, new_status, previous_remarks, new_remarks "
                "FROM attendance_record_changes WHERE record_id = :r ORDER BY sequence"
            ),
            {"r": record_id},
        ).all()
    )
    assert len(rows) == len(statuses)  # distinct remarks: no change was a no-op or lost
    assert rows[0][0] == "present"  # the first change starts from the original status
    for before, after in itertools.pairwise(rows):
        assert after[0] == before[1]  # each change starts where the previous one ended
        assert after[2] == before[3]
    final = env.run(
        lambda s: s.execute(
            text("SELECT status, original_status, remarks FROM attendance_records WHERE id = :r"),
            {"r": record_id},
        ).one()
    )
    assert tuple(final) == (rows[-1][1], "present", rows[-1][3])


def test_correction_waits_for_a_concurrent_writer(env: Env) -> None:
    _bulk(env, env.students[:1], "present")()
    record_id = env.scalar(
        "SELECT id FROM attendance_records WHERE session_id = :s", s=env.ids["session"]
    )
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            text("SELECT id FROM attendance_records WHERE id = :r FOR UPDATE"), {"r": record_id}
        )
        future = pool.submit(_patch(env, record_id, status="absent", reason="Register error"))
        assert_blocked(future)
        holder.commit()
        assert future.result(timeout=TIMEOUT) == "absent"


def test_deactivation_racing_bulk_is_serialised(env: Env) -> None:
    results = run_together([_deactivate(env), _bulk(env, env.students)])
    assert results[0] == "inactive", results
    if isinstance(results[1], Exception):
        assert isinstance(results[1], BusinessRuleError), results
        assert _records(env) == 0  # deactivated first: nothing written
    else:
        assert _records(env) == STUDENTS  # marked first, then deactivated


# ========================================================================== database
def test_database_blocks_duplicate_record_without_service(env: Env) -> None:
    student = env.students[0]
    enrollment = env.scalar("SELECT id FROM student_enrollments WHERE student_id = :s", s=student)
    sql = text(
        "INSERT INTO attendance_records (id, school_id, session_id, enrollment_id, student_id, "
        "academic_year_id, class_id, section_id, status, original_status, marked_by_user_id, "
        "marked_at) VALUES (gen_random_uuid(), :school, :session, :enrollment, :student, :y, "
        ":c, :sec, 'present', 'present', :u, now())"
    )
    params = {
        "school": env.ids["school"],
        "session": env.ids["session"],
        "enrollment": enrollment,
        "student": student,
        "y": env.ids["year"],
        "c": env.ids["class"],
        "sec": env.ids["section"],
        "u": env.ids["actor"],
    }
    with env.db.engine.connect() as first, ThreadPoolExecutor(1) as pool:
        first.begin()
        first.execute(sql, params)

        def second() -> None:
            with env.db.engine.begin() as conn:
                conn.execute(sql, params)

        future = pool.submit(second)
        assert_blocked(future)
        first.commit()
        with pytest.raises(IntegrityError) as exc_info:
            future.result(timeout=TIMEOUT)
        assert isinstance(exc_info.value.orig, pg.UniqueViolation)
    assert _records(env) == 1
