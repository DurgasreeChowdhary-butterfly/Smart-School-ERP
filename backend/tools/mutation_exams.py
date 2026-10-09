"""Phase 7 mutation testing: break one protection at a time, run the tests that should
catch it, restore. A mutant is *killed* when those tests fail.

Run from ``backend/`` with the dev dependencies installed and the test database up::

    python tools/mutation_exams.py            # every mutant
    python tools/mutation_exams.py --list     # names only
    python tools/mutation_exams.py --only lock

Safety:

* Database mutants drop a constraint/trigger in the **test** database only: the same
  database the test suite uses (``TEST_POSTGRES_DB``, default ``<POSTGRES_DB>_test``).
  The tool refuses any name that does not end in ``_test``. The exam schema is rebuilt
  afterwards (exam tables only; the test database keeps no data between tests).
* Code mutants edit one source file at a time. The original is first copied to
  ``<file>.mutation-backup`` and restored afterwards; if a previous run was killed, the
  next run restores any leftover backup and stops, so a mutant can never be left behind.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

from app.config import Settings
from app.database import build_engine

BACKEND = Path(__file__).resolve().parent.parent
T = "tests/integration/"
BACKUP = ".mutation-backup"


@dataclass
class Mutant:
    name: str
    tests: list[str]
    file: str | None = None
    old: str = ""
    new: str = ""
    sql: str | None = None
    note: str = field(default="")


MUTANTS = [
    # ------------------------------------------------ faculty subject/section/date scope
    Mutant(
        "faculty write scope ignores the subject",
        [T + "test_exam_marks_api.py::test_faculty_enter_marks_only_for_their_subject_and_section"],
        "app/security/exam_scope.py",
        "            subject_id=component.subject_id,\n            on_date=assessment_date",
        "            subject_id=None,\n            on_date=assessment_date",
    ),
    Mutant(
        "faculty read scope ignores the subject",
        [T + "test_exam_marks_api.py::test_faculty_read_scope"],
        "app/security/exam_scope.py",
        "                subject_id=C.subject_id,",
        "                subject_id=None,",
    ),
    Mutant(
        "faculty read scope ignores the student's section",
        [T + "test_exam_marks_api.py::test_faculty_read_scope"],
        "app/security/exam_scope.py",
        "                section_id=M.section_id,",
        "                section_id=FA.section_id,",
    ),
    Mutant(
        "faculty write scope ignores the section",
        [T + "test_exam_marks_api.py::test_faculty_enter_marks_only_for_their_subject_and_section"],
        "app/security/exam_scope.py",
        "    if exam.section_id is not None:\n"
        "        stmt = stmt.where(Section.id == exam.section_id)\n"
        "    return set(session.scalars(stmt))",
        "    return None",
    ),
    Mutant(
        "faculty assignment judged on today, not the assessment date",
        [
            T + "test_exam_marks_api.py::"
            "test_faculty_assignment_must_be_effective_on_the_assessment_date"
        ],
        "app/security/exam_scope.py",
        "            on_date=assessment_date(exam, component),",
        "            on_date=func.current_date(),",
    ),
    Mutant(
        "exam visibility: faculty see every exam of the school",
        [T + "test_exam_authorization.py::test_faculty_cannot_widen_their_scope"],
        "app/security/exam_scope.py",
        "        return and_(in_school, teaches_class)",
        "        return in_school",
    ),
    # ------------------------------------------------------------ parent / student scope
    Mutant(
        "report cards: parents/students see every student's",
        [
            T + "test_exam_results_api.py::test_parent_and_student_report_card_visibility",
            T + "test_exam_authorization.py::test_parent_cannot_reach_another_childs_results",
        ],
        "app/security/exam_scope.py",
        "        ExamReportCard.student_id.in_(select(Student.id).where(personal)),",
        "        ExamReportCard.school_id == school_id,",
    ),
    Mutant(
        "student-self: a student reaches the whole school (personal scope)",
        [T + "test_exam_results_api.py::test_parent_and_student_report_card_visibility"],
        "app/security/people_scope.py",
        "    if principal.has_permission(PermissionName.STUDENT_READ_SELF):\n"
        "        persona.append(Student.user_id == principal.user_id)\n"
        "    if not persona:\n"
        "        return false()\n"
        "    return and_(Student.school_id == school_id, or_(*persona))",
        "    if principal.has_permission(PermissionName.STUDENT_READ_SELF):\n"
        "        persona.append(Student.school_id == school_id)\n"
        "    if not persona:\n"
        "        return false()\n"
        "    return and_(Student.school_id == school_id, or_(*persona))",
    ),
    Mutant(
        "published-only: parents see withdrawn publications",
        [T + "test_exam_results_api.py::test_reopen_correct_and_republish_keeps_history"],
        "app/security/exam_scope.py",
        "        ExamPublication.status == PublicationStatus.ACTIVE.value,\n",
        "",
    ),
    Mutant(
        "unpublished: parents/students can see exams",
        [T + "test_exam_marks_api.py::test_parents_and_students_cannot_enter_or_read_marks"],
        "app/security/exam_scope.py",
        "        return and_(in_school, teaches_class)\n    return false()",
        "        return and_(in_school, teaches_class)\n    return in_school",
    ),
    Mutant(
        "unpublished: results preview open to any marks.read holder",
        [T + "test_exam_results_api.py::test_results_are_for_school_staff"],
        "app/services/exam_results.py",
        "        exam = self.visible_exam(scope, exam_id)\n"
        "        require_school_staff(scope, STAFF_ONLY)\n"
        "        outcomes = self.calculate(exam)",
        "        exam = self.visible_exam(scope, exam_id)\n        outcomes = self.calculate(exam)",
    ),
    # ------------------------------------------------------------- cross-school integrity
    Mutant(
        "service: component subject looked up without the school",
        [
            T + "test_exams_api.py::test_components_configuration",
            T + "test_exams_api.py::test_other_schools_subjects_are_indistinguishable_from_unknown",
        ],
        "app/services/exams.py",
        "        subject = self.subject_catalog.get(scope.school_id, data.subject_id)",
        "        from app.models.academic import Subject\n\n"
        "        subject = self.session.get(Subject, data.subject_id)",
    ),
    Mutant(
        "service: eligibility not limited to the exam's school",
        [T + "test_exam_marks_api.py::test_eligibility"],
        "app/repositories/exams.py",
        "        EN.school_id == exam.school_id,\n",
        "",
        note="equivalent: the exam's academic year pins the school "
        "(fk_enrollments_year_same_school)",
    ),
    Mutant(
        "db: drop fk_exam_marks_enrollment_placement",
        [T + "test_exam_constraints.py"],
        sql="ALTER TABLE exam_marks DROP CONSTRAINT fk_exam_marks_enrollment_placement",
    ),
    Mutant(
        "db: drop fk_exams_class_same_school",
        [T + "test_exam_constraints.py"],
        sql="ALTER TABLE exams DROP CONSTRAINT fk_exams_class_same_school",
    ),
    Mutant(
        "db: drop fk_exam_subjects_subject_same_school",
        [T + "test_exam_constraints.py"],
        sql="ALTER TABLE exam_subjects DROP CONSTRAINT fk_exam_subjects_subject_same_school",
    ),
    # ----------------------------------------------------------------- maximum marks
    Mutant(
        "service: maximum marks not checked",
        [
            T + "test_exam_marks_api.py::test_maximum_is_per_component",
            T + "test_exam_marks_api.py::test_bulk_is_all_or_nothing",
        ],
        "app/services/exam_marks.py",
        "    return value is not None and value > component.max_marks",
        "    return False",
    ),
    Mutant(
        "db: drop ck_exam_marks_marks_range",
        [T + "test_exam_constraints.py"],
        sql="ALTER TABLE exam_marks DROP CONSTRAINT ck_exam_marks_marks_range",
    ),
    Mutant(
        "db: drop fk_exam_marks_component (max no longer tied to the component)",
        [T + "test_exam_constraints.py"],
        sql="ALTER TABLE exam_marks DROP CONSTRAINT fk_exam_marks_component",
    ),
    # ------------------------------------------------------------------ duplicates
    Mutant(
        "service: no pre-check for an existing mark",
        [T + "test_exam_marks_api.py::test_duplicate_entry_and_state_gating"],
        "app/services/exam_marks.py",
        "        if self.marks.students_with_marks(component.id, [entry.student_id]):",
        "        if False:",
        note="equivalent at the API: the unique constraint returns the same 409 and location",
    ),
    Mutant(
        "db: drop uq_exam_marks_exam_subject_id_student_id",
        [
            T + "test_exam_constraints.py",
            T + "test_exam_concurrency.py::test_raw_sql_duplicate_mark_race",
        ],
        sql="ALTER TABLE exam_marks DROP CONSTRAINT uq_exam_marks_exam_subject_id_student_id",
    ),
    # ------------------------------------------------------- history / configuration
    Mutant(
        "service: correction history row not written",
        [T + "test_exam_marks_api.py::test_correction_history"],
        "app/services/exam_marks.py",
        "            self.session.add(change)\n            mark.status = new_status",
        "            mark.status = new_status",
    ),
    Mutant(
        "db: history no longer append-only",
        [T + "test_exam_constraints.py::test_mark_history_is_append_only"],
        sql="DROP TRIGGER trg_exam_mark_changes_append_only ON exam_mark_changes",
    ),
    Mutant(
        "service: subjects removable outside draft",
        [T + "test_exams_api.py::test_subject_removal_is_draft_only"],
        "app/services/exams.py",
        "        if exam.status != ExamStatus.DRAFT:\n            raise ConflictError(\n"
        '                "Subjects can only be removed',
        "        if False:\n            raise ConflictError(\n"
        '                "Subjects can only be removed',
    ),
    Mutant(
        "service: subject with marks can be removed",
        [T + "test_exams_api.py::test_subject_with_marks_is_never_removed"],
        "app/services/exams.py",
        "        if self.marks.exists_for_component(item.id):",
        "        if False:",
    ),
    Mutant(
        "db: marks accepted in any exam status",
        [T + "test_exam_constraints.py"],
        sql="DROP TRIGGER trg_exam_marks_guard ON exam_marks",
    ),
    Mutant(
        "service: marks accepted outside marks entry",
        [
            T + "test_exam_marks_api.py::test_duplicate_entry_and_state_gating",
            T + "test_exam_results_api.py::test_publish_freezes_report_cards",
        ],
        "app/services/exam_marks.py",
        "        if exam.status != ExamStatus.MARKS_ENTRY:\n"
        "            raise ConflictError(\n"
        '                f"Marks can only',
        '        if False:\n            raise ConflictError(\n                f"Marks can only',
    ),
    # ------------------------------------------------------------------------ locks
    Mutant(
        "lock: publication does not lock the exam",
        [
            T
            + "test_exam_concurrency.py::test_publication_snapshot_includes_a_write_it_waited_for",
            T + "test_exam_concurrency.py::test_publication_waits_for_marks_writers",
        ],
        "app/services/exam_results.py",
        "    ) -> PublicationRead:\n"
        '        exam = self.visible_exam(scope, exam_id, lock="update")\n'
        "        require_school_staff(scope, STAFF_ONLY)\n"
        "        if exam.status != ExamStatus.MARKS_ENTRY:",
        "    ) -> PublicationRead:\n"
        "        exam = self.visible_exam(scope, exam_id)\n"
        "        require_school_staff(scope, STAFF_ONLY)\n"
        "        if exam.status != ExamStatus.MARKS_ENTRY:",
    ),
    Mutant(
        "lock: marks writers do not share-lock the exam",
        [T + "test_exam_concurrency.py::test_marks_writers_wait_for_publication"],
        "app/services/exam_marks.py",
        '        exam = self.visible_exam(scope, exam_id, lock="share")',
        "        exam = self.visible_exam(scope, exam_id)",
    ),
]

EXAM_TABLES = (
    "exam_report_cards",
    "exam_publications",
    "exam_mark_changes",
    "exam_marks",
    "exam_subjects",
    "exams",
)
EXAM_FUNCTIONS = (
    "exams_lifecycle_guard",
    "exam_subjects_config_guard",
    "exam_marks_guard",
    "exam_history_append_only",
    "exam_report_cards_insert_guard",
    "exam_publications_guard",
)


def test_settings() -> Settings:
    """The test suite's database (same rule as tests/conftest.py), never anything else."""
    base = Settings()
    name = os.environ.get("TEST_POSTGRES_DB") or f"{base.postgres_db}_test"
    if not name.endswith("_test"):
        sys.exit(f"Refusing to run: database {name!r} does not end with '_test'.")
    return base.model_copy(update={"postgres_db": name})


