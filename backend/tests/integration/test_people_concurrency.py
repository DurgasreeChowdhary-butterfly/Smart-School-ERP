"""Real concurrency for students, parents, links and enrollments.

Same method as Phase 3's test_concurrency.py: committed rows, separate
connections on separate threads, deterministic "second writer blocks" proofs and
barrier-released stress runs whose final state must satisfy the invariant.
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
from app.core.errors import ConflictError
from app.database import Database
from app.models.academic import AcademicYear, SchoolClass, Section
from app.models.people import Student
from app.models.school import School
from app.schemas.people import (
    EnrollmentCreate,
    EnrollmentUpdate,
    ParentCreate,
    StudentCreate,
    StudentParentCreate,
)
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.enrollments import EnrollmentService
from app.services.parents import ParentService
from app.services.students import StudentService
from app.services.users import create_user_account

pytestmark = pytest.mark.db
TIMEOUT = 30


@dataclass
class Env:
    db: Database
    settings: Settings
    scope: SchoolScope
    ids: dict[str, uuid.UUID]

    def run(self, fn: Callable[[Session], Any]) -> Any:
        with self.db.session() as session:
            return fn(session)


@pytest.fixture
def env(db_engine: object, test_settings: Settings) -> Iterator[Env]:
    db = Database(test_settings.model_copy(update={"db_pool_size": 12}))
    tag = uuid.uuid4().hex[:8].upper()
    with db.session() as s:
        school = School(name=f"People Race {tag}", code=f"PR-{tag}")
        s.add(school)
        s.flush()
        years = [
            AcademicYear(school_id=school.id, name=n, start_date=a, end_date=b)
            for n, a, b in (
                ("Y1", date(2025, 6, 1), date(2026, 5, 31)),
                ("Y2", date(2026, 6, 1), date(2027, 5, 31)),
                ("Y3", date(2027, 6, 1), date(2028, 5, 31)),
            )
        ]
        cls = SchoolClass(school_id=school.id, name="C", code="C")
        s.add_all([*years, cls])
        s.flush()
        section = Section(school_id=school.id, class_id=cls.id, name="A", code="A")
        student = Student(
            school_id=school.id, admission_number="RACE-1", first_name="R", last_name="One"
        )
        s.add_all([section, student])
        actor = create_user_account(
            s,
            test_settings,
            full_name="Race Root",
            email=f"race-{tag.lower()}@x.test",
            password=None,
            roles=[RoleName.SUPER_ADMIN],
            source="test",
        )
        s.commit()
        ids = {
            "school": school.id,
            "y1": years[0].id,
            "y2": years[1].id,
            "y3": years[2].id,
            "class": cls.id,
            "section": section.id,
            "student": student.id,
            "actor": actor.id,
        }
    principal = Principal(
        user_id=ids["actor"],
        session_id=uuid.uuid4(),
        school_id=None,
        roles=frozenset({RoleName.SUPER_ADMIN.value}),
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[RoleName.SUPER_ADMIN]),
    )
    try:
        yield Env(db, test_settings, SchoolScope(school_id=ids["school"], principal=principal), ids)
    finally:
        with db.session() as s:
            p = {"s": ids["school"], "ss": str(ids["school"]), "u": ids["actor"]}
            s.execute(text("DELETE FROM student_enrollments WHERE school_id = :s"), p)
            s.execute(text("DELETE FROM student_parents WHERE school_id = :s"), p)
            user_ids = (
                s.execute(
                    text(
                        "SELECT user_id FROM parents WHERE school_id = :s UNION "
                        "SELECT user_id FROM students WHERE school_id = :s AND user_id IS NOT NULL"
                    ),
                    p,
                )
                .scalars()
                .all()
            )
            s.execute(text("DELETE FROM parents WHERE school_id = :s"), p)
            s.execute(text("DELETE FROM students WHERE school_id = :s"), p)
            for table in ("sections", "classes", "academic_years"):
                s.execute(text(f"DELETE FROM {table} WHERE school_id = :s"), p)  # noqa: S608
            s.execute(
                text("DELETE FROM audit_logs WHERE user_id = :u OR metadata->>'school_id' = :ss"), p
            )
            for user_id in [*user_ids, ids["actor"]]:
                s.execute(
                    text("DELETE FROM audit_logs WHERE resource_id = :r"), {"r": str(user_id)}
                )
                s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            s.execute(text("DELETE FROM schools WHERE id = :s"), p)
            s.commit()
        db.dispose()


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


def _enroll(env: Env, year: str, *, current: bool) -> Callable[[], Any]:
    data = EnrollmentCreate(
        student_id=env.ids["student"],
        academic_year_id=env.ids[year],
        class_id=env.ids["class"],
        section_id=env.ids["section"],
        enrollment_date=date({"y1": 2025, "y2": 2026, "y3": 2027}[year], 6, 10),
        is_current=current,
    )
    return lambda: env.run(lambda s: EnrollmentService(s).create(env.scope, data).id)


def _current_count(env: Env) -> int:
    return int(
        env.run(
            lambda s: s.execute(
                text(
                    "SELECT count(*) FROM student_enrollments WHERE student_id = :st AND is_current"
                ),
                {"st": env.ids["student"]},
            ).scalar_one()
        )
    )


# ================================================================ current enrollment
def test_concurrent_current_enrollment_creation_leaves_one_current(env: Env) -> None:
    results = run_together([_enroll(env, "y1", current=True), _enroll(env, "y2", current=True)])
    assert all(isinstance(r, uuid.UUID) for r in results), results  # serialised, both succeed
    assert _current_count(env) == 1


def test_concurrent_set_current_patches_always_leave_one(env: Env) -> None:
    """Real contention: one enrollment is current while two threads each try to make a
    *different* one current. Without the student-row lock, both clear the old current
    row, and the second commit hits the one-current index (or deadlocks)."""
    e1 = _enroll(env, "y1", current=True)()
    e2 = _enroll(env, "y2", current=False)()
    e3 = _enroll(env, "y3", current=False)()

    def make_current(enrollment_id: uuid.UUID) -> Callable[[], Any]:
        return lambda: env.run(
            lambda s: (
                EnrollmentService(s)
                .update(env.scope, enrollment_id, EnrollmentUpdate(is_current=True))
                .id
            )
        )

    for _ in range(10):
        make_current(e1)()  # reset: e1 current, e2/e3 not
        results = run_together([make_current(e2), make_current(e3)])
        assert all(isinstance(r, uuid.UUID) for r in results), results
        assert _current_count(env) == 1


def test_database_blocks_second_current_enrollment_without_lock(env: Env) -> None:
    e1 = _enroll(env, "y1", current=False)()
    e2 = _enroll(env, "y2", current=False)()
    with env.db.engine.connect() as first, ThreadPoolExecutor(1) as pool:
        first.begin()
        first.execute(
            text("UPDATE student_enrollments SET is_current = true WHERE id = :e"), {"e": e1}
        )

        def second() -> None:
            with env.db.engine.begin() as conn:
                conn.execute(
                    text("UPDATE student_enrollments SET is_current = true WHERE id = :e"),
                    {"e": e2},
                )

        future = pool.submit(second)
        assert_blocked(future)
        first.commit()
        with pytest.raises(IntegrityError) as exc_info:
            future.result(timeout=TIMEOUT)
        assert isinstance(exc_info.value.orig, pg.UniqueViolation)
    assert _current_count(env) == 1


def test_enrollment_write_waits_for_student_lock(env: Env) -> None:
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            # FOR NO KEY UPDATE conflicts with the service's FOR UPDATE but not with the
            # KEY SHARE lock a foreign-key check takes, so only the explicit lock waits.
            text("SELECT id FROM students WHERE id = :s FOR NO KEY UPDATE"),
            {"s": env.ids["student"]},
        )
        future = pool.submit(_enroll(env, "y2", current=True))
        assert_blocked(future)
        holder.commit()
        assert isinstance(future.result(timeout=TIMEOUT), uuid.UUID)


def test_concurrent_conflicting_enrollments_same_year(env: Env) -> None:
    single_winner(run_together([_enroll(env, "y2", current=False) for _ in range(5)]))


# ======================================================================= duplicates
def test_concurrent_duplicate_students(env: Env) -> None:
    data = StudentCreate(admission_number="DUP-1", first_name="D", last_name="Up")
    single_winner(
        run_together(
            [lambda: env.run(lambda s: StudentService(s, env.settings).create(env.scope, data).id)]
            * 5
        )
    )


def test_concurrent_duplicate_parent_mobile(env: Env) -> None:
    mobile = f"+9197{uuid.uuid4().int % 10**8:08d}"
    data = ParentCreate(first_name="P", last_name="Dup", mobile=mobile)
    single_winner(
        run_together(
            [
                lambda: env.run(
                    lambda s: ParentService(s, env.settings).create(env.scope, data)[0].id
                )
            ]
            * 5
        )
    )


def test_concurrent_duplicate_parent_child_link(env: Env) -> None:
    mobile = f"+9196{uuid.uuid4().int % 10**8:08d}"
    parent_id = env.run(
        lambda s: (
            ParentService(s, env.settings)
            .create(env.scope, ParentCreate(first_name="P", last_name="Link", mobile=mobile))[0]
            .id
        )
    )
    data = StudentParentCreate(parent_id=parent_id, relationship_type="mother")
    single_winner(
        run_together(
            [
                lambda: env.run(
                    lambda s: StudentService(s, env.settings).link_parent(
                        env.scope, env.ids["student"], data
                    )[0]
                )
            ]
            * 5
        )
    )
