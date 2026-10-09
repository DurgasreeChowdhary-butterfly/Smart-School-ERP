"""Real concurrency for faculty and faculty assignments.

Same method as test_people_concurrency.py: committed rows, separate connections on
separate threads (no mocks), deterministic "second writer blocks" proofs, and
barrier-released races whose final state must satisfy the invariant.
"""

from __future__ import annotations

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
from app.models.school import School
from app.schemas.faculty import FacultyAssignmentCreate, FacultyAssignmentUpdate, FacultyCreate
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.faculty import FacultyAssignmentService, FacultyService
from app.services.users import create_user_account

pytestmark = pytest.mark.db
TIMEOUT = 30
PASSWORD = "Concurrent-Teacher-77"


@dataclass
class Env:
    db: Database
    settings: Settings
    scope: SchoolScope
    ids: dict[str, uuid.UUID]

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
        school = School(name=f"Faculty Race {tag}", code=f"FR-{tag}")
        s.add(school)
        s.flush()
        year = AcademicYear(
            school_id=school.id, name="Y", start_date=date(2026, 6, 1), end_date=date(2027, 5, 31)
        )
        cls = SchoolClass(school_id=school.id, name="C", code="C")
        subjects = [Subject(school_id=school.id, name=f"S{i}", code=f"S{i}") for i in range(5)]
        s.add_all([year, cls, *subjects])
        s.flush()
        section = Section(school_id=school.id, class_id=cls.id, name="A", code="A")
        s.add(section)
        actor = create_user_account(
            s,
            test_settings,
            full_name="Race Root",
            email=f"frace-{tag.lower()}@x.test",
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
            "actor": actor.id,
            **{f"subject{i}": subject.id for i, subject in enumerate(subjects)},
        }
    principal = Principal(
        user_id=ids["actor"],
        session_id=uuid.uuid4(),
        school_id=None,
        roles=frozenset({RoleName.SUPER_ADMIN.value}),
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[RoleName.SUPER_ADMIN]),
    )
    env = Env(db, test_settings, SchoolScope(school_id=ids["school"], principal=principal), ids)
    env.ids["faculty"] = _create_faculty(env, "EMP-BASE", f"base-{tag.lower()}@x.test")()
    try:
        yield env
    finally:
        with db.session() as s:
            p = {"s": ids["school"], "ss": str(ids["school"]), "u": ids["actor"]}
            s.execute(text("DELETE FROM faculty_assignments WHERE school_id = :s"), p)
            user_ids = (
                s.execute(text("SELECT id FROM users WHERE school_id = :s"), p).scalars().all()
            )
            s.execute(text("DELETE FROM faculty WHERE school_id = :s"), p)
            for table in ("sections", "subjects", "classes", "academic_years"):
                s.execute(text(f"DELETE FROM {table} WHERE school_id = :s"), p)  # noqa: S608
            s.execute(
                text("DELETE FROM audit_logs WHERE user_id = :u OR metadata->>'school_id' = :ss"), p
            )
            for user_id in [*user_ids, ids["actor"]]:
                s.execute(
                    text(
                        "DELETE FROM audit_logs "
                        "WHERE resource_id = :r OR user_id = CAST(:r AS uuid)"
                    ),
                    {"r": str(user_id)},
                )
                s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            s.execute(text("DELETE FROM schools WHERE id = :s"), p)
            s.commit()
        db.dispose()


def _create_faculty(env: Env, employee_id: str, email: str) -> Callable[[], uuid.UUID]:
    data = FacultyCreate(
        employee_id=employee_id, first_name="T", last_name="R", email=email, password=PASSWORD
    )
    return lambda: env.run(lambda s: FacultyService(s, env.settings).create(env.scope, data)[0].id)


def _assign(env: Env, subject: str = "subject0") -> Callable[[], uuid.UUID]:
    data = FacultyAssignmentCreate(
        faculty_id=env.ids["faculty"],
        academic_year_id=env.ids["year"],
        class_id=env.ids["class"],
        section_id=env.ids["section"],
        subject_id=env.ids[subject],
    )
    return lambda: env.run(lambda s: FacultyAssignmentService(s).create(env.scope, data).id)


