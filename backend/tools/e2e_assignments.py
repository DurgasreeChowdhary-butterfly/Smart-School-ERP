"""Phase 8 (assignments and submissions) real-HTTP E2E run against the Docker stack.

Run from the repository root while ``docker compose up -d`` is running::

    python backend/tools/e2e_assignments.py --cleanup

It reuses the guarded setup, client and scoped cleanup of ``e2e_exams.py`` (same safety
rules: refuses unless the backend reports ``APP_ENV`` development/test; creates two
fresh ``E2E7A-/E2E7B-<tag>`` schools; ``--cleanup`` deletes only the rows this run
created). Standard library only; exit code 1 if any check fails.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_exams as core

call, check, expect, login = core.call, core.check, core.expect, core.login


def _at(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def scenario() -> None:
    admin, admin_b, mgmt = login(core.ADMIN_A), login(core.ADMIN_B), login(core.MGMT)
    school_code = call("GET", "/school", admin)[1]["code"]
    tag = core.TAG

    def make(token: dict[str, str], path: str, body: dict[str, Any]) -> str:
        status, data = call("POST", path, token, body)
        if status != 201:
            raise RuntimeError(f"setup failed: {path} -> {status} {data}")
        return str(data.get("id", ""))

    ids: dict[str, str] = {}
    for key, token in (("a", admin), ("b", admin_b)):
        ids[f"year_{key}"] = make(
            token,
            "/academic-years",
            {"name": "2026-27", "start_date": "2026-06-01", "end_date": "2027-05-31"},
        )
        ids[f"class_{key}"] = make(token, "/classes", {"name": "Class 8", "code": "C8"})
        ids[f"sec_{key}"] = make(
            token, "/sections", {"class_id": ids[f"class_{key}"], "name": "A", "code": "A"}
        )
        ids[f"math_{key}"] = make(token, "/subjects", {"name": "Mathematics", "code": "MATH"})
    ids["sec_ab"] = make(admin, "/sections", {"class_id": ids["class_a"], "name": "B", "code": "B"})
    ids["sci_a"] = make(admin, "/subjects", {"name": "Science", "code": "SCI"})

    def faculty(subject: str) -> dict[str, str]:
        email = f"e2e8-{subject}-{tag.lower()}@e2e.test"
        fid = make(
            admin,
            "/faculty",
            {
                "employee_id": f"E8-{subject}-{tag}".upper()[:30],
                "first_name": "Fac",
                "last_name": subject,
                "email": email,
                "password": core.PASSWORD,
            },
        )
        make(
            admin,
            "/faculty-assignments",
            {
                "faculty_id": fid,
                "academic_year_id": ids["year_a"],
                "class_id": ids["class_a"],
                "section_id": ids["sec_a"],
                "subject_id": ids[subject],
            },
        )
        return login(email)

    f_math, g_sci = faculty("math_a"), faculty("sci_a")
    students: dict[str, str] = {}
    for key, section in (("s1", "sec_a"), ("s2", "sec_a"), ("sb", "sec_ab")):
        students[key] = make(
            admin,
            "/students",
            {
                "admission_number": f"{key}-{tag}".upper(),
                "first_name": "Stu",
                "last_name": key.upper(),
                "password": core.PASSWORD,
            },
        )
        make(
            admin,
            "/enrollments",
            {
                "student_id": students[key],
                "academic_year_id": ids["year_a"],
                "class_id": ids["class_a"],
                "section_id": ids[section],
                "enrollment_date": "2026-06-10",
                "is_current": True,
            },
        )
    s1 = login(f"s1-{tag}".upper(), school_code=school_code)
    s2 = login(f"s2-{tag}".upper(), school_code=school_code)
    sb = login(f"sb-{tag}".upper(), school_code=school_code)
    mobile = f"+9197{int(tag, 16) % 100000000:08d}"
    parent_id = make(admin, "/parents", {"first_name": "Par", "last_name": "Ent", "mobile": mobile})
    make(
        admin,
        f"/students/{students['s1']}/parents",
        {"parent_id": parent_id, "relationship_type": "father"},
    )
    parent = core.otp_login(mobile)

    def body(**extra: Any) -> dict[str, Any]:
        return {
            "academic_year_id": ids["year_a"],
            "class_id": ids["class_a"],
            "section_id": ids["sec_a"],
            "subject_id": ids["math_a"],
            "title": "Fractions worksheet",
            "instructions": "Answer all questions.",
            "assigned_on": datetime.now(UTC).date().isoformat(),
            "due_at": _at(5),
            "max_marks": 10,
            **extra,
        }

    # ------------------------------------------------------------- management
    expect(
        "science teacher cannot set maths work", call("POST", "/assignments", g_sci, body()), 403
    )
    expect("parent cannot create", call("POST", "/assignments", parent, body()), 403)
    expect("student cannot create", call("POST", "/assignments", s1, body()), 403)
    expect(
        "naive due time rejected",
        call("POST", "/assignments", f_math, body(due_at="2026-12-01T10:00:00")),
        422,
    )
    expect(
        "cross-school subject rejected",
        call("POST", "/assignments", admin, body(subject_id=ids["math_b"])),
        422,
    )
    work = expect("teacher creates draft", call("POST", "/assignments", f_math, body()), 201)["id"]
    expect("draft hidden from student", call("GET", f"/assignments/{work}", s1), 404)
    expect("draft hidden from parent", call("GET", f"/assignments/{work}", parent), 404)
    expect("teacher publishes", call("POST", f"/assignments/{work}/publish", f_math), 200)
    expect(
        "published: max marks frozen",
        call("PATCH", f"/assignments/{work}", f_math, {"max_marks": 20}),
        409,
    )
    expect("student sees published work", call("GET", f"/assignments/{work}", s1), 200)
    expect("other section's student does not", call("GET", f"/assignments/{work}", sb), 404)

    # ------------------------------------------------------------ submissions
    sub1 = expect(
        "student submits",
        call("POST", f"/assignments/{work}/submissions", s1, {"content": "1/2 + 1/4 = 3/4"}),
        201,
    )["id"]
    expect(
        "duplicate submission",
        call("POST", f"/assignments/{work}/submissions", s1, {"content": "again"}),
        409,
    )
    second = expect(
        "resubmission",
        call("POST", f"/assignment-submissions/{sub1}/revisions", s1, {"content": "Revised: 3/4"}),
        201,
    )
    check("revision 2 recorded", second["revision_count"] == 2, second)
    sub2 = expect(
        "second student submits",
        call("POST", f"/assignments/{work}/submissions", s2, {"content": "s2 answer"}),
        201,
    )["id"]
    expect(
        "student cannot resubmit another's work",
        call("POST", f"/assignment-submissions/{sub1}/revisions", s2, {"content": "x"}),
        404,
    )
    expect(
        "teacher cannot submit",
        call("POST", f"/assignments/{work}/submissions", f_math, {"content": "x"}),
        403,
    )

    race = expect(
        "second assignment", call("POST", "/assignments", admin, body(title="Race")), 201
    )["id"]
    call("POST", f"/assignments/{race}/publish", admin)
    statuses = sorted(
        r[0]
        for r in core.together(
            [
                lambda: call("POST", f"/assignments/{race}/submissions", s2, {"content": "same"})
                for _ in range(4)
            ]
        )
    )
    check(
        "concurrent duplicate submissions: exactly one wins",
        statuses == [201, 409, 409, 409],
        statuses,
    )

    strict = expect(
        "strict deadline work", call("POST", "/assignments", admin, body(title="Strict")), 201
    )["id"]
    lenient = expect(
        "late-allowed work",
        call("POST", "/assignments", admin, body(title="Lenient", allow_late=True)),
        201,
    )["id"]
    for item in (strict, lenient):
        call("POST", f"/assignments/{item}/publish", admin)
        call("PATCH", f"/assignments/{item}", admin, {"due_at": _at(-0.01)})
    expect(
        "late work refused",
        call("POST", f"/assignments/{strict}/submissions", s1, {"content": "late"}),
        409,
    )
    late = expect(
        "late work accepted when allowed",
        call("POST", f"/assignments/{lenient}/submissions", s1, {"content": "late"}),
        201,
    )
    check("late submission flagged", late["is_late"] is True, late)

    # ----------------------------------------------------------------- review
    expect(
        "grade above maximum",
        call("PUT", f"/assignment-submissions/{sub1}/review", f_math, {"marks_awarded": 11}),
        422,
    )
    expect(
        "too many decimals",
        call("PUT", f"/assignment-submissions/{sub1}/review", f_math, {"marks_awarded": 8.555}),
        422,
    )
    expect(
        "science teacher cannot review",
        call("PUT", f"/assignment-submissions/{sub1}/review", g_sci, {"marks_awarded": 1}),
        404,
    )
    expect(
        "management cannot review",
        call("PUT", f"/assignment-submissions/{sub1}/review", mgmt, {"marks_awarded": 1}),
        403,
    )
    expect(
        "parent cannot review",
        call("PUT", f"/assignment-submissions/{sub1}/review", parent, {"marks_awarded": 10}),
        403,
    )
    reviewed = expect(
        "teacher reviews",
        call(
            "PUT",
            f"/assignment-submissions/{sub1}/review",
            f_math,
            {"marks_awarded": 8.5, "feedback": "Good work"},
        ),
        200,
    )
    check(
        "review recorded on revision 2",
        (reviewed["status"], reviewed["reviewed_revision"]) == ("reviewed", 2),
        reviewed,
    )
    expect(
        "grade change needs a reason",
        call("PUT", f"/assignment-submissions/{sub1}/review", f_math, {"marks_awarded": 9}),
        422,
    )
    expect(
        "grade change with reason",
        call(
            "PUT",
            f"/assignment-submissions/{sub1}/review",
            f_math,
            {"marks_awarded": 9, "reason": "Re-checked Q3"},
        ),
        200,
    )
    history = call("GET", f"/assignment-submissions/{sub1}/review-history", f_math)[1]
    check(
        "grade history preserved",
        [(h["previous_marks"], h["new_marks"]) for h in history] == [(None, 8.5), (8.5, 9)],
        history,
    )
    expect(
        "resubmission after review refused",
        call("POST", f"/assignment-submissions/{sub1}/revisions", s1, {"content": "x"}),
        409,
    )

    # -------------------------------------------------------------- visibility
    seen = expect(
        "parent reads child's submission",
        call("GET", f"/assignment-submissions/{sub1}", parent),
        200,
    )
    check(
        "parent sees grade but not the work",
        (seen["marks_awarded"], seen["content"]) == (9, None),
        seen,
    )
    expect(
        "parent cannot read the work's revisions",
        call("GET", f"/assignment-submissions/{sub1}/revisions", parent),
        403,
    )
    expect(
        "parent denied another child", call("GET", f"/assignment-submissions/{sub2}", parent), 404
    )
    check(
        "parent filter cannot widen",
        call("GET", "/assignment-submissions", parent, params={"student_id": students["s2"]})[1][
            "items"
        ]
        == [],
    )
    expect(
        "student denied another student's work",
        call("GET", f"/assignment-submissions/{sub2}", s1),
        404,
    )
    check(
        "student sees own content",
        call("GET", f"/assignment-submissions/{sub1}", s1)[1]["content"] == "Revised: 3/4",
    )
    roster = call("GET", f"/assignments/{work}/students", f_math, params={"outstanding": "true"})[
        1
    ]["items"]
    check("no outstanding students for the worksheet", roster == [], roster)
    expect(
        "management reads submissions", call("GET", f"/assignment-submissions/{sub1}", mgmt), 200
    )

    # ------------------------------------------------------- cross-school / IDOR
    for path in (
        f"/assignments/{work}",
        f"/assignment-submissions/{sub1}",
        f"/assignments/{work}/students",
    ):
        expect(f"school B admin -> {path}", call("GET", path, admin_b), 404)
    expect(
        "school B admin cannot close A's work",
        call("POST", f"/assignments/{work}/close", admin_b),
        404,
    )
    expect(
        "forged X-School-ID",
        call("GET", "/assignments", {**admin, "X-School-ID": core.SCHOOL_B}),
        404,
    )
    check(
        "filter by school B section is empty",
        call("GET", "/assignments", admin, params={"section_id": ids["sec_b"]})[1]["items"] == [],
    )

    # ----------------------------------------------------------------- closure
    expect("teacher closes", call("POST", f"/assignments/{work}/close", f_math), 200)
    expect(
        "no submissions after closing",
        call("POST", f"/assignment-submissions/{sub2}/revisions", s2, {"content": "x"}),
        409,
    )
    expect(
        "review after closing",
        call("PUT", f"/assignment-submissions/{sub2}/review", f_math, {"marks_awarded": 6}),
        200,
    )
    exam_rows = core.psql(
        # Only the run's validated school UUIDs are interpolated.
        "SELECT count(*) FROM exam_report_cards WHERE "  # noqa: S608
        + core._in_run("school_id")
        + ";"
    ).strip()
    check("assignment grading never touches report cards", exam_rows == "0", exam_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 real-HTTP E2E (Docker stack).")
    parser.add_argument("--base-url", default=core.BASE)
    parser.add_argument("--cleanup", action="store_true", help="delete this run's rows afterwards")
    args = parser.parse_args()
    core.BASE = args.base_url.rstrip("/")
    core.guard()
    core.setup()
    try:
        scenario()
        core.audit_scan()
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        check("scenario ran to completion", False, repr(exc))
    finally:
        if args.cleanup:
            core.cleanup()
    failed = [r for r in core.RESULTS if not r[1]]
    for name, ok, detail in core.RESULTS:
        print(("PASS " if ok else "FAIL ") + name + (f"  <- {detail}" if detail else ""))
    print(f"{len(core.RESULTS) - len(failed)}/{len(core.RESULTS)} checks passed (tag {core.TAG})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
