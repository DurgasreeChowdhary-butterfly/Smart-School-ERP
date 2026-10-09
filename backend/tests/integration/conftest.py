"""Fixtures for people/enrollment tests: two schools with structure, built over HTTP."""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.security.permissions import RoleName
from tests.conftest import SCHOOL_B, CapturingOtpSender, HeadersFactory, bearer

STUDENT_PASSWORD = "Student-Pass-2026"
FACULTY_PASSWORD = "Teacher-Secret-2026"
_mobiles = itertools.count(1)
_employees = itertools.count(1)


@dataclass
class World:
    client: TestClient
    outbox: CapturingOtpSender
    admin_a: dict[str, str]
    admin_b: dict[str, str]
    ids: dict[str, str] = field(default_factory=dict)

    # ---------------------------------------------------------------- creation
    def student(
        self,
        adm: str,
        headers: dict[str, str] | None = None,
        *,
        password: str | None = None,
        **extra: Any,
    ) -> Any:
        body = {"admission_number": adm, "first_name": "Stu", "last_name": adm, **extra}
        if password:
            body["password"] = password
        return self.client.post("/api/v1/students", headers=headers or self.admin_a, json=body)

    def parent(
        self, headers: dict[str, str] | None = None, mobile: str | None = None, **extra: Any
    ) -> Any:
        mobile = mobile or f"+9197{next(_mobiles):08d}"
        body = {"first_name": "Par", "last_name": "Ent", "mobile": mobile, **extra}
        return self.client.post("/api/v1/parents", headers=headers or self.admin_a, json=body)

    def link(
        self, student_id: str, parent_id: str, headers: dict[str, str] | None = None, **extra: Any
    ) -> Any:
        body = {"parent_id": parent_id, "relationship_type": "mother", **extra}
        return self.client.post(
            f"/api/v1/students/{student_id}/parents", headers=headers or self.admin_a, json=body
        )

    def enroll(
        self,
        student_id: str,
        headers: dict[str, str] | None = None,
        *,
        year: str = "year_a",
        cls: str = "class_a",
        sec: str = "section_a",
        date: str = "2026-06-10",
        current: bool = True,
        **extra: Any,
    ) -> Any:
        body = {
            "student_id": student_id,
            "academic_year_id": self.ids[year],
            "class_id": self.ids[cls],
            "section_id": self.ids[sec],
            "enrollment_date": date,
            "is_current": current,
            **extra,
        }
        return self.client.post("/api/v1/enrollments", headers=headers or self.admin_a, json=body)

    def faculty(
        self,
        headers: dict[str, str] | None = None,
        *,
        email: str | None = None,
        mobile: str | None = None,
        password: str | None = FACULTY_PASSWORD,
        **extra: Any,
    ) -> Any:
        n = next(_employees)
        body: dict[str, Any] = {
            "employee_id": f"EMP-{n}",
            "first_name": "Fac",
            "last_name": f"L{n}",
        }
        if email is None and mobile is None and "user_id" not in extra:
            email = f"teacher-{uuid.uuid4().hex[:10]}@school.test"
        for key, value in (("email", email), ("mobile", mobile), ("password", password)):
            if value is not None and "user_id" not in extra:
                body[key] = value
        body.update(extra)
        return self.client.post("/api/v1/faculty", headers=headers or self.admin_a, json=body)

    def assign(
        self,
        faculty_id: str,
        headers: dict[str, str] | None = None,
        *,
        year: str = "year_a",
        cls: str = "class_a",
        sec: str = "section_a",
        subject: str = "math_a",
        **extra: Any,
    ) -> Any:
        body = {
            "faculty_id": faculty_id,
            "academic_year_id": self.ids[year],
            "class_id": self.ids[cls],
            "section_id": self.ids[sec],
            "subject_id": self.ids[subject],
            **extra,
        }
        return self.client.post(
            "/api/v1/faculty-assignments", headers=headers or self.admin_a, json=body
        )

    def add_subjects(self) -> None:
        """Subjects for both schools plus a second section (B) in class_a / class_b."""
        for suffix, headers in (("a", self.admin_a), ("b", self.admin_b)):
            for code in ("math", "sci"):
                self.ids[f"{code}_{suffix}"] = _post(
                    self.client, headers, "/api/v1/subjects", {"name": code, "code": code}
                )
            self.ids[f"section_{suffix}_b"] = _post(
                self.client,
                headers,
                "/api/v1/sections",
                {"class_id": self.ids[f"class_{suffix}"], "name": "B", "code": "B"},
            )

    # ---------------------------------------------------------------- sign-in
    def login_student(self, adm: str, password: str = STUDENT_PASSWORD, code: str = "SCH-A") -> Any:
        return self.client.post(
            "/api/v1/auth/login",
            json={"school_code": code, "identifier": adm, "password": password},
        )

    def student_headers(self, adm: str, password: str = STUDENT_PASSWORD) -> dict[str, str]:
        response = self.login_student(adm, password)
        assert response.status_code == 200, response.text
        return bearer(response.json()["access_token"])

    def faculty_headers(self, identifier: str, password: str = FACULTY_PASSWORD) -> dict[str, str]:
        response = self.client.post(
            "/api/v1/auth/login", json={"identifier": identifier, "password": password}
        )
        assert response.status_code == 200, response.text
        return bearer(response.json()["access_token"])

    def parent_headers(self, mobile: str) -> dict[str, str]:
        assert (
            self.client.post("/api/v1/auth/otp/request", json={"phone": mobile}).status_code == 202
        )
        response = self.client.post(
            "/api/v1/auth/otp/verify", json={"phone": mobile, "code": self.outbox.last_code(mobile)}
        )
        assert response.status_code == 200, response.text
        return bearer(response.json()["access_token"])


