"""Real concurrency for assignments and submissions (committed rows, separate
connections on separate threads, no mocks)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
from app.schemas.assignments import AssignmentCreate, ReviewInput
from app.security.permissions import ROLE_PERMISSIONS, RoleName
from app.security.principal import Principal
from app.security.school_scope import SchoolScope
from app.services.assignment_submissions import SubmissionService
from app.services.assignments import AssignmentService
from app.services.users import create_user_account
from tests.integration.test_attendance_concurrency import (
    TIMEOUT,
    assert_blocked,
    run_together,
    single_winner,
)

pytestmark = pytest.mark.db
STUDENTS = 4
TABLES = (
    "assignment_review_changes",
    "assignment_submission_revisions",
    "assignment_submissions",
    "assignments",
    "student_enrollments",
    "students",
    "sections",
    "subjects",
    "classes",
    "academic_years",
)


def _principal(user_id: uuid.UUID, role: RoleName, school: uuid.UUID | None) -> Principal:
    return Principal(
        user_id=user_id,
        session_id=uuid.uuid4(),
        school_id=school,
        roles=frozenset({role.value}),
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[role]),
    )


@dataclass
class Env:
    db: Database
    admin: SchoolScope
    students: list[SchoolScope]
    ids: dict[str, uuid.UUID]

    def run(self, fn: Callable[[Session], Any]) -> Any:
        with self.db.session() as session:
            return fn(session)

    def scalar(self, sql: str, **params: object) -> Any:
        return self.run(lambda s: s.execute(text(sql), params).scalar_one())

    def assignment(self, **extra: Any) -> uuid.UUID:
        data = AssignmentCreate(
            academic_year_id=self.ids["year"],
            class_id=self.ids["class"],
            section_id=self.ids["section"],
            subject_id=self.ids["subject"],
            title="Race",
            instructions="Do it",
            assigned_on=datetime.now(UTC).date(),
            due_at=datetime.now(UTC) + timedelta(days=3),
            max_marks=Decimal(10),
            **extra,
        )
        return self.run(lambda s: AssignmentService(s).create(self.admin, data).id)  # type: ignore[no-any-return]

    def publish(self, assignment: uuid.UUID) -> Callable[[], str]:
        return lambda: self.run(
            lambda s: AssignmentService(s).publish(self.admin, assignment).status
        )

    def close(self, assignment: uuid.UUID) -> Callable[[], str]:
        return lambda: self.run(lambda s: AssignmentService(s).close(self.admin, assignment).status)

    def submit(
        self, assignment: uuid.UUID, student: int = 0, content: str = "Work"
    ) -> Callable[[], uuid.UUID]:
        return lambda: self.run(
            lambda s: SubmissionService(s).submit(self.students[student], assignment, content).id
        )

    def resubmit(self, submission: uuid.UUID, content: str, student: int = 0) -> Callable[[], int]:
        return lambda: self.run(
            lambda s: (
                SubmissionService(s)
                .resubmit(self.students[student], submission, content)
                .revision_count
            )
        )

    def review(self, submission: uuid.UUID, **body: Any) -> Callable[[], Any]:
        data = ReviewInput(**body)
        return lambda: self.run(lambda s: SubmissionService(s).review(self.admin, submission, data))


@pytest.fixture
def env(db_engine: object, test_settings: Settings) -> Iterator[Env]:
    db = Database(test_settings.model_copy(update={"db_pool_size": 12}))
    tag = uuid.uuid4().hex[:8].upper()
    users: list[uuid.UUID] = []
    with db.session() as s:
        school = School(name=f"Homework Race {tag}", code=f"HR-{tag}")
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
        s.add(section)
        s.flush()
        admin = create_user_account(
            s,
            test_settings,
            full_name="Race Root",
            email=f"hrace-{tag.lower()}@x.test",
            password=None,
            roles=[RoleName.SUPER_ADMIN],
            source="test",
        )
        users.append(admin.id)
        student_scopes: list[uuid.UUID] = []
        for i in range(STUDENTS):
            user = create_user_account(
                s,
                test_settings,
                full_name=f"S{i}",
                email=f"hrs{i}-{tag.lower()}@x.test",
                password=None,
                roles=[RoleName.STUDENT],
                school_id=school.id,
                source="test",
            )
            users.append(user.id)
            student = Student(
                school_id=school.id,
                user_id=user.id,
                admission_number=f"H{i}",
                first_name="S",
                last_name=str(i),
            )
            s.add(student)
            s.flush()
            s.add(
                StudentEnrollment(
                    school_id=school.id,
                    student_id=student.id,
                    academic_year_id=year.id,
                    class_id=cls.id,
                    section_id=section.id,
                    enrollment_date=date(2026, 6, 10),
                )
            )
            student_scopes.append(user.id)
        s.commit()
        ids = {
            "school": school.id,
            "year": year.id,
            "class": cls.id,
            "section": section.id,
            "subject": subject.id,
        }
    env = Env(
        db,
        SchoolScope(
            school_id=ids["school"], principal=_principal(users[0], RoleName.SUPER_ADMIN, None)
        ),
        [
            SchoolScope(
                school_id=ids["school"], principal=_principal(u, RoleName.STUDENT, ids["school"])
            )
            for u in student_scopes
        ],
        ids,
    )
    try:
        yield env
    finally:
        with db.session() as s:
            p = {"s": ids["school"], "ss": str(ids["school"])}
            s.execute(text("SET LOCAL app.assignment_history_purge = 'on'"))
            for table in TABLES:
                s.execute(text(f"DELETE FROM {table} WHERE school_id = :s"), p)  # noqa: S608
            for user in users:
                s.execute(
                    text(
                        "DELETE FROM audit_logs WHERE user_id = :u "
                        "OR resource_id = CAST(:u AS text) "
                        "OR metadata->>'school_id' = :ss"
                    ),
                    {**p, "u": user},
                )
            s.execute(
                text("DELETE FROM users WHERE school_id = :s OR id = ANY(:u)"), {**p, "u": users}
            )
            s.execute(text("DELETE FROM schools WHERE id = :s"), p)
            s.commit()
        db.dispose()


def _published(env: Env, **extra: Any) -> uuid.UUID:
    assignment = env.assignment(**extra)
    env.publish(assignment)()
    return assignment


# ====================================================================== submissions
def test_concurrent_first_submissions(env: Env) -> None:
    assignment = _published(env)
    single_winner(run_together([env.submit(assignment, 0, f"try {i}") for i in range(5)]))
    assert (
        env.scalar(
            "SELECT count(*) FROM assignment_submissions WHERE assignment_id = :a", a=assignment
        )
        == 1
    )
    assert env.scalar("SELECT count(*) FROM assignment_submission_revisions") >= 1
    distinct = run_together([env.submit(assignment, i) for i in range(1, STUDENTS)])
    assert all(isinstance(r, uuid.UUID) for r in distinct), distinct


def test_concurrent_resubmissions_keep_a_contiguous_history(env: Env) -> None:
    assignment = _published(env)
    submission = env.submit(assignment)()
    results = run_together([env.resubmit(submission, f"revision {i}") for i in range(4)])
    assert not [r for r in results if isinstance(r, Exception)], results
    revisions = env.run(
        lambda s: (
            s.execute(
                text(
                    "SELECT revision FROM assignment_submission_revisions "
                    "WHERE submission_id = :s ORDER BY revision"
                ),
                {"s": submission},
            )
            .scalars()
            .all()
        )
    )
    assert revisions == [1, 2, 3, 4, 5]
    assert (
        env.scalar("SELECT revision_count FROM assignment_submissions WHERE id = :s", s=submission)
        == 5
    )


def test_concurrent_review_changes_keep_a_consistent_history(env: Env) -> None:
    assignment = _published(env)
    submission = env.submit(assignment)()
    env.review(submission, marks_awarded=5)()
    results = run_together(
        [env.review(submission, marks_awarded=m, reason=f"r{m}") for m in (6, 7, 8, 9)]
    )
    assert not [r for r in results if isinstance(r, Exception)], results
    rows = env.run(
        lambda s: s.execute(
            text(
                "SELECT sequence, previous_marks, new_marks FROM assignment_review_changes "
                "WHERE submission_id = :s ORDER BY sequence"
            ),
            {"s": submission},
        ).all()
    )
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]
    for before, after in itertools.pairwise(rows):
        assert after[1] == before[2]  # each change starts where the previous one ended
    assert (
        env.scalar("SELECT marks_awarded FROM assignment_submissions WHERE id = :s", s=submission)
        == rows[-1][2]
    )


def test_resubmission_racing_review(env: Env) -> None:
    """Either the resubmission lands first (and the review covers it) or the review does
    (and the resubmission is refused). Never a review of a revision that was replaced."""
    for _ in range(3):
        assignment = _published(env)
        submission = env.submit(assignment)()
        resubmitted, reviewed = run_together(
            [env.resubmit(submission, "late change"), env.review(submission, marks_awarded=7)]
        )
        assert not isinstance(reviewed, Exception), reviewed
        assert isinstance(resubmitted, int | ConflictError), resubmitted
        row = env.run(
            lambda s, sub=submission: s.execute(
                text(
                    "SELECT revision_count, reviewed_revision FROM assignment_submissions "
                    "WHERE id = :s"
                ),
                {"s": sub},
            ).one()
        )
        assert row[0] == row[1]


def test_submission_racing_closure(env: Env) -> None:
    for _ in range(3):
        assignment = _published(env)
        submitted, closed = run_together([env.submit(assignment), env.close(assignment)])
        assert closed == "closed", closed
        assert isinstance(submitted, uuid.UUID | ConflictError), submitted
        late = env.scalar(
            "SELECT count(*) FROM assignment_submission_revisions r "
            "JOIN assignment_submissions s ON s.id = r.submission_id "
            "JOIN assignments a ON a.id = s.assignment_id "
            "WHERE a.id = :a AND r.submitted_at > a.closed_at",
            a=assignment,
        )
        assert late == 0


# ======================================================================== lifecycle
def test_concurrent_publication(env: Env) -> None:
    assignment = env.assignment()
    single_winner(run_together([env.publish(assignment) for _ in range(4)]))
    assert env.scalar("SELECT status FROM assignments WHERE id = :a", a=assignment) == "published"


def test_closing_waits_for_in_flight_submissions(env: Env) -> None:
    assignment = _published(env)
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            text("SELECT id FROM assignments WHERE id = :a FOR SHARE"), {"a": assignment}
        )
        future = pool.submit(env.close(assignment))
        assert_blocked(future)
        holder.commit()
        assert future.result(timeout=TIMEOUT) == "closed"


def test_submission_waits_for_a_closing_transaction(env: Env) -> None:
    assignment = _published(env)
    with env.db.engine.connect() as holder, ThreadPoolExecutor(1) as pool:
        holder.begin()
        holder.execute(
            text("SELECT id FROM assignments WHERE id = :a FOR UPDATE"), {"a": assignment}
        )
        future = pool.submit(env.submit(assignment))
        assert_blocked(future)
        holder.execute(
            text(
                "UPDATE assignments SET status = 'closed', closed_at = now(), "
                "closed_by_user_id = :u WHERE id = :a"
            ),
            {"a": assignment, "u": env.admin.actor_id},
        )
        holder.commit()
        with pytest.raises(ConflictError):
            future.result(timeout=TIMEOUT)


def test_raw_sql_duplicate_submission_race(env: Env) -> None:
    assignment = _published(env)
    student, enrollment = env.run(
        lambda s: s.execute(
            text("SELECT student_id, id FROM student_enrollments WHERE school_id = :s LIMIT 1"),
            {"s": env.ids["school"]},
        ).one()
    )
    sql = text(
        "INSERT INTO assignment_submissions (id, school_id, assignment_id, student_id, "
        "enrollment_id, "
        "academic_year_id, class_id, section_id, max_marks, revision_count, first_submitted_at, "
        "last_submitted_at, is_late) "
        "VALUES (gen_random_uuid(), :school, :a, :st, :e, :y, :c, :sec, "
        "10, 1, now(), now(), false)"
    )
    params = {
        "school": env.ids["school"],
        "a": assignment,
        "st": student,
        "e": enrollment,
        "y": env.ids["year"],
        "c": env.ids["class"],
        "sec": env.ids["section"],
    }

    def insert() -> str:
        with env.db.engine.begin() as conn:
            conn.execute(sql, params)
        return "inserted"

    results = run_together([insert, insert])
    errors = [r for r in results if isinstance(r, Exception)]
    assert results.count("inserted") == 1, results
    assert isinstance(errors[0], IntegrityError)
    assert isinstance(errors[0].orig, pg.UniqueViolation)