def run_sql(settings: Settings, *statements: str) -> None:
    engine = build_engine(settings)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def rebuild_exam_schema(settings: Settings) -> None:
    """Drop the 0007 objects (IF EXISTS, so a dropped trigger cannot break this), mark the
    test database at 0006 and upgrade it to head again."""
    run_sql(
        settings,
        *(f"DROP TABLE IF EXISTS {table} CASCADE" for table in EXAM_TABLES),
        *(f"DROP FUNCTION IF EXISTS {fn}() CASCADE" for fn in EXAM_FUNCTIONS),
        "UPDATE alembic_version SET version_num = '0006_attendance'",
    )
    import logging

    from alembic import command

    logging.getLogger("alembic").setLevel(logging.WARNING)

    from tests.conftest import alembic_config

    engine = build_engine(settings)
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
    finally:
        engine.dispose()


def tests_pass(tests: list[str]) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _patterns(mutant: Mutant, text_: str) -> tuple[str, str]:
    # Match the file's line endings (Windows checkouts may use CRLF).
    eol = "\r\n" if "\r\n" in text_ else "\n"
    return mutant.old.replace("\n", eol), mutant.new.replace("\n", eol)


def recover_backups() -> bool:
    leftovers = list((BACKEND / "app").rglob(f"*{BACKUP}"))
    for backup in leftovers:
        original = backup.with_name(backup.name.removesuffix(BACKUP))
        original.write_bytes(backup.read_bytes())
        backup.unlink()
        print(f"restored {original.relative_to(BACKEND)} from an interrupted run")
    return bool(leftovers)


