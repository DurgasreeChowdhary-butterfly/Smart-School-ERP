"""Database-level assignment integrity, exercised with raw SQL that bypasses every service
check. PostgreSQL itself (constraints and triggers) must refuse invalid data."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import SCHOOL_A, SCHOOL_B
from tests.integration.test_exam_constraints import _world

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]
Ids = dict[str, uuid.UUID]


def _violates(
    session: Session,
    sql: str,
    params: dict[str, object],
    expected: type,
    constraint: str | None = None,
) -> None:
    with pytest.raises(IntegrityError) as exc_info, session.begin_nested():
        session.execute(text(sql), params)
    assert isinstance(exc_info.value.orig, expected), exc_info.value.orig
    if constraint is not None:
        assert exc_info.value.orig.diag.constraint_name == constraint


ASSIGNMENT = (
    "INSERT INTO assignments (id, school_id, academic_year_id, class_id, section_id, subject_id, "
    "title, instructions, assigned_on, due_at, allow_late, max_marks, status, created_by_user_id) "
    "VALUES (:id, :school, :year, :class, :section, :subject, 'T', 'Do it', '2026-07-01', "
    ":due, :late, :max, :status, :user)"
)


def _assignment_params(base: Ids, school: uuid.UUID = SCHOOL_A, **override: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(),
        "school": school,
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "subject": base["subject"],
        "due": "2026-07-10T10:00:00+00",
        "late": False,
        "max": 10,
        "status": "draft",
        "user": base["user"],
    }
    params.update(override)
    return params


def _assignment(session: Session, base: Ids, **override: Any) -> uuid.UUID:
    params = _assignment_params(base, **override)
    session.execute(text(ASSIGNMENT), params)
    return params["id"]  # type: ignore[no-any-return]


def _publish(session: Session, assignment: uuid.UUID, user: uuid.UUID) -> None:
    session.execute(
        text(
            "UPDATE assignments SET status = 'published', published_at = now(), "
            "published_by_user_id = :u WHERE id = :a"
        ),
        {"u": user, "a": assignment},
    )


SUBMISSION = (
    "INSERT INTO assignment_submissions (id, school_id, assignment_id, student_id, enrollment_id, "
    "academic_year_id, class_id, section_id, max_marks, revision_count, first_submitted_at, "
    "last_submitted_at, is_late) VALUES (:id, :school, :assignment, :student, :enrollment, "
    ":year, :class, :section, :max, 1, now(), now(), false)"
)


def _submission_params(base: Ids, assignment: uuid.UUID, **override: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "assignment": assignment,
        "student": base["student"],
        "enrollment": base["enrollment_student"],
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "max": 10,
    }
    params.update(override)
    return params


REVISION = (
    "INSERT INTO assignment_submission_revisions (id, school_id, submission_id, revision, content, "
    "submitted_by_user_id, submitted_at, due_at) VALUES (:id, :school, :submission, :revision, "
    ":content, :user, :at, :due)"
)


@pytest.fixture
def ids(db_session: Session) -> dict[str, Ids]:
    return {"a": _world(db_session, SCHOOL_A, "A"), "b": _world(db_session, SCHOOL_B, "B")}


@pytest.fixture
def open_assignment(db_session: Session, ids: dict[str, Ids]) -> uuid.UUID:
    a = ids["a"]
    assignment = _assignment(db_session, a, due="2099-01-01T00:00:00+00")
    _publish(db_session, assignment, a["user"])
    return assignment


# ================================================================== assignments
@pytest.mark.parametrize(
    ("override", "error", "constraint"),
    [
        ({"year": ("b", "year")}, pg.ForeignKeyViolation, "fk_assignments_year_same_school"),
        ({"class": ("b", "class")}, pg.ForeignKeyViolation, "fk_assignments_class_same_school"),
        (
            {"section": ("b", "section")},
            pg.ForeignKeyViolation,
            "fk_assignments_section_same_school",
        ),
        ({"section": ("a", "section2")}, pg.ForeignKeyViolation, "fk_assignments_section_in_class"),
        (
            {"subject": ("b", "subject")},
            pg.ForeignKeyViolation,
            "fk_assignments_subject_same_school",
        ),
        ({"max": 0}, pg.CheckViolation, "ck_assignments_max_marks_range"),
        ({"max": 1000.01}, pg.CheckViolation, "ck_assignments_max_marks_range"),
        ({"status": "published"}, pg.CheckViolation, "assignments_lifecycle"),
    ],
)
def test_assignment_constraints(
    db_session: Session, ids: dict[str, Ids], override: dict[str, Any], error: type, constraint: str
) -> None:
    resolved = {k: ids[v[0]][v[1]] if isinstance(v, tuple) else v for k, v in override.items()}
    _violates(db_session, ASSIGNMENT, _assignment_params(ids["a"], **resolved), error, constraint)


def test_lifecycle_and_frozen_settings(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    assignment = _assignment(db_session, a)
    for sql, constraint in (
        (
            "UPDATE assignments SET status = 'closed', closed_at = now(), closed_by_user_id = :u "
            "WHERE id = :a",
            "assignments_lifecycle",
        ),
        (
            "UPDATE assignments SET status = 'published' WHERE id = :a",
            "ck_assignments_publication_complete",
        ),
        ("UPDATE assignments SET section_id = :s WHERE id = :a", "assignments_placement_immutable"),
    ):
        _violates(
            db_session,
            sql,
            {"a": assignment, "u": a["user"], "s": a["section_b"]},
            pg.CheckViolation,
            constraint,
        )
    _publish(db_session, assignment, a["user"])
    for sql in (
        "UPDATE assignments SET max_marks = 50 WHERE id = :a",
        "UPDATE assignments SET assigned_on = '2026-07-02' WHERE id = :a",
    ):
        _violates(
            db_session, sql, {"a": assignment}, pg.CheckViolation, "assignments_settings_frozen"
        )
    _violates(
        db_session,
        "UPDATE assignments SET status = 'draft', published_at = NULL, published_by_user_id = NULL "
        "WHERE id = :a",
        {"a": assignment},
        pg.CheckViolation,
        "assignments_lifecycle",
    )
    db_session.execute(
        text("UPDATE assignments SET due_at = due_at + interval '1 day' WHERE id = :a"),
        {"a": assignment},
    )
    db_session.execute(
        text(
            "UPDATE assignments SET status = 'closed', closed_at = now(), closed_by_user_id = :u "
            "WHERE id = :a"
        ),
        {"a": assignment, "u": a["user"]},
    )
    _violates(
        db_session,
        "UPDATE assignments SET title = 'x' WHERE id = :a",
        {"a": assignment},
        pg.CheckViolation,
        "assignments_lifecycle",
    )


# =================================================================== submissions
@pytest.mark.parametrize(
    ("override", "error", "constraint"),
    [
        # Another student's enrollment.
        (
            {"enrollment": "enrollment_student_b"},
            pg.ForeignKeyViolation,
            "fk_assignment_submissions_enrollment_placement",
        ),
        # The right student, claimed to be in another section.
        (
            {"section": "section_b"},
            pg.ForeignKeyViolation,
            "fk_assignment_submissions_assignment_placement",
        ),
        # A student of another class.
        (
            {
                "student": "student2",
                "enrollment": "enrollment_student2",
                "class": "class2",
                "section": "section2",
            },
            pg.ForeignKeyViolation,
            "fk_assignment_submissions_assignment_placement",
        ),
        # A bigger maximum than the assignment's.
        ({"max": 100}, pg.ForeignKeyViolation, "fk_assignment_submissions_max_marks"),
    ],
)
def test_submission_placement(
    db_session: Session,
    ids: dict[str, Ids],
    open_assignment: uuid.UUID,
    override: dict[str, Any],
    error: type,
    constraint: str,
) -> None:
    a = ids["a"]
    resolved = {k: a[v] if isinstance(v, str) else v for k, v in override.items()}
    _violates(
        db_session,
        SUBMISSION,
        _submission_params(a, open_assignment, **resolved),
        error,
        constraint,
    )


def test_submissions_cannot_cross_schools(
    db_session: Session, ids: dict[str, Ids], open_assignment: uuid.UUID
) -> None:
    a, b = ids["a"], ids["b"]
    params = _submission_params(
        a, open_assignment, student=b["student"], enrollment=b["enrollment_student"]
    )
    _violates(db_session, SUBMISSION, params, pg.ForeignKeyViolation)
    params = _submission_params(a, open_assignment, school=SCHOOL_B)
    _violates(db_session, SUBMISSION, params, pg.ForeignKeyViolation)


def test_submission_rules(
    db_session: Session, ids: dict[str, Ids], open_assignment: uuid.UUID
) -> None:
    a = ids["a"]
    draft = _assignment(db_session, a)
    _violates(
        db_session,
        SUBMISSION,
        _submission_params(a, draft),
        pg.CheckViolation,
        "assignment_submissions_assignment_open",
    )
    params = _submission_params(a, open_assignment)
    db_session.execute(text(SUBMISSION), params)
    _violates(
        db_session,
        SUBMISSION,
        {**params, "id": uuid.uuid4()},
        pg.UniqueViolation,
        "uq_assignment_submissions_assignment_id_student_id",
    )
    for sql, constraint in (
        (
            "UPDATE assignment_submissions SET marks_awarded = 11, reviewed_at = now(), "
            "reviewed_by_user_id = :u, reviewed_revision = 1 WHERE id = :s",
            "ck_assignment_submissions_marks_range",
        ),
        (
            "UPDATE assignment_submissions SET marks_awarded = 5 WHERE id = :s",
            "ck_assignment_submissions_review_fields_need_review",
        ),
        (
            "UPDATE assignment_submissions SET feedback = 'x', reviewed_at = now(), "
            "reviewed_by_user_id = :u, reviewed_revision = 2 WHERE id = :s",
            "ck_assignment_submissions_reviewed_revision_exists",
        ),
        (
            "UPDATE assignment_submissions SET student_id = :o WHERE id = :s",
            "assignment_submissions_immutable",
        ),
    ):
        _violates(
            db_session,
            sql,
            {"s": params["id"], "u": a["user"], "o": a["student_b"]},
            pg.CheckViolation,
            constraint,
        )
    _violates(
        db_session,
        "DELETE FROM assignment_submissions WHERE id = :s",
        {"s": params["id"]},
        pg.RestrictViolation,
    )


def test_ungraded_assignments_cannot_carry_marks(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    ungraded = _assignment(db_session, a, max=None, due="2099-01-01T00:00:00+00")
    _publish(db_session, ungraded, a["user"])
    params = _submission_params(a, ungraded, max=None)
    db_session.execute(text(SUBMISSION), params)
    _violates(
        db_session,
        "UPDATE assignment_submissions SET marks_awarded = 1, reviewed_at = now(), "
        "reviewed_by_user_id = :u, reviewed_revision = 1 WHERE id = :s",
        {"s": params["id"], "u": a["user"]},
        pg.CheckViolation,
        "ck_assignment_submissions_marks_range",
    )


# ===================================================================== revisions
def test_revisions(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    due = "2026-07-10T10:00:00+00"
    assignment = _assignment(db_session, a, due=due)
    _publish(db_session, assignment, a["user"])
    sub = _submission_params(a, assignment)
    db_session.execute(text(SUBMISSION), sub)
    base = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "submission": sub["id"],
        "revision": 1,
        "content": "Work",
        "user": a["user"],
        "at": "2026-07-09T10:00:00+00",
        "due": due,
    }
    db_session.execute(text(REVISION), base)
    is_late = db_session.execute(
        text("SELECT is_late FROM assignment_submission_revisions WHERE id = :i"), {"i": base["id"]}
    ).scalar()
    assert is_late is False
    for override, error, constraint in (
        (
            {"revision": 1},
            pg.UniqueViolation,
            "uq_assignment_submission_revisions_submission_id_revision",
        ),
        ({"content": "  "}, pg.CheckViolation, "ck_assignment_submission_revisions_content_valid"),
        ({"due": "2026-08-01T00:00:00+00"}, pg.CheckViolation, "assignment_revisions_due_snapshot"),
        ({"at": "2026-07-11T10:00:00+00"}, pg.CheckViolation, "assignment_revisions_late_refused"),
        (
            {"school": SCHOOL_B},
            pg.ForeignKeyViolation,
            "fk_assignment_submission_revisions_submission_same_school",
        ),
    ):
        params = {**base, "id": uuid.uuid4(), "revision": 2, **override}
        _violates(db_session, REVISION, params, error, constraint)
    for sql in (
        "UPDATE assignment_submission_revisions SET content = 'Edited' WHERE id = :i",
        "DELETE FROM assignment_submission_revisions WHERE id = :i",
    ):
        _violates(db_session, sql, {"i": base["id"]}, pg.RestrictViolation)
    db_session.execute(
        text(
            "UPDATE assignments SET status = 'closed', closed_at = now(), closed_by_user_id = :u "
            "WHERE id = :a"
        ),
        {"a": assignment, "u": a["user"]},
    )
    _violates(
        db_session,
        REVISION,
        {**base, "id": uuid.uuid4(), "revision": 2},
        pg.CheckViolation,
        "assignment_submissions_assignment_open",
    )
    _violates(
        db_session,
        "UPDATE assignment_submissions SET revision_count = 2, last_submitted_at = now() "
        "WHERE id = :s",
        {"s": sub["id"]},
        pg.CheckViolation,
        "assignment_submissions_assignment_open",
    )
    # Reviews remain possible after closing.
    db_session.execute(
        text(
            "UPDATE assignment_submissions SET marks_awarded = 9, reviewed_at = now(), "
            "reviewed_by_user_id = :u, reviewed_revision = 1 WHERE id = :s"
        ),
        {"s": sub["id"], "u": a["user"]},
    )


# ================================================================ review history
CHANGE = (
    "INSERT INTO assignment_review_changes (id, school_id, submission_id, sequence, revision, "
    "new_marks, reason, changed_by_user_id) "
    "VALUES (:id, :school, :submission, :seq, 1, 5, :reason, :user)"
)


def test_review_history_is_append_only(
    db_session: Session, ids: dict[str, Ids], open_assignment: uuid.UUID
) -> None:
    a = ids["a"]
    sub = _submission_params(a, open_assignment)
    db_session.execute(text(SUBMISSION), sub)
    base = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "submission": sub["id"],
        "seq": 1,
        "reason": None,
        "user": a["user"],
    }
    db_session.execute(text(CHANGE), base)
    _violates(
        db_session,
        CHANGE,
        {**base, "id": uuid.uuid4(), "seq": 2},
        pg.CheckViolation,
        "ck_assignment_review_changes_reason_required_for_change",
    )
    _violates(
        db_session,
        CHANGE,
        {**base, "id": uuid.uuid4(), "reason": "x"},
        pg.UniqueViolation,
        "uq_assignment_review_changes_submission_id_sequence",
    )
    _violates(
        db_session,
        CHANGE,
        {**base, "id": uuid.uuid4(), "seq": 2, "reason": "x", "school": SCHOOL_B},
        pg.ForeignKeyViolation,
    )
    for sql in (
        "UPDATE assignment_review_changes SET reason = 'x' WHERE id = :i",
        "DELETE FROM assignment_review_changes WHERE id = :i",
    ):
        _violates(db_session, sql, {"i": base["id"]}, pg.RestrictViolation)
    db_session.execute(text("SET LOCAL app.assignment_history_purge = 'on'"))
    db_session.execute(
        text("DELETE FROM assignment_review_changes WHERE id = :i"), {"i": base["id"]}
    )
