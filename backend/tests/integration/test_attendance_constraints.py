"""Database-level attendance integrity, exercised with raw SQL that bypasses every
service check. PostgreSQL itself must refuse invalid data."""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import SCHOOL_A, SCHOOL_B

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seed_schools")]


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


def _insert(session: Session, sql: str, **params: object) -> uuid.UUID:
    new_id = uuid.uuid4()
    session.execute(text(sql), {"id": new_id, **params})
    return new_id


def _world(session: Session, school: uuid.UUID, tag: str) -> dict[str, uuid.UUID]:
    def row(sql: str, **params: object) -> uuid.UUID:
        return _insert(session, sql, s=school, **params)

    ids = {
        "user": row(
            "INSERT INTO users (id, full_name, email, school_id) VALUES (:id, 'T', :e, :s)",
            e=f"{uuid.uuid4().hex[:12]}@x.test",
        ),
        "year": row(
            "INSERT INTO academic_years (id, school_id, name, start_date, end_date) "
            "VALUES (:id, :s, :n, '2026-06-01', '2027-05-31')",
            n=f"Y-{tag}",
        ),
        "class": row(
            "INSERT INTO classes (id, school_id, name, code) VALUES (:id, :s, 'C', :c)",
            c=f"C-{tag}",
        ),
        "class2": row(
            "INSERT INTO classes (id, school_id, name, code) VALUES (:id, :s, 'C2', :c)",
            c=f"C2-{tag}",
        ),
        "subject": row(
            "INSERT INTO subjects (id, school_id, name, code) VALUES (:id, :s, 'M', :c)",
            c=f"M-{tag}",
        ),
    }
    for key, cls, code in (
        ("section", "class", "A"),
        ("section_b", "class", "B"),
        ("section2", "class2", "A"),
    ):
        ids[key] = row(
            "INSERT INTO sections (id, school_id, class_id, name, code) "
            "VALUES (:id, :s, :c, :n, :n)",
            c=ids[cls],
            n=code,
        )
    for key, section in (("student", "section"), ("student_b", "section_b")):
        ids[key] = row(
            "INSERT INTO students (id, school_id, admission_number, first_name, last_name) "
            "VALUES (:id, :s, :adm, 'S', 'T')",
            adm=f"{key}-{tag}".upper(),
        )
        ids[f"enrollment_{key}"] = row(
            "INSERT INTO student_enrollments (id, school_id, student_id, academic_year_id, "
            "class_id, section_id, enrollment_date) VALUES (:id, :s, :st, :y, :c, :sec, "
            "'2026-06-10')",
            st=ids[key],
            y=ids["year"],
            c=ids["class"],
            sec=ids[section],
        )
    ids["session"] = _session(session, school, ids)
    return ids


SESSION = (
    "INSERT INTO attendance_sessions (id, school_id, academic_year_id, class_id, section_id, "
    "subject_id, session_date, period, status, start_time, end_time, created_by_user_id) "
    "VALUES (:id, :school, :year, :class, :section, :subject, :date, :period, :status, "
    ":start, :end, :user)"
)


def _session_params(base: dict[str, uuid.UUID], **override: object) -> dict[str, object]:
    params: dict[str, object] = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "subject": base["subject"],
        "date": "2026-07-10",
        "period": 1,
        "status": "active",
        "start": None,
        "end": None,
        "user": base["user"],
    }
    params.update(override)
    return params


def _session(
    session: Session, school: uuid.UUID, base: dict[str, uuid.UUID], **override: object
) -> uuid.UUID:
    params = _session_params(base, school=school, **override)
    session.execute(text(SESSION), params)
    return params["id"]  # type: ignore[return-value]


RECORD = (
    "INSERT INTO attendance_records (id, school_id, session_id, enrollment_id, student_id, "
    "academic_year_id, class_id, section_id, status, original_status, marked_by_user_id, "
    "marked_at, corrected_by_user_id, corrected_at, correction_reason) "
    "VALUES (:id, :school, :session, :enrollment, :student, :year, :class, :section, "
    ":status, :original, :user, now(), :cby, :cat, :creason)"
)


def _record_params(base: dict[str, uuid.UUID], **override: object) -> dict[str, object]:
    params: dict[str, object] = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "session": base["session"],
        "enrollment": base["enrollment_student"],
        "student": base["student"],
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "status": "present",
        "original": "present",
        "user": base["user"],
        "cby": None,
        "cat": None,
        "creason": None,
    }
    params.update(override)
    return params


@pytest.fixture
def a(db_session: Session) -> dict[str, uuid.UUID]:
    return _world(db_session, SCHOOL_A, "A")


@pytest.fixture
def b(db_session: Session) -> dict[str, uuid.UUID]:
    return _world(db_session, SCHOOL_B, "B")


