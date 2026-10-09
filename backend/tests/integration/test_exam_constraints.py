"""Database-level exam integrity, exercised with raw SQL that bypasses every service
check. PostgreSQL itself (constraints and triggers) must refuse invalid data."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import SCHOOL_A, SCHOOL_B

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


def _row(session: Session, sql: str, **params: object) -> uuid.UUID:
    new_id = uuid.uuid4()
    session.execute(text(sql), {"id": new_id, **params})
    return new_id


def _world(session: Session, school: uuid.UUID, tag: str) -> Ids:
    def row(sql: str, **params: object) -> uuid.UUID:
        return _row(session, sql, s=school, **params)

    ids: Ids = {
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
        "subject2": row(
            "INSERT INTO subjects (id, school_id, name, code) VALUES (:id, :s, 'S', :c)",
            c=f"S-{tag}",
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
    for key, cls, section in (
        ("student", "class", "section"),
        ("student_b", "class", "section_b"),
        ("student2", "class2", "section2"),
    ):
        ids[key] = row(
            "INSERT INTO students (id, school_id, admission_number, first_name, last_name) "
            "VALUES (:id, :s, :adm, 'S', 'T')",
            adm=f"{key}-{tag}".upper(),
        )
        ids[f"enrollment_{key}"] = row(
            "INSERT INTO student_enrollments (id, school_id, student_id, academic_year_id, "
            "class_id, section_id, enrollment_date) "
            "VALUES (:id, :s, :st, :y, :c, :sec, '2026-06-10')",
            st=ids[key],
            y=ids["year"],
            c=ids[cls],
            sec=ids[section],
        )
    ids["exam"] = _exam(session, school, ids)
    ids["component"] = _component(session, school, ids)
    return ids


EXAM = (
    "INSERT INTO exams (id, school_id, academic_year_id, class_id, section_id, name, exam_type, "
    "start_date, end_date, status, created_by_user_id) VALUES (:id, :school, :year, :class, "
    ":section, :name, 'midterm', '2026-09-01', '2026-09-10', :status, :user)"
)


def _exam(session: Session, school: uuid.UUID, base: Ids, **override: object) -> uuid.UUID:
    params: dict[str, object] = {
        "id": uuid.uuid4(),
        "school": school,
        "year": base["year"],
        "class": base["class"],
        "section": None,
        "name": f"Exam {uuid.uuid4().hex[:6]}",
        "status": "draft",
        "user": base["user"],
    }
    params.update(override)
    session.execute(text(EXAM), params)
    return params["id"]  # type: ignore[return-value]


COMPONENT = (
    "INSERT INTO exam_subjects (id, school_id, exam_id, subject_id, max_marks, pass_marks, "
    "created_by_user_id) VALUES (:id, :school, :exam, :subject, :max, :pass, :user)"
)


def _component_params(school: uuid.UUID, base: Ids, **override: object) -> dict[str, object]:
    params: dict[str, object] = {
        "id": uuid.uuid4(),
        "school": school,
        "exam": base["exam"],
        "subject": base["subject"],
        "max": 100,
        "pass": 35,
        "user": base["user"],
    }
    params.update(override)
    return params


def _component(session: Session, school: uuid.UUID, base: Ids, **override: object) -> uuid.UUID:
    params = _component_params(school, base, **override)
    session.execute(text(COMPONENT), params)
    return params["id"]  # type: ignore[return-value]


def _status(session: Session, exam: uuid.UUID, status: str) -> None:
    session.execute(text("UPDATE exams SET status = :st WHERE id = :e"), {"st": status, "e": exam})


MARK = (
    "INSERT INTO exam_marks (id, school_id, exam_id, exam_subject_id, max_marks, student_id, "
    "enrollment_id, academic_year_id, class_id, section_id, status, marks_obtained, "
    "entered_by_user_id, entered_at) VALUES (:id, :school, :exam, :component, :max, :student, "
    ":enrollment, :year, :class, :section, :status, :marks, :user, now())"
)


def _mark_params(base: Ids, school: uuid.UUID = SCHOOL_A, **override: object) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(),
        "school": school,
        "exam": base["exam"],
        "component": base["component"],
        "max": 100,
        "student": base["student"],
        "enrollment": base["enrollment_student"],
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "status": "present",
        "marks": 50,
        "user": base["user"],
    }
    params.update(override)
    return params


@pytest.fixture
def ids(db_session: Session) -> dict[str, Ids]:
    return {"a": _world(db_session, SCHOOL_A, "A"), "b": _world(db_session, SCHOOL_B, "B")}


def _open(session: Session, exam: uuid.UUID) -> None:
    _status(session, exam, "scheduled")
    _status(session, exam, "marks_entry")


# ===================================================================== exams
@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"year": ("b", "year")}, "fk_exams_year_same_school"),
        ({"class": ("b", "class")}, "fk_exams_class_same_school"),
        ({"section": ("b", "section")}, "fk_exams_section_same_school"),
        ({"section": ("a", "section2")}, "fk_exams_section_in_class"),
    ],
)
def test_exam_references_stay_in_school(
    db_session: Session, ids: dict[str, Ids], override: dict[str, tuple[str, str]], constraint: str
) -> None:
    a = ids["a"]
    params = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "year": a["year"],
        "class": a["class"],
        "section": None,
        "name": "X",
        "status": "draft",
        "user": a["user"],
    }
    params.update({k: ids[w][key] for k, (w, key) in override.items()})
    _violates(db_session, EXAM, params, pg.ForeignKeyViolation, constraint)


def test_exam_checks(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    base = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "year": a["year"],
        "class": a["class"],
        "section": None,
        "user": a["user"],
    }
    _violates(
        db_session,
        EXAM,
        {**base, "name": "X", "status": "published"},
        pg.CheckViolation,
        "exams_lifecycle",
    )
    _violates(
        db_session,
        EXAM,
        {**base, "name": " ", "status": "draft"},
        pg.CheckViolation,
        "ck_exams_name_not_blank",
    )
    _violates(
        db_session,
        "UPDATE exams SET pass_percentage = 100.01 WHERE id = :e",
        {"e": a["exam"]},
        pg.CheckViolation,
        "ck_exams_pass_percentage_range",
    )
    _violates(
        db_session,
        "UPDATE exams SET end_date = '2026-08-01' WHERE id = :e",
        {"e": a["exam"]},
        pg.CheckViolation,
        "ck_exams_dates_ordered",
    )
    _violates(
        db_session,
        "UPDATE exams SET status = 'cancelled' WHERE id = :e",
        {"e": a["exam"]},
        pg.CheckViolation,
        "ck_exams_cancellation_complete",
    )


def test_duplicate_exam_names(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    _exam(db_session, SCHOOL_A, a, name="Midterm")
    params = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "year": a["year"],
        "class": a["class"],
        "section": None,
        "name": "Midterm",
        "status": "draft",
        "user": a["user"],
    }
    _violates(db_session, EXAM, params, pg.UniqueViolation, "uq_exams_name_per_placement")
    # Per section, the same rule (NULLS NOT DISTINCT covers the class-wide case above).
    _exam(db_session, SCHOOL_A, a, name="UT", section=a["section"])
    _violates(
        db_session,
        EXAM,
        {**params, "id": uuid.uuid4(), "name": "UT", "section": a["section"]},
        pg.UniqueViolation,
    )
    _exam(db_session, SCHOOL_A, a, name="UT", section=a["section_b"])


@pytest.mark.parametrize(
    ("path", "target", "constraint"),
    [
        ((), "marks_entry", "exams_lifecycle"),
        ((), "published", "exams_lifecycle"),
        (("scheduled",), "published", "exams_lifecycle"),
        (("scheduled", "marks_entry"), "draft", "exams_lifecycle"),
        (("scheduled", "marks_entry"), "scheduled", "exams_lifecycle"),
        (("scheduled", "marks_entry"), "published", "exams_published_has_publication"),
    ],
)
def test_invalid_transitions(
    db_session: Session, ids: dict[str, Ids], path: tuple[str, ...], target: str, constraint: str
) -> None:
    exam = ids["a"]["exam"]
    for step in path:
        _status(db_session, exam, step)
    _violates(
        db_session,
        "UPDATE exams SET status = :st WHERE id = :e",
        {"st": target, "e": exam},
        pg.CheckViolation,
        constraint,
    )


def test_scheduling_needs_a_subject(db_session: Session, ids: dict[str, Ids]) -> None:
    empty = _exam(db_session, SCHOOL_A, ids["a"])
    _violates(
        db_session,
        "UPDATE exams SET status = 'scheduled' WHERE id = :e",
        {"e": empty},
        pg.CheckViolation,
        "exams_scheduled_has_subjects",
    )


def test_cancelled_and_placement_are_immutable(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    _violates(
        db_session,
        "UPDATE exams SET class_id = :c WHERE id = :e",
        {"c": a["class2"], "e": a["exam"]},
        pg.CheckViolation,
        "exams_placement_immutable",
    )
    db_session.execute(
        text(
            "UPDATE exams SET status = 'cancelled', cancelled_at = now(), "
            "cancelled_by_user_id = :u, cancellation_reason = 'x' WHERE id = :e"
        ),
        {"u": a["user"], "e": a["exam"]},
    )
    for sql in (
        "UPDATE exams SET status = 'draft', cancelled_at = NULL, cancelled_by_user_id = NULL, "
        "cancellation_reason = NULL WHERE id = :e",
        "UPDATE exams SET name = 'Renamed' WHERE id = :e",
    ):
        _violates(db_session, sql, {"e": a["exam"]}, pg.CheckViolation, "exams_lifecycle")


# ============================================================== exam subjects
@pytest.mark.parametrize(
    ("override", "error", "constraint"),
    [
        (
            {"subject": ("b", "subject")},
            pg.ForeignKeyViolation,
            "fk_exam_subjects_subject_same_school",
        ),
        ({"exam": ("b", "exam")}, pg.ForeignKeyViolation, "fk_exam_subjects_exam_same_school"),
        ({"max": 0}, pg.CheckViolation, "ck_exam_subjects_max_marks_range"),
        ({"max": 1000.01}, pg.CheckViolation, "ck_exam_subjects_max_marks_range"),
        ({"pass": 101}, pg.CheckViolation, "ck_exam_subjects_pass_marks_range"),
        ({"pass": -1}, pg.CheckViolation, "ck_exam_subjects_pass_marks_range"),
        ({"subject": ("a", "subject")}, pg.UniqueViolation, "uq_exam_subjects_exam_id_subject_id"),
    ],
)
def test_component_constraints(
    db_session: Session, ids: dict[str, Ids], override: dict[str, Any], error: type, constraint: str
) -> None:
    resolved = {k: ids[v[0]][v[1]] if isinstance(v, tuple) else v for k, v in override.items()}
    params = _component_params(SCHOOL_A, ids["a"], subject=ids["a"]["subject2"])
    params.update(resolved)
    _violates(db_session, COMPONENT, params, error, constraint)


def test_components_frozen_outside_configuration(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    _open(db_session, a["exam"])
    _violates(
        db_session,
        COMPONENT,
        _component_params(SCHOOL_A, a, subject=a["subject2"]),
        pg.CheckViolation,
        "exam_subjects_config_frozen",
    )
    _violates(
        db_session,
        "UPDATE exam_subjects SET max_marks = 50 WHERE id = :c",
        {"c": a["component"]},
        pg.CheckViolation,
        "exam_subjects_config_frozen",
    )
    _violates(
        db_session,
        "DELETE FROM exam_subjects WHERE id = :c",
        {"c": a["component"]},
        pg.CheckViolation,
        "exam_subjects_removal_draft_only",
    )


def test_subjects_removed_only_in_draft(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    _status(db_session, a["exam"], "scheduled")
    _violates(
        db_session,
        "DELETE FROM exam_subjects WHERE id = :c",
        {"c": a["component"]},
        pg.CheckViolation,
        "exam_subjects_removal_draft_only",
    )
    # Even if a subject vanished behind the trigger's back, marks entry needs subjects.
    db_session.execute(text("ALTER TABLE exam_subjects DISABLE TRIGGER trg_exam_subjects_config"))
    db_session.execute(text("DELETE FROM exam_subjects WHERE id = :c"), {"c": a["component"]})
    db_session.execute(text("ALTER TABLE exam_subjects ENABLE TRIGGER trg_exam_subjects_config"))
    _violates(
        db_session,
        "UPDATE exams SET status = 'marks_entry' WHERE id = :e",
        {"e": a["exam"]},
        pg.CheckViolation,
        "exams_scheduled_has_subjects",
    )
    _status(db_session, a["exam"], "draft")
    other = _component(db_session, SCHOOL_A, a)
    db_session.execute(text("DELETE FROM exam_subjects WHERE id = :c"), {"c": other})


# ====================================================================== marks
@pytest.mark.parametrize(
    ("override", "error", "constraint"),
    [
        ({"marks": 100.01}, pg.CheckViolation, "ck_exam_marks_marks_range"),
        ({"marks": -0.01}, pg.CheckViolation, "ck_exam_marks_marks_range"),
        ({"marks": None}, pg.CheckViolation, "ck_exam_marks_marks_match_status"),
        ({"status": "absent"}, pg.CheckViolation, "ck_exam_marks_marks_match_status"),
        ({"status": "late", "marks": None}, pg.CheckViolation, "ck_exam_marks_status_valid"),
        # Claiming a bigger maximum than the component's is a broken reference.
        ({"max": 1000, "marks": 500}, pg.ForeignKeyViolation, "fk_exam_marks_component"),
        # Enrollment of another class: the placement no longer matches the exam's.
        (
            {
                "student": "student2",
                "enrollment": "enrollment_student2",
                "class": "class2",
                "section": "section2",
            },
            pg.ForeignKeyViolation,
            "fk_exam_marks_exam_placement",
        ),
        # Enrollment of another student.
        (
            {"enrollment": "enrollment_student_b"},
            pg.ForeignKeyViolation,
            "fk_exam_marks_enrollment_placement",
        ),
        # Correct student, wrong section claimed.
        ({"section": "section_b"}, pg.ForeignKeyViolation, "fk_exam_marks_enrollment_placement"),
    ],
)
def test_mark_constraints(
    db_session: Session, ids: dict[str, Ids], override: dict[str, Any], error: type, constraint: str
) -> None:
    a = ids["a"]
    _open(db_session, a["exam"])
    resolved = {k: a[v] if isinstance(v, str) and v in a else v for k, v in override.items()}
    _violates(db_session, MARK, _mark_params(a, **resolved), error, constraint)


@pytest.mark.parametrize(
    ("override", "constraint"),
    [
        ({"school": SCHOOL_B}, "fk_exam_marks_component"),
        (
            {"student": "student", "enrollment": "enrollment_student"},
            "fk_exam_marks_enrollment_placement",
        ),
    ],
)
def test_marks_cannot_cross_schools(
    db_session: Session, ids: dict[str, Ids], override: dict[str, Any], constraint: str
) -> None:
    a, b = ids["a"], ids["b"]
    _open(db_session, a["exam"])
    resolved = {k: b[v] if isinstance(v, str) else v for k, v in override.items()}
    _violates(db_session, MARK, _mark_params(a, **resolved), pg.ForeignKeyViolation, constraint)


def test_marks_follow_the_exam_lifecycle_and_section(
    db_session: Session, ids: dict[str, Ids]
) -> None:
    a = ids["a"]
    _violates(db_session, MARK, _mark_params(a), pg.CheckViolation, "exam_marks_exam_accepts_marks")
    _status(db_session, a["exam"], "scheduled")
    _violates(db_session, MARK, _mark_params(a), pg.CheckViolation, "exam_marks_exam_accepts_marks")
    # A section exam only accepts its own section's students.
    section_exam = _exam(db_session, SCHOOL_A, a, section=a["section"])
    component = _component(db_session, SCHOOL_A, a, exam=section_exam)
    _open(db_session, section_exam)
    params = _mark_params(
        a,
        exam=section_exam,
        component=component,
        student=a["student_b"],
        enrollment=a["enrollment_student_b"],
        section=a["section_b"],
    )
    _violates(db_session, MARK, params, pg.CheckViolation, "exam_marks_section_in_exam")
    db_session.execute(text(MARK), _mark_params(a, exam=section_exam, component=component))


def test_marks_are_unique_immutable_and_never_deleted(
    db_session: Session, ids: dict[str, Ids]
) -> None:
    a = ids["a"]
    _open(db_session, a["exam"])
    params = _mark_params(a)
    db_session.execute(text(MARK), params)
    _violates(
        db_session,
        MARK,
        {**params, "id": uuid.uuid4()},
        pg.UniqueViolation,
        "uq_exam_marks_exam_subject_id_student_id",
    )
    _violates(
        db_session,
        "UPDATE exam_marks SET student_id = :s, enrollment_id = :e, section_id = :sec "
        "WHERE id = :m",
        {
            "s": a["student_b"],
            "e": a["enrollment_student_b"],
            "sec": a["section_b"],
            "m": params["id"],
        },
        pg.CheckViolation,
        "exam_marks_immutable",
    )
    _violates(
        db_session,
        "DELETE FROM exam_marks WHERE id = :m",
        {"m": params["id"]},
        pg.RestrictViolation,
    )
    _violates(
        db_session,
        "UPDATE exam_subjects SET max_marks = 50 WHERE id = :c",
        {"c": a["component"]},
        pg.CheckViolation,
    )
    # Changing a mark after publication is refused.
    db_session.execute(
        text("UPDATE exam_marks SET marks_obtained = 60 WHERE id = :m"), {"m": params["id"]}
    )
    _publish(db_session, a)
    _violates(
        db_session,
        "UPDATE exam_marks SET marks_obtained = 70 WHERE id = :m",
        {"m": params["id"]},
        pg.CheckViolation,
        "exam_marks_exam_accepts_marks",
    )
    db_session.execute(text("SET LOCAL app.exam_history_purge = 'on'"))
    db_session.execute(text("DELETE FROM exam_report_cards WHERE exam_id = :e"), {"e": a["exam"]})
    db_session.execute(text("DELETE FROM exam_marks WHERE id = :m"), {"m": params["id"]})


# ==================================================================== history
CHANGE = (
    "INSERT INTO exam_mark_changes (id, school_id, mark_id, sequence, previous_status, "
    "new_status, previous_marks, new_marks, reason, changed_by_user_id) VALUES (:id, :school, "
    ":mark, :seq, 'present', 'present', 50, 60, :reason, :user)"
)


def test_mark_history_is_append_only(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    _open(db_session, a["exam"])
    mark = _mark_params(a)
    db_session.execute(text(MARK), mark)
    params = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "mark": mark["id"],
        "seq": 1,
        "reason": "Fix",
        "user": a["user"],
    }
    db_session.execute(text(CHANGE), params)
    _violates(
        db_session,
        CHANGE,
        {**params, "id": uuid.uuid4()},
        pg.UniqueViolation,
        "uq_exam_mark_changes_mark_id_sequence",
    )
    _violates(
        db_session,
        CHANGE,
        {**params, "id": uuid.uuid4(), "seq": 2, "reason": " "},
        pg.CheckViolation,
        "ck_exam_mark_changes_reason_not_blank",
    )
    _violates(
        db_session,
        CHANGE,
        {**params, "id": uuid.uuid4(), "seq": 2, "reason": None},
        pg.NotNullViolation,
    )
    _violates(
        db_session,
        CHANGE,
        {**params, "id": uuid.uuid4(), "seq": 2, "school": SCHOOL_B},
        pg.ForeignKeyViolation,
        "fk_exam_mark_changes_mark_same_school",
    )
    for sql in (
        "UPDATE exam_mark_changes SET reason = 'Edited' WHERE id = :c",
        "DELETE FROM exam_mark_changes WHERE id = :c",
    ):
        _violates(db_session, sql, {"c": params["id"]}, pg.RestrictViolation)


# ======================================================= publications / report cards
PUBLICATION = (
    "INSERT INTO exam_publications (id, school_id, exam_id, version, status, settings, "
    "published_by_user_id, published_at) VALUES (:id, :school, :exam, :version, :status, "
    "'{}'::jsonb, :user, now())"
)
CARD = (
    "INSERT INTO exam_report_cards (id, school_id, publication_id, exam_id, student_id, "
    "enrollment_id, academic_year_id, class_id, section_id, total_obtained, total_max, "
    "percentage, result, subjects) VALUES (:id, :school, :publication, :exam, :student, "
    ":enrollment, :year, :class, :section, :obtained, :max, :pct, :result, "
    "CAST(:subjects AS jsonb))"
)


def _card_params(base: Ids, publication: uuid.UUID, **override: object) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "publication": publication,
        "exam": base["exam"],
        "student": base["student"],
        "enrollment": base["enrollment_student"],
        "year": base["year"],
        "class": base["class"],
        "section": base["section"],
        "obtained": 50,
        "max": 100,
        "pct": 50,
        "result": "pass",
        "subjects": json.dumps([]),
    }
    params.update(override)
    return params


def _publish(session: Session, base: Ids) -> uuid.UUID:
    publication = _row(
        session,
        PUBLICATION,
        school=SCHOOL_A,
        exam=base["exam"],
        version=1,
        status="active",
        user=base["user"],
    )
    session.execute(text(CARD), _card_params(base, publication))
    _status(session, base["exam"], "published")
    return publication


def test_publications(db_session: Session, ids: dict[str, Ids]) -> None:
    a = ids["a"]
    params = {
        "id": uuid.uuid4(),
        "school": SCHOOL_A,
        "exam": a["exam"],
        "version": 1,
        "status": "active",
        "user": a["user"],
    }
    _violates(
        db_session, PUBLICATION, params, pg.CheckViolation, "exam_publications_lifecycle"
    )  # draft
    _open(db_session, a["exam"])
    _violates(
        db_session,
        PUBLICATION,
        {**params, "status": "withdrawn"},
        pg.CheckViolation,
        "exam_publications_lifecycle",
    )
    _violates(
        db_session,
        PUBLICATION,
        {**params, "school": SCHOOL_B},
        pg.ForeignKeyViolation,
        "fk_exam_publications_exam_same_school",
    )
    db_session.execute(text(PUBLICATION), params)
    _violates(
        db_session,
        PUBLICATION,
        {**params, "id": uuid.uuid4(), "version": 2},
        pg.UniqueViolation,
        "uq_exam_publications_one_active",
    )
    _status(db_session, a["exam"], "published")
    # The exam cannot leave "published" while the publication is active.
    _violates(
        db_session,
        "UPDATE exams SET status = 'marks_entry' WHERE id = :e",
        {"e": a["exam"]},
        pg.CheckViolation,
        "exams_published_has_publication",
    )
    for sql in (
        "UPDATE exam_publications SET version = 5 WHERE id = :p",
        "UPDATE exam_publications SET settings = '{\"forged\": true}' WHERE id = :p",
    ):
        _violates(
            db_session, sql, {"p": params["id"]}, pg.CheckViolation, "exam_publications_lifecycle"
        )
    _violates(
        db_session,
        "UPDATE exam_publications SET status = 'withdrawn' WHERE id = :p",
        {"p": params["id"]},
        pg.CheckViolation,
        "ck_exam_publications_withdrawal_complete",
    )
    _violates(
        db_session,
        "DELETE FROM exam_publications WHERE id = :p",
        {"p": params["id"]},
        pg.RestrictViolation,
    )
    db_session.execute(
        text(
            "UPDATE exam_publications SET status = 'withdrawn', withdrawn_at = now(), "
            "withdrawn_by_user_id = :u, withdrawal_reason = 'Fix' WHERE id = :p"
        ),
        {"u": a["user"], "p": params["id"]},
    )
    _status(db_session, a["exam"], "marks_entry")
    _violates(
        db_session,
        "UPDATE exam_publications SET status = 'active', withdrawn_at = NULL, "
        "withdrawn_by_user_id = NULL, withdrawal_reason = NULL WHERE id = :p",
        {"p": params["id"]},
        pg.CheckViolation,
        "exam_publications_lifecycle",
    )
    _violates(
        db_session,
        PUBLICATION,
        {**params, "id": uuid.uuid4()},
        pg.UniqueViolation,
        "uq_exam_publications_exam_id_version",
    )


def test_report_cards_are_frozen(db_session: Session, ids: dict[str, Ids]) -> None:
    a, b = ids["a"], ids["b"]
    _open(db_session, a["exam"])
    publication = _row(
        db_session,
        PUBLICATION,
        school=SCHOOL_A,
        exam=a["exam"],
        version=1,
        status="active",
        user=a["user"],
    )
    for override, error, constraint in (
        ({"pct": 100.01}, pg.CheckViolation, "ck_exam_report_cards_percentage_range"),
        ({"obtained": 101}, pg.CheckViolation, "ck_exam_report_cards_totals_range"),
        ({"result": "distinction"}, pg.CheckViolation, "ck_exam_report_cards_result_valid"),
        (
            {"enrollment": b["enrollment_student"], "student": b["student"]},
            pg.ForeignKeyViolation,
            "fk_exam_report_cards_enrollment_placement",
        ),
        (
            {
                "class": a["class2"],
                "section": a["section2"],
                "student": a["student2"],
                "enrollment": a["enrollment_student2"],
            },
            pg.ForeignKeyViolation,
            "fk_exam_report_cards_exam_placement",
        ),
    ):
        _violates(db_session, CARD, _card_params(a, publication, **override), error, constraint)
    card = _card_params(a, publication)
    db_session.execute(text(CARD), card)
    _violates(
        db_session,
        CARD,
        {**card, "id": uuid.uuid4()},
        pg.UniqueViolation,
        "uq_exam_report_cards_publication_id_student_id",
    )
    _status(db_session, a["exam"], "published")
    # Nothing can be added to, changed in or removed from a published snapshot.
    _violates(
        db_session,
        CARD,
        _card_params(
            a,
            publication,
            student=a["student_b"],
            enrollment=a["enrollment_student_b"],
            section=a["section_b"],
        ),
        pg.CheckViolation,
        "exam_report_cards_frozen",
    )
    for sql in (
        "UPDATE exam_report_cards SET result = 'fail' WHERE id = :c",
        "DELETE FROM exam_report_cards WHERE id = :c",
    ):
        _violates(db_session, sql, {"c": card["id"]}, pg.RestrictViolation)
