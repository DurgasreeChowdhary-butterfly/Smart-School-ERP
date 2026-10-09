"""Real concurrency for exams, marks and publication.

Same method as test_attendance_concurrency.py: committed rows, separate connections on
separate threads (no mocks), deterministic "second writer blocks" proofs, and
barrier-released races whose final state must satisfy the invariant.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.errors import ConflictError
from app.database import Database
from app.models.academic import AcademicYear, SchoolClass, Section, Subject
from app.models.people import Student, StudentEnrollment
from app.models.school import School
from app.schemas.exams import (
    ExamCreate,
    ExamSubjectCreate,
    MarkBulkCreate,
    MarkCorrection,
    MarkEntry,
)
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.exam_marks import ExamMarkService
from app.services.exam_results import ExamResultService
from app.services.exams import ExamService, ExamSubjectService
from app.services.users import create_user_account
from tests.integration.test_attendance_concurrency import (
    TIMEOUT,
    assert_blocked,
    run_together,
    single_winner,
)

pytestmark = pytest.mark.db
STUDENTS = 6
TABLES = (
    "exam_report_cards",
    "exam_publications",
    "exam_mark_changes",
    "exam_marks",
    "exam_subjects",
    "exams",
    "student_enrollments",
    "students",
    "sections",
    "subjects",
    "classes",
    "academic_years",
)


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

    # ------------------------------------------------------------ operations
    def create_exam(self, name: str = "Midterm") -> Callable[[], uuid.UUID]:
        data = ExamCreate(
            academic_year_id=self.ids["year"],
            class_id=self.ids["class"],
            name=name,
            exam_type="midterm",  # type: ignore[arg-type]
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 10),
        )
        return lambda: self.run(lambda s: ExamService(s).create(self.scope, data).id)

    def add_component(self, exam: uuid.UUID, subject: str = "math") -> Callable[[], uuid.UUID]:
        data = ExamSubjectCreate(subject_id=self.ids[subject], max_marks=Decimal(100))
        return lambda: self.run(lambda s: ExamSubjectService(s).create(self.scope, exam, data).id)

    def transition(self, exam: uuid.UUID, verb: str) -> Callable[[], str]:
        def call(s: Session) -> str:
            service = ExamService(s)
            if verb == "cancel":
                return service.cancel(self.scope, exam, "Called off").status
            return str(getattr(service, verb)(self.scope, exam).status)

        return lambda: self.run(call)

    def publish(self, exam: uuid.UUID) -> Callable[[], int]:
        return lambda: self.run(
            lambda s: ExamResultService(s).publish(self.scope, exam, allow_incomplete=True).version
        )

    def reopen(self, exam: uuid.UUID) -> Callable[[], int]:
        return lambda: self.run(
            lambda s: ExamResultService(s).reopen(self.scope, exam, "Recheck").version
        )

    def bulk(
        self, exam: uuid.UUID, component: uuid.UUID, students: list[uuid.UUID], marks: int = 50
    ) -> Callable[[], int]:
        data = MarkBulkCreate(
            marks=[MarkEntry(student_id=sid, marks_obtained=Decimal(marks)) for sid in students]
        )
        return lambda: self.run(
            lambda s: len(ExamMarkService(s).bulk_create(self.scope, exam, component, data))
        )

    def correct(self, mark: uuid.UUID, marks: int, reason: str = "Fix") -> Callable[[], Decimal]:
        data = MarkCorrection(marks_obtained=Decimal(marks), reason=reason)
        return lambda: self.run(
            lambda s: ExamMarkService(s).correct(self.scope, mark, data).marks_obtained
        )

    def open_exam(self, name: str = "Midterm") -> tuple[uuid.UUID, uuid.UUID]:
        exam = self.create_exam(name)()
        component = self.add_component(exam)()
        self.transition(exam, "schedule")()
        self.transition(exam, "open_marks_entry")()
        return exam, component


@pytest.fixture
def env(db_engine: object, test_settings: Settings) -> Iterator[Env]:
    db = Database(test_settings.model_copy(update={"db_pool_size": 12}))
    tag = uuid.uuid4().hex[:8].upper()
    with db.session() as s:
        school = School(name=f"Exam Race {tag}", code=f"ER-{tag}")
        s.add(school)
        s.flush()
        year = AcademicYear(
            school_id=school.id, name="Y", start_date=date(2026, 6, 1), end_date=date(2027, 5, 31)
        )
        cls = SchoolClass(school_id=school.id, name="C", code="C")
        math = Subject(school_id=school.id, name="Math", code="M")
        sci = Subject(school_id=school.id, name="Science", code="S")
        s.add_all([year, cls, math, sci])
        s.flush()
        section = Section(school_id=school.id, class_id=cls.id, name="A", code="A")
        students = [
            Student(school_id=school.id, admission_number=f"X{i}", first_name="S", last_name=f"{i}")
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
            full_name="Exam Race Root",
            email=f"erace-{tag.lower()}@x.test",
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
            "math": math.id,
            "sci": sci.id,
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
    try:
        yield Env(db, SchoolScope(school_id=ids["school"], principal=principal), ids, student_ids)
    finally:
        with db.session() as s:
            p = {"s": ids["school"], "ss": str(ids["school"]), "u": ids["actor"]}
            s.execute(text("SET LOCAL app.exam_history_purge = 'on'"))
            for table in TABLES:
                s.execute(text(f"DELETE FROM {table} WHERE school_id = :s"), p)  # noqa: S608
            s.execute(
                text(
                    "DELETE FROM audit_logs WHERE user_id = :u OR resource_id = CAST(:u AS text) "
                    "OR metadata->>'school_id' = :ss"
                ),
                p,
            )
            s.execute(text("DELETE FROM users WHERE id = :u"), p)
            s.execute(text("DELETE FROM schools WHERE id = :s"), p)
            s.commit()
        db.dispose()


def _count(env: Env, table: str, **where: uuid.UUID) -> int:
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "TRUE"
    return int(env.scalar(f"SELECT count(*) FROM {table} WHERE {clause}", **where))  # noqa: S608


# ===================================================================== configuration
def test_concurrent_duplicate_exam_creation(env: Env) -> None:
    single_winner(run_together([env.create_exam() for _ in range(6)]))
    assert _count(env, "exams", school_id=env.ids["school"]) == 1


def test_concurrent_component_configuration(env: Env) -> None:
    exam = env.create_exam()()
    single_winner(run_together([env.add_component(exam) for _ in range(5)]))
    assert _count(env, "exam_subjects", exam_id=exam) == 1
    other = env.create_exam("Final")()
    results = run_together([env.add_component(other, "math"), env.add_component(other, "sci")])
    assert all(isinstance(r, uuid.UUID) for r in results), results


def test_component_racing_the_end_of_configuration(env: Env) -> None:
    """Adding a subject while marks entry opens: either the subject made it in first, or
    it is refused. Never a subject added to an exam that already accepts marks."""
    for round_ in range(4):
        exam = env.create_exam(f"Race {round_}")()
        env.add_component(exam, "math")()
        env.transition(exam, "schedule")()
        add, opened = run_together(
            [env.add_component(exam, "sci"), env.transition(exam, "open_marks_entry")]
        )
        assert opened == "marks_entry", opened
        assert isinstance(add, uuid.UUID | ConflictError), add
        expected = 2 if isinstance(add, uuid.UUID) else 1
        assert _count(env, "exam_subjects", exam_id=exam) == expected


def test_conflicting_lifecycle_transitions(env: Env) -> None:
    exam, component = env.open_exam()
    env.bulk(exam, component, env.students)()
    results = run_together([env.publish(exam), env.transition(exam, "cancel")])
    single_winner(results)
    status = env.scalar("SELECT status FROM exams WHERE id = :e", e=exam)
    publications = _count(env, "exam_publications", exam_id=exam)
    assert (status, publications) in (("published", 1), ("cancelled", 0))


# ============================================================================ marks
def test_concurrent_bulk_for_the_same_students(env: Env) -> None:
    exam, component = env.open_exam()
    results = run_together([env.bulk(exam, component, env.students, m) for m in (10, 20, 30)])
    single_winner(results)
    assert _count(env, "exam_marks", exam_subject_id=component) == STUDENTS
    values = env.run(
        lambda s: set(
            s.execute(
                text("SELECT marks_obtained FROM exam_marks WHERE exam_subject_id = :c"),
                {"c": component},
            ).scalars()
        )
    )
    assert len(values) == 1  # one request's values, never a mixture


def test_concurrent_disjoint_and_overlapping_bulks(env: Env) -> None:
    exam, component = env.open_exam()
    groups = [env.students[i::3] for i in range(3)]
    assert run_together([env.bulk(exam, component, g) for g in groups]) == [2, 2, 2]
    other, other_component = env.open_exam("Final")
    first, second = env.students[:4], env.students[3:]
    results = run_together(
        [env.bulk(other, other_component, first), env.bulk(other, other_component, second)]
    )
    single_winner(results)
    assert _count(env, "exam_marks", exam_subject_id=other_component) in (len(first), len(second))


def test_concurrent_corrections_keep_a_consistent_history(env: Env) -> None:
    exam, component = env.open_exam()
    env.bulk(exam, component, env.students[:1], 40)()
    mark = env.scalar("SELECT id FROM exam_marks WHERE exam_subject_id = :c", c=component)
    targets = [41, 42, 43, 44, 45]
    results = run_together([env.correct(mark, m, f"fix {m}") for m in targets])
    assert not [r for r in results if isinstance(r, Exception)], results
    rows = env.run(
        lambda s: s.execute(
            text(
                "SELECT sequence, previous_marks, new_marks FROM exam_mark_changes "
                "WHERE mark_id = :m ORDER BY sequence"
            ),
            {"m": mark},
        ).all()
    )
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0][1] == Decimal("40.00")  # the first correction starts from the original
    for before, after in itertools.pairwise(rows):
        assert after[1] == before[2]  # each correction starts where the previous ended
    assert env.scalar("SELECT marks_obtained FROM exam_marks WHERE id = :m", m=mark) == rows[-1][2]


# ====================================================================== publication
def test_duplicate_publication(env: Env) -> None:
    exam, component = env.open_exam()
    env.bulk(exam, component, env.students)()
    results = run_together([env.publish(exam) for _ in range(4)])
    single_winner(results)
    assert _count(env, "exam_publications", exam_id=exam) == 1
    assert _count(env, "exam_report_cards", exam_id=exam) == STUDENTS
    single_winner(run_together([env.reopen(exam) for _ in range(3)]))
    assert env.scalar("SELECT status FROM exams WHERE id = :e", e=exam) == "marks_entry"


def test_correction_racing_publication(env: Env) -> None:
    """A correction either commits before the publication (and is in the snapshot) or
    is refused because the results are published: never lost, never half-applied."""
    for round_ in range(4):
        exam, component = env.open_exam(f"Race {round_}")
        env.bulk(exam, component, env.students[:1], 40)()
        mark = env.scalar("SELECT id FROM exam_marks WHERE exam_subject_id = :c", c=component)
        corrected, published = run_together([env.correct(mark, 77), env.publish(exam)])
        assert published == 1, published
        snapshot = env.scalar(
            "SELECT subjects->0->>'marks_obtained' FROM exam_report_cards "
            "WHERE exam_id = :e AND student_id = :s",
            e=exam,
            s=env.students[0],
        )
        if isinstance(corrected, Exception):
            assert isinstance(corrected, ConflictError), corrected
            assert snapshot == "40.00"
        else:
            assert corrected == Decimal("77.00")
            assert snapshot == "77.00"
        stored = env.scalar("SELECT marks_obtained FROM exam_marks WHERE id = :m", m=mark)
        assert str(stored) == snapshot


def test_publication_waits_for_marks_writers(env: Env) -> None:
    exam, component = env.open_exam()
    env.bulk(exam, component, env.students)()
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(text("SELECT id FROM exams WHERE id = :e FOR SHARE"), {"e": exam})
        future = pool.submit(env.publish(exam))
        assert_blocked(future)
        holder.commit()
        assert future.result(timeout=TIMEOUT) == 1


def test_publication_snapshot_includes_a_write_it_waited_for(env: Env) -> None:
    """Publishing must calculate *after* in-flight marks writes commit, not before."""
    exam, component = env.open_exam()
    env.bulk(exam, component, env.students[:1], 40)()
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(text("SELECT id FROM exams WHERE id = :e FOR SHARE"), {"e": exam})
        holder.execute(
            text("UPDATE exam_marks SET marks_obtained = 77 WHERE exam_subject_id = :c"),
            {"c": component},
        )
        future = pool.submit(env.publish(exam))
        assert_blocked(future)
        holder.commit()
        assert future.result(timeout=TIMEOUT) == 1
    snapshot = env.scalar(
        "SELECT subjects->0->>'marks_obtained' FROM exam_report_cards "
        "WHERE exam_id = :e AND student_id = :s",
        e=exam,
        s=env.students[0],
    )
    assert snapshot == "77.00"


def test_marks_writers_wait_for_publication(env: Env) -> None:
    exam, component = env.open_exam()
    env.bulk(exam, component, env.students[:1])()
    mark = env.scalar("SELECT id FROM exam_marks WHERE exam_subject_id = :c", c=component)
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(text("SELECT id FROM exams WHERE id = :e FOR UPDATE"), {"e": exam})
        future = pool.submit(env.correct(mark, 60))
        assert_blocked(future)
        holder.rollback()
        assert future.result(timeout=TIMEOUT) == Decimal("60.00")


def test_raw_sql_duplicate_mark_race(env: Env) -> None:
    """Without any service: two connections inserting the same mark, one must fail."""
    exam, component = env.open_exam()
    enrollment = env.scalar(
        "SELECT id FROM student_enrollments WHERE student_id = :s", s=env.students[0]
    )
    sql = text(
        "INSERT INTO exam_marks (id, school_id, exam_id, exam_subject_id, max_marks, student_id, "
        "enrollment_id, academic_year_id, class_id, section_id, status, marks_obtained, "
        "entered_by_user_id, entered_at) VALUES (gen_random_uuid(), :school, :exam, :c, 100, "
        ":student, :enrollment, :year, :class, :section, 'present', 1, :user, now())"
    )
    params = {
        "school": env.ids["school"],
        "exam": exam,
        "c": component,
        "student": env.students[0],
        "enrollment": enrollment,
        "year": env.ids["year"],
        "class": env.ids["class"],
        "section": env.ids["section"],
        "user": env.ids["actor"],
    }

    def insert() -> str:
        with env.db.engine.begin() as conn:
            conn.execute(sql, params)
        return "inserted"

    results = run_together([insert, insert])
    errors = [r for r in results if isinstance(r, Exception)]
    assert results.count("inserted") == 1, results
    assert len(errors) == 1
    assert isinstance(errors[0], IntegrityError)
    assert isinstance(errors[0].orig, pg.UniqueViolation)