# ========================================================================== sessions
@pytest.mark.parametrize(
    ("key", "column", "constraint"),
    [
        ("year", "year", "fk_attendance_sessions_year_same_school"),
        ("class", "class", "fk_attendance_sessions_class_same_school"),
        ("section", "section", "fk_attendance_sessions_section_same_school"),
        ("subject", "subject", "fk_attendance_sessions_subject_same_school"),
    ],
)
def test_session_cannot_mix_schools(
    db_session: Session,
    a: dict[str, uuid.UUID],
    b: dict[str, uuid.UUID],
    key: str,
    column: str,
    constraint: str,
) -> None:
    params = _session_params(a, date="2026-07-20", **{column: b[key]})
    if key == "class":  # keep the section consistent with the foreign class
        params["section"] = b["section"]
    with pytest.raises(IntegrityError) as exc_info, db_session.begin_nested():
        db_session.execute(text(SESSION), params)
    assert isinstance(exc_info.value.orig, pg.ForeignKeyViolation)
    assert exc_info.value.orig.diag.constraint_name in {
        constraint,
        "fk_attendance_sessions_section_same_school",
    }


def test_session_entirely_in_school_b_labelled_school_a(
    db_session: Session, a: dict[str, uuid.UUID], b: dict[str, uuid.UUID]
) -> None:
    _violates(
        db_session,
        SESSION,
        _session_params(b, school=SCHOOL_A, date="2026-07-20"),
        pg.ForeignKeyViolation,
    )


def test_session_section_must_belong_to_class(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    _violates(
        db_session,
        SESSION,
        _session_params(a, date="2026-07-20", **{"class": a["class2"]}),
        pg.ForeignKeyViolation,
        "fk_attendance_sessions_section_in_class",
    )


def test_session_unknown_references(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    for column in ("year", "class", "section", "subject", "user"):
        _violates(
            db_session,
            SESSION,
            _session_params(a, date="2026-07-20", **{column: uuid.uuid4()}),
            pg.ForeignKeyViolation,
        )


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"status": "cancelled"}, "ck_attendance_sessions_status_valid"),
        ({"period": 0}, "ck_attendance_sessions_period_range"),
        ({"period": 21}, "ck_attendance_sessions_period_range"),
        ({"start": "10:00", "end": "09:00"}, "ck_attendance_sessions_times_ordered"),
    ],
)
def test_session_checks(
    db_session: Session, a: dict[str, uuid.UUID], override: dict[str, object], constraint: str
) -> None:
    _violates(
        db_session,
        SESSION,
        _session_params(a, date="2026-07-20", **override),
        pg.CheckViolation,
        constraint,
    )


def test_session_uniqueness_boundaries(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    # The fixture created an active maths session on 07-10, period 1.
    _violates(
        db_session,
        SESSION,
        _session_params(a),
        pg.UniqueViolation,
        "uq_attendance_sessions_subject_active",
    )
    _session(db_session, SCHOOL_A, a, period=2)
    _session(db_session, SCHOOL_A, a, status="inactive")  # history may repeat
    _session(db_session, SCHOOL_A, a, subject=None)
    _violates(
        db_session,
        SESSION,
        _session_params(a, subject=None),
        pg.UniqueViolation,
        "uq_attendance_sessions_daily_active",
    )
    _session(db_session, SCHOOL_A, a, subject=None, section=a["section_b"])


def test_session_type_is_derived(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    daily = _session(db_session, SCHOOL_A, a, subject=None)
    kinds = dict(
        db_session.execute(
            text("SELECT id, session_type FROM attendance_sessions WHERE id IN (:a, :b)"),
            {"a": a["session"], "b": daily},
        ).all()
    )
    assert kinds == {a["session"]: "subject", daily: "daily"}
    with pytest.raises(Exception, match="generated"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE attendance_sessions SET session_type = 'daily' WHERE id = :i"),
            {"i": a["session"]},
        )


# =========================================================================== records
def test_valid_record_accepted(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(RECORD), _record_params(a))


def test_record_for_student_of_another_section_rejected(
    db_session: Session, a: dict[str, uuid.UUID]
) -> None:
    """student_b is enrolled in section B; the session is section A."""
    _violates(
        db_session,
        RECORD,
        _record_params(a, student=a["student_b"], enrollment=a["enrollment_student_b"]),
        pg.ForeignKeyViolation,
        "fk_attendance_records_enrollment_placement",
    )
    # Relabelling the record's section to match the enrollment breaks the session link.
    _violates(
        db_session,
        RECORD,
        _record_params(
            a,
            student=a["student_b"],
            enrollment=a["enrollment_student_b"],
            section=a["section_b"],
        ),
        pg.ForeignKeyViolation,
        "fk_attendance_records_session_placement",
    )


def test_record_enrollment_of_another_student_rejected(
    db_session: Session, a: dict[str, uuid.UUID]
) -> None:
    _violates(
        db_session,
        RECORD,
        _record_params(a, student=a["student_b"]),
        pg.ForeignKeyViolation,
        "fk_attendance_records_enrollment_placement",
    )


def test_record_cannot_cross_schools(
    db_session: Session, a: dict[str, uuid.UUID], b: dict[str, uuid.UUID]
) -> None:
    # School B student/enrollment in a school A session.
    _violates(
        db_session,
        RECORD,
        _record_params(a, student=b["student"], enrollment=b["enrollment_student"]),
        pg.ForeignKeyViolation,
    )
    # School B record pointing at a school A session.
    _violates(
        db_session,
        RECORD,
        _record_params(b, school=SCHOOL_B, session=a["session"]),
        pg.ForeignKeyViolation,
        "fk_attendance_records_session_placement",
    )
    # Fully school-B data labelled as school A.
    _violates(db_session, RECORD, _record_params(b), pg.ForeignKeyViolation)


def test_one_record_per_student_per_session(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(RECORD), _record_params(a))
    _violates(
        db_session,
        RECORD,
        _record_params(a, status="absent", original="absent"),
        pg.UniqueViolation,
        "uq_attendance_records_session_id_student_id",
    )


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"status": "sick"}, "ck_attendance_records_status_valid"),
        ({"status": "PRESENT"}, "ck_attendance_records_status_valid"),
        ({"original": "unknown"}, "ck_attendance_records_original_status_valid"),
        ({"cat": "2026-07-10T10:00:00Z"}, "ck_attendance_records_correction_complete"),
        ({"creason": "why"}, "ck_attendance_records_correction_complete"),
    ],
)
def test_record_checks(
    db_session: Session, a: dict[str, uuid.UUID], override: dict[str, object], constraint: str
) -> None:
    _violates(db_session, RECORD, _record_params(a, **override), pg.CheckViolation, constraint)