def _patch(env: Env, assignment_id: uuid.UUID, **changes: Any) -> Callable[[], Any]:
    data = FacultyAssignmentUpdate(**changes)
    return lambda: env.run(
        lambda s: FacultyAssignmentService(s).update(env.scope, assignment_id, data).status
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


def _active_count(env: Env) -> int:
    return int(
        env.scalar(
            "SELECT count(*) FROM faculty_assignments WHERE faculty_id = :f AND status = 'active'",
            f=env.ids["faculty"],
        )
    )


# ========================================================================== faculty
def test_concurrent_duplicate_employee_id(env: Env) -> None:
    """Five requests, one employee ID, different emails: one faculty, no orphan logins."""
    single_winner(
        run_together(
            [
                _create_faculty(env, "EMP-DUP", f"dup{i}-{uuid.uuid4().hex[:6]}@x.test")
                for i in range(5)
            ]
        )
    )
    assert (
        env.scalar(
            "SELECT count(*) FROM faculty WHERE school_id = :s AND employee_id = 'EMP-DUP'",
            s=env.ids["school"],
        )
        == 1
    )
    # Every faculty user has a faculty record: the losers' users were rolled back.
    assert (
        env.scalar(
            "SELECT count(*) FROM users u WHERE u.school_id = :s AND NOT EXISTS "
            "(SELECT 1 FROM faculty f WHERE f.user_id = u.id)",
            s=env.ids["school"],
        )
        == 0
    )


def test_concurrent_duplicate_email(env: Env) -> None:
    email = f"same-{uuid.uuid4().hex[:6]}@x.test"
    single_winner(run_together([_create_faculty(env, f"EMP-E{i}", email) for i in range(5)]))
    assert env.scalar("SELECT count(*) FROM users WHERE email = :e", e=email) == 1
    assert (
        env.scalar(
            "SELECT count(*) FROM faculty WHERE employee_id LIKE 'EMP-E%' AND school_id = :s",
            s=env.ids["school"],
        )
        == 1
    )


# ====================================================================== assignments
def test_concurrent_duplicate_assignment(env: Env) -> None:
    single_winner(run_together([_assign(env) for _ in range(6)]))
    assert _active_count(env) == 1


def test_competing_distinct_assignments_all_succeed(env: Env) -> None:
    """Different subjects for one faculty member queue on the faculty lock; none is lost."""
    results = run_together([_assign(env, f"subject{i}") for i in range(5)])
    assert all(isinstance(r, uuid.UUID) for r in results), results
    assert _active_count(env) == 5


def test_competing_reactivation_of_history_rows(env: Env) -> None:
    """Two inactive history rows of the same placement reactivated at once: one wins."""
    first = _assign(env)()
    _patch(env, first, status="inactive")()
    second = _assign(env)()
    _patch(env, second, status="inactive")()
    results = run_together(
        [_patch(env, first, status="active"), _patch(env, second, status="active")]
    )
    single_winner(results)
    assert _active_count(env) == 1


def test_concurrent_deactivation_and_update_serialise(env: Env) -> None:
    item = _assign(env)()
    for _ in range(5):
        _patch(env, item, status="active", effective_to=None)()
        results = run_together(
            [
                _patch(env, item, status="inactive"),
                _patch(env, item, effective_to=date(2027, 3, 31)),
            ]
        )
        assert all(isinstance(r, str) for r in results), results
        status, effective_to = env.run(
            lambda s: s.execute(
                text("SELECT status, effective_to FROM faculty_assignments WHERE id = :i"),
                {"i": item},
            ).one()
        )
        # Neither write was lost.
        assert (status, effective_to) == ("inactive", date(2027, 3, 31))


def test_assignment_waits_for_faculty_lock(env: Env) -> None:
    """Assignment writes lock the faculty row, so they cannot interleave with a faculty
    status change: deactivate-then-assign yields a clean rejection, never a new
    assignment for an inactive faculty member."""
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            # FOR NO KEY UPDATE conflicts with the service's FOR UPDATE but not with the
            # KEY SHARE lock a foreign-key check takes, so only the explicit lock waits.
            text("UPDATE faculty SET status = 'inactive' WHERE id = :f"),
            {"f": env.ids["faculty"]},
        )
        future = pool.submit(_assign(env))
        assert_blocked(future)
        holder.commit()
        with pytest.raises(BusinessRuleError):
            future.result(timeout=TIMEOUT)
    assert _active_count(env) == 0


def test_database_blocks_duplicate_without_service(env: Env) -> None:
    sql = text(
        "INSERT INTO faculty_assignments (id, school_id, faculty_id, academic_year_id, class_id, "
        "section_id, subject_id) VALUES (gen_random_uuid(), :s, :f, :y, :c, :sec, :sub)"
    )
    params = {
        "s": env.ids["school"],
        "f": env.ids["faculty"],
        "y": env.ids["year"],
        "c": env.ids["class"],
        "sec": env.ids["section"],
        "sub": env.ids["subject0"],
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
    assert _active_count(env) == 1