def _post(client: TestClient, headers: dict[str, str], path: str, body: dict[str, Any]) -> str:
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
def world(
    db_client: TestClient, auth_headers: HeadersFactory, otp_outbox: CapturingOtpSender
) -> World:
    w = World(
        client=db_client,
        outbox=otp_outbox,
        admin_a=auth_headers(RoleName.SCHOOL_ADMIN),
        admin_b=auth_headers(RoleName.SCHOOL_ADMIN, SCHOOL_B),
    )
    for suffix, headers in (("a", w.admin_a), ("b", w.admin_b)):
        years = "/api/v1/academic-years"
        w.ids[f"year_{suffix}_prev"] = _post(
            db_client,
            headers,
            years,
            {"name": "2025-26", "start_date": "2025-06-01", "end_date": "2026-05-31"},
        )
        w.ids[f"year_{suffix}"] = _post(
            db_client,
            headers,
            years,
            {"name": "2026-27", "start_date": "2026-06-01", "end_date": "2027-05-31"},
        )
        for cls in ("class", "class2"):
            w.ids[f"{cls}_{suffix}"] = _post(
                db_client, headers, "/api/v1/classes", {"name": cls, "code": f"{cls}-{suffix}"}
            )
            w.ids[f"{cls.replace('class', 'section')}_{suffix}"] = _post(
                db_client,
                headers,
                "/api/v1/sections",
                {"class_id": w.ids[f"{cls}_{suffix}"], "name": "A", "code": "A"},
            )
    return w


@pytest.fixture
def fworld(world: World) -> World:
    """``world`` plus subjects (math/sci) and a section B in class_a/class_b per school."""
    world.add_subjects()
    return world


# =========================================================================== attendance
SESSION_DATE = "2026-07-10"
ATTENDANCE = "/api/v1/attendance"