def run_mutant(mutant: Mutant, settings: Settings) -> bool:
    """Return True when the mutant survived."""
    if mutant.sql:
        run_sql(settings, mutant.sql)
        try:
            return tests_pass(mutant.tests)
        finally:
            rebuild_exam_schema(settings)
    path = BACKEND / (mutant.file or "")
    original = path.read_bytes()
    backup = path.with_name(path.name + BACKUP)
    backup.write_bytes(original)
    old, new = _patterns(mutant, original.decode())
    path.write_bytes(original.decode().replace(old, new, 1).encode())
    try:
        return tests_pass(mutant.tests)
    finally:
        path.write_bytes(original)
        backup.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--list", action="store_true", help="list mutants and exit")
    parser.add_argument("--only", help="run mutants whose name contains this text")
    args = parser.parse_args()
    if args.list:
        for mutant in MUTANTS:
            print(mutant.name)
        return 0
    if recover_backups():
        print("A previous run was interrupted; sources restored. Re-run to continue.")
        return 1
    selected = [m for m in MUTANTS if not args.only or args.only in m.name]
    for mutant in selected:  # validate every pattern before mutating anything
        if mutant.file:
            content = (BACKEND / mutant.file).read_bytes().decode()
            old, _ = _patterns(mutant, content)
            if content.count(old) != 1:
                print(f"pattern not found (exactly once) for: {mutant.name}")
                return 2
    settings = test_settings()
    killed = 0
    for mutant in selected:
        survived = run_mutant(mutant, settings)
        killed += not survived
        label = "SURVIVED" if survived else "killed  "
        print(f"{label} {mutant.name}" + (f"  ({mutant.note})" if survived and mutant.note else ""))
    print(f"\n{killed}/{len(selected)} mutants killed")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    sys.exit(main())