def test_record_unknown_references(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    for column in ("session", "enrollment", "user"):
        _violates(
            db_session, RECORD, _record_params(a, **{column: uuid.uuid4()}), pg.ForeignKeyViolation
        )


# =========================================================================== history
CHANGE = (
    "INSERT INTO attendance_record_changes (id, school_id, record_id, sequence, previous_status, "
    "new_status, reason, changed_by_user_id) VALUES (gen_random_uuid(), :school, :record, :seq, "
    ":prev, :new, :reason, :user)"
)


def test_history_rules(
    db_session: Session, a: dict[str, uuid.UUID], b: dict[str, uuid.UUID]
) -> None:
    params = _record_params(a)
    db_session.execute(text(RECORD), params)
    base = {
        "school": SCHOOL_A,
        "record": params["id"],
        "prev": "present",
        "new": "absent",
        "reason": "Register error",
        "user": a["user"],
        "seq": 1,
    }
    db_session.execute(text(CHANGE), base)
    _violates(
        db_session,
        CHANGE,
        {**base, "seq": 2, "reason": None},
        pg.CheckViolation,
        "ck_attendance_record_changes_reason_required_for_status_change",
    )
    # Two writers can never both append change number 1.
    _violates(
        db_session,
        CHANGE,
        {**base, "prev": "absent", "new": "late"},
        pg.UniqueViolation,
        "uq_attendance_record_changes_record_id_sequence",
    )
    _violates(
        db_session,
        CHANGE,
        {**base, "seq": 0},
        pg.CheckViolation,
        "ck_attendance_record_changes_sequence_positive",
    )
    remarks_only = {**base, "seq": 2, "prev": "absent", "new": "absent", "reason": None}
    db_session.execute(text(CHANGE), remarks_only)
    _violates(db_session, CHANGE, {**base, "seq": 3, "new": "gone"}, pg.CheckViolation)
    _violates(
        db_session,
        CHANGE,
        {**base, "seq": 4, "school": SCHOOL_B, "user": b["user"]},
        pg.ForeignKeyViolation,
        "fk_attendance_record_changes_record_same_school",
    )
    for sql in (
        "UPDATE attendance_record_changes SET reason = 'rewritten'",
        "DELETE FROM attendance_record_changes",
    ):
        _violates(db_session, sql, {}, pg.RestrictViolation)
    # A deliberate purge (maintenance only) is the single way to remove history.
    with db_session.begin_nested():
        db_session.execute(text("SET LOCAL app.attendance_history_purge = 'on'"))
        assert db_session.execute(text("DELETE FROM attendance_record_changes")).rowcount == 2
        db_session.execute(text("SET LOCAL app.attendance_history_purge = 'off'"))


def test_nothing_is_cascade_deleted(db_session: Session, a: dict[str, uuid.UUID]) -> None:
    db_session.execute(text(RECORD), _record_params(a))
    for sql in (
        "DELETE FROM attendance_sessions WHERE id = :i",
        "DELETE FROM student_enrollments WHERE id = :e",
        "DELETE FROM students WHERE id = :s",
    ):
        _violates(
            db_session,
            sql,
            {"i": a["session"], "e": a["enrollment_student"], "s": a["student"]},
            pg.ForeignKeyViolation,
        )