@dataclass
class AttendanceWorld:
    """``fworld`` plus three faculty members and students placed for attendance tests.

    * F teaches year_a / class_a / section_a / math_a
    * G teaches year_a / class_a / section_a / sci_a
    * H teaches year_a / class_a / section_a_b / math_a
    * students: a1, a2 (section_a, with logins), ab (section_a_b), c2 (class2_a),
      prev (section_a, previous year only), b (school B)
    """

    w: World
    f: dict[str, str]
    g: dict[str, str]
    h: dict[str, str]
    faculty_ids: dict[str, str]
    s: dict[str, str]
    e: dict[str, str]

    @property
    def client(self) -> TestClient:
        return self.w.client

    @property
    def admin(self) -> dict[str, str]:
        return self.w.admin_a

    def sid(self, key: str) -> str:
        return self.s.get(key, key)

    def session(
        self,
        headers: dict[str, str] | None = None,
        *,
        year: str = "year_a",
        cls: str = "class_a",
        sec: str = "section_a",
        subject: str | None = "math_a",
        date: str = SESSION_DATE,
        **extra: Any,
    ) -> Any:
        ids = self.w.ids
        body: dict[str, Any] = {
            "academic_year_id": ids[year],
            "class_id": ids[cls],
            "section_id": ids[sec],
            "session_date": date,
            **extra,
        }
        if subject is not None:
            body["subject_id"] = ids[subject]
        return self.client.post(f"{ATTENDANCE}/sessions", headers=headers or self.admin, json=body)

    def session_id(self, headers: dict[str, str] | None = None, **kwargs: Any) -> str:
        response = self.session(headers, **kwargs)
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def mark(
        self,
        session_id: str,
        student: str,
        status: str = "present",
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> Any:
        body = {"student_id": self.sid(student), "status": status, **extra}
        return self.client.post(
            f"{ATTENDANCE}/sessions/{session_id}/records", headers=headers or self.admin, json=body
        )

    def record_id(self, session_id: str, student: str, status: str = "present") -> str:
        response = self.mark(session_id, student, status)
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def bulk(
        self, session_id: str, entries: dict[str, str], headers: dict[str, str] | None = None
    ) -> Any:
        records = [{"student_id": self.sid(k), "status": v} for k, v in entries.items()]
        return self.client.post(
            f"{ATTENDANCE}/sessions/{session_id}/records/bulk",
            headers=headers or self.admin,
            json={"records": records},
        )

    def patch(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        return self.client.patch(f"{ATTENDANCE}{path}", headers=headers or self.admin, json=body)

    def get(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        return self.client.get(f"{ATTENDANCE}{path}", headers=headers or self.admin, params=params)

    def ids(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> set[str]:
        response = self.get(path, headers, **params)
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    def parent_of(self, *students: str) -> dict[str, str]:
        mobile = f"+9196{next(_mobiles):08d}"
        parent = self.w.parent(mobile=mobile).json()
        for student in students:
            assert self.w.link(self.sid(student), parent["id"]).status_code == 201
        return self.w.parent_headers(mobile)


@pytest.fixture
def aworld(fworld: World) -> AttendanceWorld:
    w = fworld
    faculty_ids: dict[str, str] = {}
    headers: dict[str, dict[str, str]] = {}
    for key, sec, subject in (
        ("f", "section_a", "math_a"),
        ("g", "section_a", "sci_a"),
        ("h", "section_a_b", "math_a"),
    ):
        email = f"att-{key}@school.test"
        faculty_ids[key] = w.faculty(email=email).json()["id"]
        assert w.assign(faculty_ids[key], sec=sec, subject=subject).status_code == 201
        headers[key] = w.faculty_headers(email)

    s: dict[str, str] = {}
    e: dict[str, str] = {}

    def add(
        key: str, adm: str, admin: dict[str, str] | None = None, *, login: bool = False, **kw: Any
    ) -> None:
        password = STUDENT_PASSWORD if login else None
        s[key] = w.student(adm, admin, password=password).json()["id"]
        response = w.enroll(s[key], admin, **kw)
        assert response.status_code == 201, response.text
        e[key] = response.json()["id"]

    add("a1", "A1", login=True)
    add("a2", "A2", login=True)
    add("ab", "AB", sec="section_a_b")
    add("c2", "C2", cls="class2_a", sec="section2_a")
    add("prev", "PREV", year="year_a_prev", date="2025-06-10", current=False)
    add("b", "B1", w.admin_b, year="year_b", cls="class_b", sec="section_b")
    return AttendanceWorld(
        w=w, f=headers["f"], g=headers["g"], h=headers["h"], faculty_ids=faculty_ids, s=s, e=e
    )
