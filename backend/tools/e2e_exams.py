"""Phase 7 (exams, marks, report cards) real-HTTP E2E run against the Docker stack.

Run from the repository root while ``docker compose up -d`` is running::

    python backend/tools/e2e_exams.py              # run the checks, keep the data
    python backend/tools/e2e_exams.py --cleanup    # ...then delete what this run created

Standard library only. Every check is printed; the exit code is 1 if any fails.

Safety and assumptions:

* It talks to the compose project in the current directory (``docker compose``): the API
  at ``--base-url`` (default ``http://127.0.0.1:8000``) and the ``backend``/``db`` services.
* It refuses to run unless the backend reports ``APP_ENV`` ``development`` or ``test``,
  and needs ``OTP_DELIVERY_BACKEND=development_console`` (parent sign-in reads the
  development OTP stub from the backend log).
* Setup creates two fresh schools (codes ``E2E7A-<tag>``/``E2E7B-<tag>``) and their staff
  through the admin CLI, with a random password passed through an environment variable
  (never printed, never on a command line).
* ``--cleanup`` deletes **only** rows belonging to the two schools and the users this run
  created (plus their audit rows), in one transaction. Nothing else is touched.
  Throttling rows in ``login_attempts`` hold only identifier hashes and are left to expire.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE = "http://127.0.0.1:8000"
SCHOOL_A = SCHOOL_B = ADMIN_A = ADMIN_B = MGMT = ""
PASSWORD = "E2e-" + secrets.token_urlsafe(18)
TAG = secrets.token_hex(3).upper()
RESULTS: list[tuple[str, bool, str]] = []


def compose(*args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    extra: list[str] = []
    for key in env or {}:
        extra += ["-e", key]
    command = [
        "docker",
        "compose",
        *args[:1],
        *(["-T", *extra] if args[0] == "exec" else []),
        *args[1:],
    ]
    result = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def psql(sql: str) -> str:
    return compose(
        "exec",
        "db",
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1',
        stdin=sql,
    )


def guard() -> None:
    environment = compose("exec", "backend", "printenv", "APP_ENV").strip()
    if environment not in ("development", "test"):
        sys.exit(f"Refusing to run: backend APP_ENV is {environment!r}, not development/test.")
    otp = compose("exec", "backend", "printenv", "OTP_DELIVERY_BACKEND").strip()
    if otp != "development_console":
        sys.exit("OTP_DELIVERY_BACKEND must be development_console for parent sign-in.")


def setup() -> None:
    global SCHOOL_A, SCHOOL_B, ADMIN_A, ADMIN_B, MGMT
    schools = []
    for suffix, extra in (("A", ["--timezone", "Asia/Kolkata"]), ("B", [])):
        out = compose(
            "exec",
            "backend",
            "python",
            "-m",
            "app.cli",
            "create-school",
            "--name",
            f"E2E P7 {suffix} {TAG}",
            "--code",
            f"E2E7{suffix}-{TAG}",
            *extra,
        )
        schools.append(out.split()[2])
    # Validated UUIDs: the only values interpolated into the SQL below.
    SCHOOL_A, SCHOOL_B = (str(uuid.UUID(school)) for school in schools)
    tag = TAG.lower()
    accounts = (
        ("SCHOOL_ADMIN", SCHOOL_A, "admin-a"),
        ("SCHOOL_ADMIN", SCHOOL_B, "admin-b"),
        ("MANAGEMENT", SCHOOL_A, "mgmt-a"),
    )
    emails = []
    for role, school, name in accounts:
        email = f"e2e7-{name}-{tag}@e2e.test"
        compose(
            "exec",
            "backend",
            "python",
            "-m",
            "app.cli",
            "create-user",
            "--full-name",
            f"E2E {name}",
            "--email",
            email,
            "--role",
            role,
            "--school-id",
            school,
            "--password-env",
            "E2E_PASSWORD",
            env={"E2E_PASSWORD": PASSWORD},
        )
        emails.append(email)
    ADMIN_A, ADMIN_B, MGMT = emails


METADATA_SCHOOL = "metadata->>'school_id'"


def _in_run(column: str) -> str:
    # Only the two validated school UUIDs are interpolated (see setup()).
    return f"{column} IN ('{SCHOOL_A}', '{SCHOOL_B}')"


# Sensitive data as JSON keys (a value such as method="password" is not a secret).
SENSITIVE_KEYS = r'"(access_token|refresh_token|otp|code|password|first_name|last_name)":'


def audit_scan() -> None:
    """This run's stored audit metadata contains no password, token or student name.
    The password is compared in Python, so it is never sent to the database."""
    rows = psql(
        "SELECT action || ' ' || metadata::text FROM audit_logs "  # noqa: S608
        f"WHERE {_in_run(METADATA_SCHOOL)} "
        f"OR user_id IN (SELECT id FROM users WHERE {_in_run('school_id')});"
    ).splitlines()
    leaks = [
        row.split(" ", 1)[0] for row in rows if PASSWORD in row or re.search(SENSITIVE_KEYS, row)
    ]
    check(f"{len(rows)} audit rows hold no password/token/student name", not leaks, leaks)


CLEANUP = """
BEGIN;
SET LOCAL app.exam_history_purge = 'on';
SET LOCAL app.attendance_history_purge = 'on';
CREATE TEMP TABLE e2e_s ON COMMIT DROP AS SELECT id FROM schools WHERE {run_schools};
CREATE TEMP TABLE e2e_u ON COMMIT DROP AS
    SELECT id FROM users WHERE school_id IN (SELECT id FROM e2e_s);
{deletes}
DELETE FROM audit_logs WHERE user_id IN (SELECT id FROM e2e_u)
    OR resource_id IN (SELECT id::text FROM e2e_u) OR resource_id IN (SELECT id::text FROM e2e_s)
    OR metadata->>'school_id' IN (SELECT id::text FROM e2e_s);
