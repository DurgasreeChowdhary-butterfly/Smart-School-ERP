"""Phase 8 mutation testing: break one assignment/submission protection at a time, run
the tests that should catch it, restore. Same safety rules as ``mutation_exams.py``
(test database only, name must end in ``_test``; file backups with crash recovery).

Run from ``backend/``::

    python tools/mutation_assignments.py            # every mutant
    python tools/mutation_assignments.py --list
    python tools/mutation_assignments.py --only db
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

from mutation_exams import (
    BACKEND,
    Mutant,
    _patterns,
    build_engine,
    recover_backups,
    run_sql,
    test_settings,
    tests_pass,
)

from app.config import Settings

T = "tests/integration/"
API = T + "test_assignments_api.py::"
SUB = T + "test_assignment_submissions_api.py::"
AUTH = T + "test_assignment_authorization.py::"
RACE = T + "test_assignment_concurrency.py::"
SCOPE = "app/security/assignment_scope.py"
SUBMISSIONS = "app/services/assignment_submissions.py"
FACULTY_MODEL = '__import__("app.models.faculty", fromlist=["FacultyAssignment"]).FacultyAssignment'

MUTANTS = [
    # ------------------------------------------------------------- faculty scope
    Mutant(
        "faculty scope ignores the subject",
        [API + "test_faculty_manage_only_their_assignments", API + "test_list_filters_and_scope"],
        SCOPE,
        "        subject_id=A.subject_id,",
        "        subject_id=None,",
    ),
    Mutant(
        "faculty scope ignores the section",
        [API + "test_faculty_manage_only_their_assignments"],
        SCOPE,
        "        section_id=A.section_id,\n        subject_id=A.subject_id,",
        f"        section_id={FACULTY_MODEL}.section_id,\n        subject_id=A.subject_id,",
    ),
    Mutant(
        "faculty create ignores the assignment date",
        [API + "test_assignment_must_be_effective_on_the_assigned_date"],
        "app/services/assignments.py",
        "                    on_date=data.assigned_on,",
        '                    on_date=__import__("sqlalchemy").func.current_date(),',
    ),
    # ---------------------------------------------------------- personal scope
    Mutant(
        "drafts visible to students and parents",
        [API + "test_drafts_are_hidden_from_students_and_parents"],
        SCOPE,
        "                A.status.in_(VISIBLE_TO_STUDENTS),\n",
        "",
    ),
    Mutant(
        "assignment visibility: any student's enrollment counts (the bug found in testing)",
        [API + "test_students_see_only_their_sections_assignments"],
        SCOPE,
        "            E.student_id.in_(students),\n",
        "",
    ),
    Mutant(
        "student-self: a student reaches the whole school (personal scope)",
        [SUB + "test_personal_visibility"],
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
        "submissions: parents/students see every student's",
        [SUB + "test_personal_visibility"],
        SCOPE,
        "        parts.append(\n            SUB.student_id.in_(",
        "        parts.append(SUB.school_id == school_id)\n  "
        "      parts.append(\n            SUB.student_id.in_(",
    ),
    Mutant(
        "parents read the submitted work",
        [SUB + "test_personal_visibility"],
        SCOPE,
        "    return submission_student_user is not None and"
        " submission_student_user == principal.user_id",
        "    return True",
    ),
    Mutant(
        "dual role: submitted text follows the faculty role, not the assignment",
        [AUTH + "test_faculty_parent_reviews_only_through_faculty_scope"],
        SCOPE,
        "    if staff_reach:\n        return True",
        "    if is_school_wide(principal) or is_faculty(principal):\n        return True",
    ),
    Mutant(
        "dual role: roster reachable through a parent relationship",
        [AUTH + "test_faculty_parent_reviews_only_through_faculty_scope"],
        "app/services/assignments.py",
        "            assignment_staff_reach(scope.principal, scope.school_id), item.id",
        "            assignment_visibility(scope.principal, scope.school_id), item.id",
    ),
    # --------------------------------------------------------------- eligibility
    Mutant(
        "eligibility ignores the date (any past enrollment in the section)",
        [SUB + "test_eligibility_follows_the_current_enrollment"],
        "app/repositories/assignments.py",
        "                enrollment_covers(on),\n",
        "",
    ),
    Mutant(
        "eligibility ignores the section",
        [SUB + "test_eligibility_follows_the_current_enrollment"],
        "app/repositories/assignments.py",
        "                E.section_id == assignment.section_id,\n"
        "                enrollment_covers(on),",
        "                enrollment_covers(on),",
        note="equivalent: fk_assignment_submissions_enrollment_placement refuses the other "
        "section's enrollment with the same 422 message",
    ),
    Mutant(
        "resubmission ownership not checked in the service",
        [
            AUTH + "test_student_cannot_act_for_another_student",
            AUTH + "test_student_parent_resubmits_only_their_own_work",
        ],
        SUBMISSIONS,
        "        if found.student_id != student.id:",
        "        if False:",
    ),
    # ---------------------------------------------------------------- deadlines
    Mutant(
        "late work accepted regardless of allow_late (service)",
        [SUB + "test_late_work_is_refused_unless_allowed"],
        SUBMISSIONS,
        "        if late and not item.allow_late:",
        "        if False:",
    ),
    Mutant(
        "resubmission allowed after review",
        [SUB + "test_resubmission_rules"],
        SUBMISSIONS,
        "        if submission.reviewed_at is not None:",
        "        if False:",
    ),
    # ----------------------------------------------------------------- grading
    Mutant(
        "grade above the maximum not checked (service)",
        [SUB + "test_invalid_grades_are_rejected"],
        SUBMISSIONS,
        "        if new_marks is not None and item.max_marks"
        " is not None and new_marks > item.max_marks:",
        "        if False:",
    ),
    Mutant(
        "marks accepted on ungraded assignments (service)",
        [SUB + "test_ungraded_assignments_take_feedback_only"],
        SUBMISSIONS,
        "        if new_marks is not None and item.max_marks is None:",
        "        if False:",
    ),
    Mutant(
        "review change without a reason (service)",
        [SUB + "test_review_and_grade_history"],
        SUBMISSIONS,
        "        if not first and data.reason is None:",
        "        if False:",
    ),
    Mutant(
        "review history row not written",
        [SUB + "test_review_and_grade_history"],
        SUBMISSIONS,
        "            self.session.add(change)\n            submission.marks_awarded = new_marks",
        "            submission.marks_awarded = new_marks",
    ),
    Mutant(
        "review scope not checked in the service",
        [SUB + "test_who_reviews", AUTH + "test_faculty_parent_reviews_only_through_faculty_scope"],
        SUBMISSIONS,
        "            raise PermissionDeniedError("
        '"You may not review submissions for this assignment.")',
        "            pass",
    ),
    # --------------------------------------------------------------- duplicates
    Mutant(
        "no pre-check for an existing submission",
        [SUB + "test_student_submits_own_work"],
        SUBMISSIONS,
        "        if self.submissions.find(item.id, student.id) is not None:",
        "        if False:",
        note="equivalent at the API: the unique constraint returns the same 409",
    ),
    Mutant(
        "db: drop uq_assignment_submissions_assignment_id_student_id",
        [T + "test_assignment_constraints.py", RACE + "test_raw_sql_duplicate_submission_race"],
        sql="ALTER TABLE assignment_submissions "
        "DROP CONSTRAINT uq_assignment_submissions_assignment_id_student_id",
    ),
    Mutant(
        "db: drop fk_assignment_submissions_enrollment_placement",
        [T + "test_assignment_constraints.py"],
        sql="ALTER TABLE assignment_submissions "
        "DROP CONSTRAINT fk_assignment_submissions_enrollment_placement",
    ),
    Mutant(
        "db: drop ck_assignment_submissions_marks_range",
        [T + "test_assignment_constraints.py"],
        sql="ALTER TABLE assignment_submissions "
        "DROP CONSTRAINT ck_assignment_submissions_marks_range",
    ),
    Mutant(
        "db: drop fk_assignments_section_same_school",
        [T + "test_assignment_constraints.py"],
        sql="ALTER TABLE assignments DROP CONSTRAINT fk_assignments_section_same_school",
    ),
    Mutant(
        "db: revisions no longer guarded",
        [T + "test_assignment_constraints.py"],
        sql="DROP TRIGGER trg_assignment_revisions_guard ON assignment_submission_revisions",
    ),
    # ----------------------------------------------------------------------- locks
    Mutant(
        "lock: submissions do not share-lock the assignment",
        [RACE + "test_submission_waits_for_a_closing_transaction"],
        SUBMISSIONS,
        '        item = self.visible_assignment(scope, assignment_id, lock="share")',
        "        item = self.visible_assignment(scope, assignment_id)",
    ),
]

TABLES = (
    "assignment_review_changes",
    "assignment_submission_revisions",
    "assignment_submissions",
    "assignments",
)
FUNCTIONS = (
    "assignments_lifecycle_guard",
    "assignment_submissions_guard",
    "assignment_revisions_guard",
    "assignment_history_append_only",
)
_ID_NAMESPACE = uuid.UUID("6f1c2a52-0b7e-4f43-9d6c-2f0d8a1e5b11")
SUBMIT_ID = uuid.uuid5(_ID_NAMESPACE, "permission:assignment.submit")


def rebuild_schema(settings: Settings) -> None:
    """Drop the 0008 objects (IF EXISTS), remove its permission, mark the test database at
    0007 and upgrade it to head again."""
    run_sql(
        settings,
        *(f"DROP TABLE IF EXISTS {table} CASCADE" for table in TABLES),
        *(f"DROP FUNCTION IF EXISTS {fn}() CASCADE" for fn in FUNCTIONS),
        f"DELETE FROM role_permissions WHERE permission_id = '{SUBMIT_ID}'",  # noqa: S608 - constant uuid5
        f"DELETE FROM permissions WHERE id = '{SUBMIT_ID}'",  # noqa: S608 - constant uuid5
        "UPDATE alembic_version SET version_num = '0007_examinations_marks'",
    )
    from alembic import command

    from tests.conftest import alembic_config

    logging.getLogger("alembic").setLevel(logging.WARNING)
    engine = build_engine(settings)
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
    finally:
        engine.dispose()


def run_mutant(mutant: Mutant, settings: Settings) -> bool:
    """Return True when the mutant survived."""
    if mutant.sql:
        run_sql(settings, mutant.sql)
        try:
            return tests_pass(mutant.tests)
        finally:
            rebuild_schema(settings)
    path = BACKEND / (mutant.file or "")
    original = path.read_bytes()
    backup = path.with_name(path.name + ".mutation-backup")
    backup.write_bytes(original)
    old, new = _patterns(mutant, original.decode())
    path.write_bytes(original.decode().replace(old, new, 1).encode())
    try:
        return tests_pass(mutant.tests)
    finally:
        path.write_bytes(original)
        backup.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 mutation testing.")
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
        note = f"  ({mutant.note})" if survived and mutant.note else ""
        print(f"{label} {mutant.name}{note}", flush=True)
    print(f"\n{killed}/{len(selected)} mutants killed")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    sys.exit(main())
