"""Real concurrency: separate connections on separate threads against committed data.

The usual ``db_session`` fixture wraps everything in one rolled-back transaction,
which cannot exhibit races. These tests commit real rows (cleaned up afterwards)
and check two kinds of evidence:

* **deterministic interleavings**: one connection holds an uncommitted conflicting
  change while another request runs; we assert the second request *blocks* (it is
  still running after a pause) and fails cleanly once the first commits;
* **stress runs**: many threads released together by a barrier; the final
  database state must satisfy the invariant every time.
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

from app.config import Settings
from app.core.errors import AppError, ConflictError
from app.database import Database
from app.models.academic import AcademicYear
from app.models.school import School
from app.schemas.academic import (
    AcademicYearCreate,
    ClassCreate,
    ClassUpdate,
    SectionCreate,
    SubjectCreate,
)
from app.schemas.school import SchoolCreate
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.academic import (
    AcademicYearService,
    ClassService,
    SectionService,
    SubjectService,
)
from app.services.school import SchoolService
from app.services.users import create_user_account

pytestmark = pytest.mark.db

TIMEOUT = 30


@dataclass
class Env:
    db: Database
    school_id: uuid.UUID
    scope: SchoolScope

    def run(self, fn: Callable[[Any], Any]) -> Any:
        with self.db.session() as session:
            return fn(session)


@pytest.fixture
def env(db_engine: object, test_settings: Settings) -> Iterator[Env]:
    db = Database(test_settings.model_copy(update={"db_pool_size": 12}))
    tag = uuid.uuid4().hex[:8].upper()
    with db.session() as session:
        school = School(name=f"Concurrency {tag}", code=f"CONC-{tag}")
        session.add(school)
        session.flush()
        actor = create_user_account(
            session,
            test_settings,
            full_name="Concurrency Root",
            email=f"conc-{tag.lower()}@x.test",
            password=None,
            roles=[RoleName.SUPER_ADMIN],
            source="test",
        )
        session.commit()
        school_id, actor_id = school.id, actor.id
    principal = Principal(
        user_id=actor_id,
        session_id=uuid.uuid4(),
        school_id=None,
        roles=frozenset({RoleName.SUPER_ADMIN.value}),
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[RoleName.SUPER_ADMIN]),
    )
    try:
        yield Env(db, school_id, SchoolScope(school_id=school_id, principal=principal))
    finally:
        with db.session() as session:
            params = {"s": school_id, "u": actor_id}
            for table in ("sections", "classes", "subjects", "academic_years"):
                session.execute(text(f"DELETE FROM {table} WHERE school_id = :s"), params)  # noqa: S608
            session.execute(
                text(
                    "DELETE FROM audit_logs WHERE user_id = :u OR metadata->>'school_id' = :ss "
                    "OR resource_id IN (:ss, :us)"
                ),
                {**params, "ss": str(school_id), "us": str(actor_id)},
            )
            session.execute(text("DELETE FROM users WHERE id = :u"), params)
            session.execute(text("DELETE FROM schools WHERE id = :s"), params)
            session.commit()
        db.dispose()


def run_together(fns: list[Callable[[], Any]]) -> list[Any]:
    """Start all callables at the same instant; return results or raised exceptions."""
    barrier = threading.Barrier(len(fns))

    def wrapped(fn: Callable[[], Any]) -> Any:
        barrier.wait()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            return exc

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futures = [pool.submit(wrapped, fn) for fn in fns]
        return [f.result(timeout=TIMEOUT) for f in futures]


def assert_blocked(future: Future[Any], seconds: float = 0.7) -> None:
    time.sleep(seconds)
    assert not future.done(), "second writer did not wait for the first transaction"


def _year(env: Env, name: str, start: str, end: str) -> uuid.UUID:
    data = AcademicYearCreate(
        name=name, start_date=date.fromisoformat(start), end_date=date.fromisoformat(end)
    )
    return env.run(lambda s: AcademicYearService(s).create(env.scope, data).id)  # type: ignore[no-any-return]


def _current_count(env: Env) -> int:
    return env.run(  # type: ignore[no-any-return]
        lambda s: s.execute(
            text("SELECT count(*) FROM academic_years WHERE school_id = :s AND is_current"),
            {"s": env.school_id},
        ).scalar_one()
    )


# =========================================================== current academic year
def test_concurrent_set_current_always_leaves_exactly_one(env: Env) -> None:
    years = [_year(env, f"Y{i}", f"20{20 + i}-06-01", f"20{21 + i}-05-31") for i in range(3)]

    def switch(year_id: uuid.UUID) -> Callable[[], Any]:
        return lambda: env.run(lambda s: AcademicYearService(s).set_current(env.scope, year_id).id)

    for _ in range(8):
        results = run_together([switch(y) for y in years])
        assert all(isinstance(r, uuid.UUID) for r in results), results  # all succeed, serialised
        assert _current_count(env) == 1


def test_database_blocks_second_current_year_even_without_app_lock(env: Env) -> None:
    """Bypass the service (and its row lock): the partial unique index still holds."""
    y1 = _year(env, "Y1", "2024-06-01", "2025-05-31")
    y2 = _year(env, "Y2", "2025-06-01", "2026-05-31")
    with env.db.engine.connect() as first, ThreadPoolExecutor(1) as pool:
        first.begin()
        first.execute(text("UPDATE academic_years SET is_current = true WHERE id = :y"), {"y": y1})

        def second() -> None:
            with env.db.engine.begin() as conn:
                conn.execute(
                    text("UPDATE academic_years SET is_current = true WHERE id = :y"), {"y": y2}
                )

        future = pool.submit(second)
        assert_blocked(future)
        first.commit()
        with pytest.raises(IntegrityError) as exc_info:
            future.result(timeout=TIMEOUT)
        assert isinstance(exc_info.value.orig, pg.UniqueViolation)
    assert _current_count(env) == 1


def test_service_set_current_waits_for_school_lock(env: Env) -> None:
    """While another transaction holds the school row, set-current waits, then wins."""
    y1 = _year(env, "Y1", "2024-06-01", "2025-05-31")
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            text("SELECT id FROM schools WHERE id = :s FOR UPDATE"), {"s": env.school_id}
        )
        future = pool.submit(
            lambda: env.run(lambda s: AcademicYearService(s).set_current(env.scope, y1))
        )
        assert_blocked(future)
        holder.commit()
        assert future.result(timeout=TIMEOUT).is_current is True
    assert _current_count(env) == 1


# ===================================================================== overlap rule
def test_concurrent_overlapping_years_only_one_wins(env: Env) -> None:
    def create(i: int) -> Callable[[], Any]:
        data = AcademicYearCreate(
            name=f"Race {i}", start_date=date(2030, 6, 1 + i), end_date=date(2031, 5, 31)
        )
        return lambda: env.run(lambda s: AcademicYearService(s).create(env.scope, data).id)

    results = run_together([create(i) for i in range(6)])
    winners = [r for r in results if isinstance(r, uuid.UUID)]
    losers = [r for r in results if isinstance(r, ConflictError)]
    assert (len(winners), len(losers)) == (1, 5), results
    assert "overlap" in losers[0].message
    count = env.run(
        lambda s: s.query(AcademicYear).filter(AcademicYear.school_id == env.school_id).count()
    )
    assert count == 1


def test_overlap_check_waits_for_uncommitted_conflicting_year(env: Env) -> None:
    with env.db.engine.connect() as first, ThreadPoolExecutor(1) as pool:
        first.begin()
        first.execute(
            text(
                "INSERT INTO academic_years (id, school_id, name, start_date, end_date) "
                "VALUES (gen_random_uuid(), :s, 'Holder', '2040-01-01', '2040-12-31')"
            ),
            {"s": env.school_id},
        )
        data = AcademicYearCreate(
            name="Late", start_date=date(2040, 6, 1), end_date=date(2041, 5, 31)
        )
        future = pool.submit(
            lambda: env.run(lambda s: AcademicYearService(s).create(env.scope, data))
        )
        assert_blocked(future)
        first.commit()
        with pytest.raises(ConflictError):
            future.result(timeout=TIMEOUT)


# ========================================================================= codes
def _expect_single_winner(results: list[Any]) -> None:
    winners = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1, results
    assert all(isinstance(e, ConflictError) for e in errors), errors


def test_concurrent_duplicate_class_codes(env: Env) -> None:
    data = ClassCreate(name="Class 10", code="C10")
    _expect_single_winner(
        run_together([lambda: env.run(lambda s: ClassService(s).create(env.scope, data).id)] * 6)
    )


def test_concurrent_duplicate_subject_codes(env: Env) -> None:
    data = SubjectCreate(name="Mathematics", code="MATH")
    _expect_single_winner(
        run_together([lambda: env.run(lambda s: SubjectService(s).create(env.scope, data).id)] * 6)
    )


def test_concurrent_duplicate_section_codes_in_one_class(env: Env) -> None:
    class_id = env.run(
        lambda s: ClassService(s).create(env.scope, ClassCreate(name="C", code="C1")).id
    )
    data = SectionCreate(class_id=class_id, name="A", code="A")
    _expect_single_winner(
        run_together([lambda: env.run(lambda s: SectionService(s).create(env.scope, data).id)] * 6)
    )


def test_concurrent_duplicate_school_codes(env: Env) -> None:
    tag_code = f"RACE-{uuid.uuid4().hex[:8].upper()}"
    data = SchoolCreate(name="Race School", code=tag_code)

    def create() -> Any:
        def work(s: Any) -> Any:
            school = SchoolService(s).create(data, actor_id=None, source="test")
            s.commit()
            return school.id

        return env.run(work)

    results = run_together([create] * 5)
    try:
        _expect_single_winner(results)
    finally:
        env.run(
            lambda s: (
                s.execute(
                    text(
                        "DELETE FROM audit_logs "
                        "WHERE resource_type = 'school' AND resource_id IN "
                        "(SELECT id::text FROM schools WHERE code = :c)"
                    ),
                    {"c": tag_code},
                ),
                s.execute(text("DELETE FROM schools WHERE code = :c"), {"c": tag_code}),
                s.commit(),
            )
        )


def test_duplicate_code_waits_for_uncommitted_insert(env: Env) -> None:
    with env.db.engine.connect() as first, ThreadPoolExecutor(1) as pool:
        first.begin()
        first.execute(
            text(
                "INSERT INTO classes (id, school_id, name, code) "
                "VALUES (gen_random_uuid(), :s, 'X', 'RACE')"
            ),
            {"s": env.school_id},
        )
        data = ClassCreate(name="Y", code="race")
        future = pool.submit(lambda: env.run(lambda s: ClassService(s).create(env.scope, data)))
        assert_blocked(future)
        first.commit()
        with pytest.raises(ConflictError):
            future.result(timeout=TIMEOUT)


# ============================================== class deactivation vs. new section
def test_class_deactivation_and_section_creation_are_serialised(env: Env) -> None:
    """Invariant: an inactive class never ends up with an active section."""
    for round_no in range(6):
        class_id = env.run(
            lambda s, n=round_no: (
                ClassService(s).create(env.scope, ClassCreate(name=f"R{n}", code=f"R{n}")).id
            )
        )
        deactivate = lambda c=class_id: env.run(  # noqa: E731
            lambda s: ClassService(s).update(env.scope, c, ClassUpdate(is_active=False))
        )
        add_section = lambda c=class_id: env.run(  # noqa: E731
            lambda s: SectionService(s).create(
                env.scope, SectionCreate(class_id=c, name="A", code="A")
            )
        )
        results = run_together([deactivate, add_section])
        # Whichever runs second is refused cleanly (or succeeds if it is still valid).
        for result in results:
            if isinstance(result, Exception):
                assert isinstance(result, AppError), result
                assert result.message in (
                    "Deactivate the class's active sections first.",
                    "Class is inactive.",
                )
        bad = env.run(
            lambda s, c=class_id: s.execute(
                text(
                    "SELECT count(*) FROM sections sec JOIN classes cls ON cls.id = sec.class_id "
                    "WHERE cls.id = :c AND NOT cls.is_active AND sec.is_active"
                ),
                {"c": c},
            ).scalar_one()
        )
        assert bad == 0, f"round {round_no}: {results}"