DELETE FROM users WHERE id IN (SELECT id FROM e2e_u);
DELETE FROM schools WHERE id IN (SELECT id FROM e2e_s);
COMMIT;
"""
SCHOOL_TABLES = (
    "exam_report_cards",
    "exam_publications",
    "exam_mark_changes",
    "exam_marks",
    "exam_subjects",
    "exams",
    "attendance_record_changes",
    "attendance_records",
    "attendance_sessions",
    "faculty_assignments",
    "faculty",
    "student_parents",
    "student_enrollments",
    "students",
    "parents",
    "sections",
    "subjects",
    "classes",
    "academic_years",
)


def cleanup() -> None:
    # Table names are constants and the schools are validated UUIDs.
    deletes = chr(10).join(
        f"DELETE FROM {table} WHERE school_id IN (SELECT id FROM e2e_s);"  # noqa: S608
        for table in SCHOOL_TABLES
    )
    psql(CLEANUP.format(run_schools=_in_run("id"), deletes=deletes))
    left = psql(f"SELECT count(*) FROM schools WHERE {_in_run('id')};").strip()  # noqa: S608
    print(f"cleanup: removed this run's schools, users and their rows (schools left: {left})")


def call(
    method: str,
    path: str,
    token: dict[str, str] | None = None,
    body: Any = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = f"{BASE}/api/v1{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    request.add_header("Content-Type", "application/json")
    for key, value in (token or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def check(name: str, ok: bool, detail: Any = "") -> bool:
    RESULTS.append((name, bool(ok), "" if ok else str(detail)[:300]))
    return bool(ok)


def expect(name: str, response: tuple[int, Any], status: int) -> Any:
    check(f"{name} -> {status}", response[0] == status, response)
    return response[1]


def login(identifier: str, password: str = "", school_code: str | None = None) -> dict[str, str]:
    body: dict[str, Any] = {"identifier": identifier, "password": password or PASSWORD}
    if school_code:
        body["school_code"] = school_code
    status, data = call("POST", "/auth/login", body=body)
    if status != 200:
        raise RuntimeError(f"login failed for {identifier}: {data}")
    return {"Authorization": f"Bearer {data['access_token']}"}


def otp_login(mobile: str) -> dict[str, str]:
    if call("POST", "/auth/otp/request", body={"phone": mobile})[0] != 202:
        raise RuntimeError("OTP request refused")
    pattern = r"code for \+\d\d\*+" + re.escape(mobile[-2:]) + r" is (\d+)"
    for _ in range(20):
        found = re.findall(pattern, compose("logs", "backend", "--since", "20s"))
        if found:
            status, data = call(
                "POST", "/auth/otp/verify", body={"phone": mobile, "code": found[-1]}
            )
            if status != 200:
                raise RuntimeError(f"OTP verify failed: {data}")
            return {"Authorization": f"Bearer {data['access_token']}"}
        time.sleep(0.5)
    raise RuntimeError("OTP code not found in the backend log")


def together(fns: list[Any]) -> list[tuple[int, Any]]:
    barrier = threading.Barrier(len(fns))
    out: list[Any] = [None] * len(fns)

    def run(i: int, fn: Any) -> None:
        barrier.wait()
        out[i] = fn()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return out


def scenario() -> None:
    admin = login(ADMIN_A)
    admin_b = login(ADMIN_B)
    mgmt = login(MGMT)
    school_code = call("GET", "/school", admin)[1]["code"]

    # ------------------------------------------------------------ structure (A and B)
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
        ids[f"class_{key}"] = make(token, "/classes", {"name": "Class 10", "code": "C10"})
        ids[f"sec_{key}"] = make(
            token, "/sections", {"class_id": ids[f"class_{key}"], "name": "A", "code": "A"}
        )
        ids[f"math_{key}"] = make(token, "/subjects", {"name": "Mathematics", "code": "MATH"})
    ids["sec_ab"] = make(admin, "/sections", {"class_id": ids["class_a"], "name": "B", "code": "B"})
    ids["sci_a"] = make(admin, "/subjects", {"name": "Science", "code": "SCI"})

    def faculty(subject: str, section: str) -> dict[str, str]:
        email = f"e2e-{subject}-{section}-{TAG.lower()}@e2e.test"
        fid = make(
            admin,
            "/faculty",
            {
                "employee_id": f"E2E-{subject}-{section}-{TAG}".upper()[:30],
                "first_name": "Fac",
                "last_name": subject,
                "email": email,
                "password": PASSWORD,
            },
        )
        make(
            admin,
            "/faculty-assignments",
            {
                "faculty_id": fid,
                "academic_year_id": ids["year_a"],
                "class_id": ids["class_a"],
                "section_id": ids[section],
                "subject_id": ids[subject],
            },
        )
        return login(email)

    f_math = faculty("math_a", "sec_a")
    g_sci = faculty("sci_a", "sec_a")

    students: dict[str, str] = {}
    for key, section, token, year, cls in (
        ("a1", "sec_a", admin, "year_a", "class_a"),
        ("a2", "sec_a", admin, "year_a", "class_a"),
        ("ab", "sec_ab", admin, "year_a", "class_a"),
        ("b1", "sec_b", admin_b, "year_b", "class_b"),
    ):
        students[key] = make(
            token,
            "/students",
            {
                "admission_number": f"{key}-{TAG}".upper(),
                "first_name": "Stu",
                "last_name": key.upper(),
                "password": PASSWORD,
            },
        )
        make(
            token,
            "/enrollments",
            {
                "student_id": students[key],
                "academic_year_id": ids[year],
                "class_id": ids[cls],
                "section_id": ids[section],
                "enrollment_date": "2026-06-10",
                "is_current": True,
            },
        )
    student_a1 = login(f"a1-{TAG}".upper(), school_code=school_code)
    student_a2 = login(f"a2-{TAG}".upper(), school_code=school_code)
    mobile = f"+9198{int(TAG, 16) % 100000000:08d}"
    parent_id = make(admin, "/parents", {"first_name": "Par", "last_name": "Ent", "mobile": mobile})
    make(
        admin,
        f"/students/{students['a1']}/parents",
        {"parent_id": parent_id, "relationship_type": "mother"},
    )
    parent = otp_login(mobile)

    # ---------------------------------------------------------------- exam config
    exam_body = {
        "academic_year_id": ids["year_a"],
        "class_id": ids["class_a"],
        "name": "Midterm",
        "exam_type": "midterm",
        "start_date": "2026-09-01",
        "end_date": "2026-09-10",
        "grade_bands": [
            {"grade": "A", "min_percentage": 75},
            {"grade": "B", "min_percentage": 50},
            {"grade": "C", "min_percentage": 0},
        ],
    }
    expect("faculty cannot create exam", call("POST", "/exams", f_math, exam_body), 403)
    expect("parent cannot create exam", call("POST", "/exams", parent, exam_body), 403)
    exam = expect("admin creates exam", call("POST", "/exams", admin, exam_body), 201)["id"]
    expect("duplicate exam name", call("POST", "/exams", admin, exam_body), 409)
    expect("schedule without subjects", call("POST", f"/exams/{exam}/schedule", admin), 422)
    math = expect(
        "add math component",
        call(
            "POST",
            f"/exams/{exam}/subjects",
            admin,
            {"subject_id": ids["math_a"], "max_marks": 100, "pass_marks": 35},
        ),
        201,
    )["id"]
    sci = expect(
        "add science component",
        call(
            "POST",
            f"/exams/{exam}/subjects",
            admin,
            {"subject_id": ids["sci_a"], "max_marks": 50, "pass_marks": 20},
        ),
        201,
    )["id"]
    expect(
        "pass > max rejected",
        call(
            "POST",
            f"/exams/{exam}/subjects",
            admin,
            {"subject_id": ids["math_a"], "max_marks": 10, "pass_marks": 11},
        ),
        422,
    )
    expect(
        "cross-school subject rejected",
        call(
            "POST", f"/exams/{exam}/subjects", admin, {"subject_id": ids["math_b"], "max_marks": 10}
        ),
        422,
    )
    expect(
        "marks before marks entry",
        call(
            "POST",
            f"/exams/{exam}/subjects/{math}/marks",
            admin,
            {"student_id": students["a1"], "marks_obtained": 1},
        ),
        409,
    )
    expect("schedule", call("POST", f"/exams/{exam}/schedule", admin), 200)
    expect(
        "subject removal refused once scheduled",
        call("DELETE", f"/exams/{exam}/subjects/{sci}", admin),
        409,
    )
    expect("open marks entry", call("POST", f"/exams/{exam}/open-marks-entry", admin), 200)
    expect(
        "components frozen",
        call("PATCH", f"/exams/{exam}/subjects/{math}", admin, {"max_marks": 90}),
        409,
    )

    # ----------------------------------------------------------------- marks entry
    m = f"/exams/{exam}/subjects"
    a1_math = expect(
        "faculty single marks entry",
        call(
            "POST",
            f"{m}/{math}/marks",
            f_math,
            {"student_id": students["a1"], "marks_obtained": 80},
        ),
        201,
    )["id"]
    expect(
        "faculty other section -> not eligible",
        call(
            "POST",
            f"{m}/{math}/marks",
            f_math,
            {"student_id": students["ab"], "marks_obtained": 50},
        ),
        422,
    )
    expect(
        "faculty other subject",
        call(
            "POST", f"{m}/{sci}/marks", f_math, {"student_id": students["a2"], "marks_obtained": 10}
        ),
        403,
    )
    expect(
        "parent cannot enter marks",
        call(
            "POST",
            f"{m}/{math}/marks",
            parent,
            {"student_id": students["a2"], "marks_obtained": 99},
        ),
        403,
    )
    expect(
        "student cannot enter marks",
        call(
            "POST",
            f"{m}/{math}/marks",
            student_a1,
            {"student_id": students["a1"], "marks_obtained": 99},
        ),
        403,
    )
    for bad in (100.5, -1, 12.345):
        expect(
            f"invalid marks {bad}",
            call(
                "POST",
                f"{m}/{math}/marks",
                admin,
                {"student_id": students["a2"], "marks_obtained": bad},
            ),
            422,
        )
    expect(
        "absent with marks",
        call(
            "POST",
            f"{m}/{math}/marks",
            admin,
            {"student_id": students["a2"], "status": "absent", "marks_obtained": 5},
        ),
        422,
    )
    expect(
        "cross-school student",
        call(
            "POST", f"{m}/{math}/marks", admin, {"student_id": students["b1"], "marks_obtained": 5}
        ),
        422,
    )
    expect(
        "bulk with one invalid entry",
        call(
            "POST",
            f"{m}/{math}/marks/bulk",
            admin,
            {
                "marks": [
                    {"student_id": students["a2"], "marks_obtained": 30},
                    {"student_id": students["b1"], "marks_obtained": 30},
                ]
            },
        ),
        422,
    )
    unmarked = call("GET", f"{m}/{math}/students", admin, params={"unmarked": "true"})[1]
    check("bulk rollback left nothing written", unmarked["total"] == 2, unmarked)
    expect(
        "admin bulk math",
        call(
            "POST",
            f"{m}/{math}/marks/bulk",
            admin,
            {
                "marks": [
                    {"student_id": students["a2"], "marks_obtained": 30},
                    {"student_id": students["ab"], "status": "exempt"},
                ]
            },
        ),
        201,
    )
    sci_bulk = expect(
        "faculty bulk science",
        call(
            "POST",
            f"{m}/{sci}/marks/bulk",
            g_sci,
            {
                "marks": [
                    {"student_id": students["a1"], "marks_obtained": 40},
                    {"student_id": students["a2"], "status": "absent"},
                ]
            },
        ),
        201,
    )
    a2_sci = next(i["id"] for i in sci_bulk["items"] if i["student_id"] == students["a2"])
    expect(
        "duplicate entry",
        call(
            "POST", f"{m}/{sci}/marks", admin, {"student_id": students["a1"], "marks_obtained": 1}
        ),
        409,
    )

    # ------------------------------------------------------------------ corrections
    expect(
        "correction needs reason",
        call("PATCH", f"/exam-marks/{a1_math}", f_math, {"marks_obtained": 82}),
        422,
    )
    expect(
        "faculty correction",
        call(
            "PATCH",
            f"/exam-marks/{a1_math}",
            f_math,
            {"marks_obtained": 82, "reason": "Totalling error"},
        ),
        200,
    )
    history = expect("history readable", call("GET", f"/exam-marks/{a1_math}/history", admin), 200)
    check(
        "history preserves old value",
        [(h["previous_marks"], h["new_marks"], h["reason"]) for h in history]
        == [(80, 82, "Totalling error")],
        history,
    )
    expect(
        "IDOR: faculty reads other subject's mark",
        call("GET", f"/exam-marks/{a2_sci}", f_math),
        404,
    )
    expect(
        "IDOR: faculty corrects other subject's mark",
        call("PATCH", f"/exam-marks/{a2_sci}", f_math, {"marks_obtained": 1, "reason": "x"}),
        404,
    )

    # ------------------------------------------------- concurrent duplicate bulk submissions
    ut = expect(
        "second exam", call("POST", "/exams", admin, {**exam_body, "name": "Unit Test"}), 201
    )["id"]
    ut_math = call(
        "POST", f"/exams/{ut}/subjects", admin, {"subject_id": ids["math_a"], "max_marks": 25}
    )[1]["id"]
    call("POST", f"/exams/{ut}/schedule", admin)
    call("POST", f"/exams/{ut}/open-marks-entry", admin)
    bulk = {
        "marks": [{"student_id": students[k], "marks_obtained": 20} for k in ("a1", "a2", "ab")]
    }
    statuses = sorted(
        r[0]
        for r in together(
            [
                lambda: call("POST", f"/exams/{ut}/subjects/{ut_math}/marks/bulk", admin, bulk)
                for _ in range(4)
            ]
        )
    )
    check("concurrent identical bulk: exactly one wins", statuses == [201, 409, 409, 409], statuses)
    check(
        "concurrent bulk: complete, not duplicated",
        call("GET", f"/exams/{ut}/subjects/{ut_math}/marks", admin)[1]["total"] == 3,
    )

    # ------------------------------------------------------------------- results
    results = {
        r["student"]["id"]: r
        for r in expect("results preview", call("GET", f"/exams/{exam}/results", admin), 200)[
            "items"
        ]
    }
    a1 = results[students["a1"]]
    check(
        "a1: 122/150 = 81.33% pass A",
        (a1["total_obtained"], a1["total_max"], a1["percentage"], a1["result"], a1["grade"])
        == (122, 150, 81.33, "pass", "A"),
        a1,
    )
    a2 = results[students["a2"]]
    check(
        "a2: absent counts 0, fails",
        (a2["total_obtained"], a2["total_max"], a2["percentage"], a2["result"])
        == (30, 150, 20, "fail"),
        a2,
    )
    ab = results[students["ab"]]
    check(
        "ab: science missing -> incomplete",
        (ab["result"], ab["percentage"]) == ("incomplete", None),
        ab,
    )
    expect("faculty cannot preview results", call("GET", f"/exams/{exam}/results", f_math), 403)
    for who, token in (("parent", parent), ("student", student_a1)):
        check(
            f"{who}: no unpublished report cards",
            call("GET", "/report-cards", token)[1]["items"] == [],
        )
        expect(f"{who}: exam hidden", call("GET", f"/exams/{exam}", token), 404)
    expect("publish refused with missing marks", call("POST", f"/exams/{exam}/publish", admin), 422)
    expect(
        "enter last mark",
        call(
            "POST", f"{m}/{sci}/marks", admin, {"student_id": students["ab"], "marks_obtained": 45}
        ),
        201,
    )

    # ---------------------------------------------- correction racing publication + duplicates
    race = together(
        [
            lambda: call(
                "PATCH", f"/exam-marks/{a1_math}", f_math, {"marks_obtained": 84, "reason": "Race"}
            ),
            lambda: call("POST", f"/exams/{exam}/publish", mgmt),
            lambda: call("POST", f"/exams/{exam}/publish", admin),
        ]
    )
    publishes = sorted(r[0] for r in race[1:])
    check("concurrent publish: exactly one wins", publishes == [201, 409], race)
    expect("faculty cannot publish", call("POST", f"/exams/{exam}/publish", f_math), 403)
    cards = {
        c["student"]["id"]: c for c in call("GET", f"/exams/{exam}/report-cards", admin)[1]["items"]
    }
    snapshot_math = next(
        line["marks_obtained"]
        for line in cards[students["a1"]]["subjects"]
        if line["subject_code"] == "MATH"
    )
    stored = call("GET", f"/exam-marks/{a1_math}", admin)[1]["marks_obtained"]
    check(
        "correction vs publish: snapshot equals committed mark",
        snapshot_math == stored and (race[0][0] == 200) == (stored == 84),
        (race[0][0], snapshot_math, stored),
    )
    expect(
        "published: marks frozen",
        call("PATCH", f"/exam-marks/{a1_math}", admin, {"marks_obtained": 1, "reason": "x"}),
        409,
    )

    # ---------------------------------------------------------------- visibility
    mine = call("GET", "/report-cards", parent)[1]["items"]
    check(
        "parent sees only linked child's card",
        [c["student"]["id"] for c in mine] == [students["a1"]],
        mine,
    )
    expect(
        "parent reads child's card",
        call("GET", f"/report-cards/{cards[students['a1']]['id']}", parent),
        200,
    )
    expect(
        "parent denied other child's card",
        call("GET", f"/report-cards/{cards[students['a2']]['id']}", parent),
        404,
    )
    check(
        "parent filter cannot widen",
        call("GET", "/report-cards", parent, params={"student_id": students["a2"]})[1]["items"]
        == [],
    )
    expect(
        "student reads own card",
        call("GET", f"/report-cards/{cards[students['a1']]['id']}", student_a1),
        200,
    )
    expect(
        "student denied another student's card",
        call("GET", f"/report-cards/{cards[students['a1']]['id']}", student_a2),
        404,
    )
    check(
        "student list shows only self",
        [c["student"]["id"] for c in call("GET", "/report-cards", student_a2)[1]["items"]]
        == [students["a2"]],
    )

    # ----------------------------------------------------------- cross-school / IDOR
    for path in (
        f"/exams/{exam}",
        f"/exams/{exam}/subjects/{math}/marks",
        f"/exam-marks/{a1_math}",
        f"/report-cards/{cards[students['a1']]['id']}",
        f"/exams/{exam}/results",
    ):
        expect(f"school B admin -> {path}", call("GET", path, admin_b), 404)
    expect(
        "school B admin cannot publish/reopen A",
        call("POST", f"/exams/{exam}/reopen", admin_b, {"reason": "x"}),
        404,
    )
    expect("forged X-School-ID", call("GET", "/exams", {**admin, "X-School-ID": SCHOOL_B}), 404)
    check(
        "filter with school B class is empty",
        call("GET", "/exams", admin, params={"class_id": ids["class_b"]})[1]["items"] == [],
    )
    check(
        "filter with school B student is empty",
        call("GET", "/report-cards", admin, params={"student_id": students["b1"]})[1]["items"]
        == [],
    )

    # ---------------------------------------------------------- reopen and republish
    expect("reopen needs reason", call("POST", f"/exams/{exam}/reopen", mgmt), 422)
    expect(
        "management reopens",
        call("POST", f"/exams/{exam}/reopen", mgmt, {"reason": "Science script found"}),
        200,
    )
    check(
        "withdrawn results hidden from parent",
        call("GET", "/report-cards", parent)[1]["items"] == [],
    )
    expect(
        "correct after reopen",
        call(
            "PATCH",
            f"/exam-marks/{a2_sci}",
            admin,
            {"status": "present", "marks_obtained": 30, "reason": "Script found"},
        ),
        200,
    )
    v2 = expect("republish", call("POST", f"/exams/{exam}/publish", admin), 201)
    check("republish is version 2", v2["version"] == 2, v2)
    old = call("GET", f"/report-cards/{cards[students['a2']]['id']}", admin)[1]
    check(
        "version 1 snapshot unchanged",
        (old["percentage"], old["publication_status"]) == (20, "withdrawn"),
        old,
    )
    new_a2 = call("GET", "/report-cards", student_a2)[1]["items"]
    check(
        "student sees version 2 only",
        [(c["publication_version"], c["percentage"]) for c in new_a2] == [(2, 40)],
        new_a2,
    )


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser(description="Phase 7 real-HTTP E2E (Docker stack).")
    parser.add_argument("--base-url", default=BASE)
    parser.add_argument(
        "--cleanup", action="store_true", help="delete the rows this run created afterwards"
    )
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")
    guard()
    setup()
    try:
        scenario()
        audit_scan()
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        check("scenario ran to completion", False, repr(exc))
    finally:
        if args.cleanup:
            cleanup()
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name + (f"  <- {detail}" if detail else ""))
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed (tag {TAG})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
