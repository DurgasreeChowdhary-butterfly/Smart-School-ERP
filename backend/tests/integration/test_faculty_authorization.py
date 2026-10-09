"""Faculty resource scope over HTTP: assigned students/sections/subjects only, IDOR attempts,
filters that must not widen access, personal-data boundaries and lifecycle effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory
from tests.integration.conftest import STUDENT_PASSWORD, World

pytestmark = pytest.mark.db
R = RoleName


@dataclass
class Scene:
    w: World
    f: dict[str, str]  # Faculty F's headers: teaches year_a / class_a / section_a / math
    f_id: str
    f_assignment: str
    g_id: str  # Faculty G: teaches class_a / section_a_b / math
    g_assignment: str
    s: dict[str, str]  # student id by key
    e: dict[str, str]  # enrollment id by key
    parent_id: str

    def get(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        return self.w.client.get(f"/api/v1{path}", headers=headers or self.f, params=params)

    def ids(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> set[str]:
        response = self.get(path, headers, **params)
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}


@pytest.fixture
def scene(fworld: World) -> Scene:
    w = fworld
    f = w.faculty(email="f@school.test").json()
    g = w.faculty(email="g@school.test").json()
    f_assignment = w.assign(f["id"]).json()["id"]
    g_assignment = w.assign(g["id"], sec="section_a_b").json()["id"]

    s: dict[str, str] = {}
    e: dict[str, str] = {}
    s["in"] = w.student(
        "IN-1",
        password=STUDENT_PASSWORD,
        email="in1@mail.example",
        phone="+919811100001",
        address="1 Private Lane",
        date_of_birth="2012-01-01",
    ).json()["id"]
    e["in"] = w.enroll(s["in"]).json()["id"]
    s["other_section"] = w.student("SEC-B").json()["id"]
    e["other_section"] = w.enroll(s["other_section"], sec="section_a_b").json()["id"]
    s["other_class"] = w.student("CLS-2").json()["id"]
    e["other_class"] = w.enroll(s["other_class"], cls="class2_a", sec="section2_a").json()["id"]
    # Same class/section, but the previous academic year only.
    s["prev_year"] = w.student("PREV-1").json()["id"]
    e["prev_year"] = w.enroll(
        s["prev_year"], year="year_a_prev", date="2025-06-10", current=False
    ).json()["id"]
    s["unenrolled"] = w.student("NONE-1").json()["id"]
    s["school_b"] = w.student("B-1", w.admin_b).json()["id"]
    e["school_b"] = w.enroll(
        s["school_b"], w.admin_b, year="year_b", cls="class_b", sec="section_b"
    ).json()["id"]
    parent = w.parent(mobile="+919877700001", occupation="Engineer").json()
    assert w.link(s["in"], parent["id"]).status_code == 201
    return Scene(
        w=w,
        f=w.faculty_headers("f@school.test"),
        f_id=f["id"],
        f_assignment=f_assignment,
        g_id=g["id"],
        g_assignment=g_assignment,
        s=s,
        e=e,
        parent_id=parent["id"],
    )


# ========================================================================= students
def test_faculty_sees_only_assigned_students(scene: Scene) -> None:
    assert scene.ids("/students") == {scene.s["in"]}
    assert scene.get(f"/students/{scene.s['in']}").status_code == 200


@pytest.mark.parametrize(
    "key", ["other_section", "other_class", "prev_year", "unenrolled", "school_b"]
)
def test_unassigned_student_by_id_is_not_found(scene: Scene, key: str) -> None:
    """IDOR: swapping in another section's/class's/year's/school's student id."""
    assert scene.get(f"/students/{scene.s[key]}").status_code == 404
    assert scene.get(f"/students/{scene.s[key]}/parents").status_code == 404


@pytest.mark.parametrize(
    ("param", "key"),
    [
        ("section_id", "section_a_b"),
        ("class_id", "class2_a"),
        ("academic_year_id", "year_a_prev"),
        ("section_id", "section_b"),
        ("class_id", "class_b"),
    ],
)
def test_filters_cannot_widen_student_access(scene: Scene, param: str, key: str) -> None:
    """GET /students?section_id=<another section> must not reveal that section's students."""
    response = scene.get("/students", **{param: scene.w.ids[key]})
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_other_school_selection_is_refused(scene: Scene) -> None:
    headers = {**scene.f, "X-School-ID": str(SCHOOL_B)}
    assert scene.get("/students", headers).status_code == 404
    assert scene.get(f"/students/{scene.s['school_b']}", headers).status_code == 404


def test_student_contact_details_hidden_from_faculty(scene: Scene) -> None:
    body = scene.get(f"/students/{scene.s['in']}").json()
    for field in ("email", "phone", "address", "city", "state", "postal_code", "country"):
        assert body[field] is None, field
    assert body["admission_number"] == "IN-1"
    assert body["date_of_birth"] == "2012-01-01"
    listed = scene.get("/students").json()["items"][0]
    assert listed["address"] is None
    assert "1 Private Lane" not in scene.get("/students").text
    # Staff and the student themself still see everything.
    admin_view = scene.get(f"/students/{scene.s['in']}", scene.w.admin_a).json()
    assert admin_view["address"] == "1 Private Lane"
    own = scene.get(f"/students/{scene.s['in']}", scene.w.student_headers("IN-1")).json()
    assert own["phone"] == "+919811100001"


def test_faculty_never_reaches_parent_personal_data(scene: Scene) -> None:
    links = scene.get(f"/students/{scene.s['in']}/parents")
    assert links.status_code == 200
    assert links.json() == []
    assert "+919877700001" not in links.text
    assert scene.get("/parents").status_code == 403
    assert scene.get(f"/parents/{scene.parent_id}").status_code == 403
    assert scene.get(f"/parents/{scene.parent_id}/children").status_code == 403


def test_faculty_cannot_write_student_data(scene: Scene) -> None:
    client = scene.w.client
    sid = scene.s["in"]
    assert (
        client.patch(
            f"/api/v1/students/{sid}", headers=scene.f, json={"first_name": "X"}
        ).status_code
        == 403
    )
    assert scene.w.enroll(sid, scene.f, sec="section_a_b").status_code == 403
    assert scene.w.link(sid, scene.parent_id, scene.f).status_code == 403


# ====================================================================== enrollments
def test_faculty_sees_only_matching_enrollments(scene: Scene) -> None:
    assert scene.ids("/enrollments") == {scene.e["in"]}
    assert scene.get(f"/enrollments/{scene.e['in']}").status_code == 200
    for key in ("other_section", "other_class", "prev_year", "school_b"):
        assert scene.get(f"/enrollments/{scene.e[key]}").status_code == 404
    assert scene.ids("/enrollments", student_id=scene.s["other_section"]) == set()
    assert scene.ids("/enrollments", section_id=scene.w.ids["section_a_b"]) == set()


def test_moving_a_student_moves_faculty_access(scene: Scene, auth_headers: HeadersFactory) -> None:
    """Scope follows the *active* enrollment: after a section change, F loses the student
    (and does not see the superseded history row); G, who teaches the new section, gains it."""
    moved = scene.w.enroll(
        scene.s["in"], sec="section_a_b", date="2026-09-01", supersedes_enrollment_id=scene.e["in"]
    )
    assert moved.status_code == 201, moved.text
    assert scene.get(f"/students/{scene.s['in']}").status_code == 404
    assert scene.ids("/enrollments") == set()
    g = scene.w.faculty_headers("g@school.test")
    assert scene.ids("/students", g) == {scene.s["in"], scene.s["other_section"]}
    assert scene.ids("/enrollments", g) == {moved.json()["id"], scene.e["other_section"]}


def test_withdrawn_enrollment_removes_access(scene: Scene) -> None:
    scene.w.client.patch(
        f"/api/v1/enrollments/{scene.e['in']}",
        headers=scene.w.admin_a,
        json={"status": "withdrawn", "end_date": "2026-09-30"},
    )
    assert scene.ids("/students") == set()


# ================================================================ academic structure
@pytest.mark.parametrize(
    ("path", "visible", "hidden"),
    [
        ("/sections", "section_a", ["section_a_b", "section2_a", "section_b"]),
        ("/classes", "class_a", ["class2_a", "class_b"]),
        ("/subjects", "math_a", ["sci_a", "math_b"]),
        ("/academic-years", "year_a", ["year_a_prev", "year_b"]),
    ],
)
def test_faculty_sees_only_assigned_structure(
    scene: Scene, path: str, visible: str, hidden: list[str]
) -> None:
    ids = scene.w.ids
    assert scene.ids(path) == {ids[visible]}
    assert scene.get(f"{path}/{ids[visible]}").status_code == 200
    for key in hidden:
        assert scene.get(f"{path}/{ids[key]}").status_code == 404, key


def test_structure_filters_cannot_widen(scene: Scene) -> None:
    assert scene.ids("/sections", class_id=scene.w.ids["class2_a"]) == set()
    assert scene.ids("/sections", class_id=scene.w.ids["class_a"]) == {scene.w.ids["section_a"]}


@pytest.mark.parametrize("role", [R.PARENT, R.STUDENT, R.MANAGEMENT])
def test_other_roles_keep_school_wide_structure(
    scene: Scene, auth_headers: HeadersFactory, role: RoleName
) -> None:
    """Phase 3 behaviour is unchanged for everyone but faculty."""
    headers = auth_headers(role)
    assert len(scene.ids("/sections", headers)) == 3
    assert len(scene.ids("/subjects", headers)) == 2


# ===================================================================== assignments
def test_faculty_sees_only_own_assignments(scene: Scene) -> None:
    assert scene.ids("/faculty-assignments") == {scene.f_assignment}
    assert scene.get(f"/faculty-assignments/{scene.f_assignment}").status_code == 200
    assert scene.get(f"/faculty-assignments/{scene.g_assignment}").status_code == 404
    assert scene.ids("/faculty-assignments", faculty_id=scene.g_id) == set()
    assert scene.ids("/faculty-assignments", section_id=scene.w.ids["section_a_b"]) == set()
    assert scene.get(f"/faculty/{scene.g_id}").status_code == 404
    patch = scene.w.client.patch(
        f"/api/v1/faculty-assignments/{scene.f_assignment}",
        headers=scene.f,
        json={"status": "inactive"},
    )
    assert patch.status_code == 403


def test_me_assignments_lists_effective_scope(scene: Scene) -> None:
    mine = scene.ids("/faculty/me/assignments")
    assert mine == {scene.f_assignment}
    me = scene.get("/faculty/me").json()
    assert me["id"] == scene.f_id


def test_cross_school_faculty_cannot_reach_school_a(scene: Scene) -> None:
    w = scene.w
    b_teacher = w.faculty(w.admin_b, email="b@school.test").json()
    w.assign(
        b_teacher["id"], w.admin_b, year="year_b", cls="class_b", sec="section_b", subject="math_b"
    )
    b = w.faculty_headers("b@school.test")
    assert scene.ids("/students", b) == {scene.s["school_b"]}
    assert scene.get(f"/students/{scene.s['in']}", b).status_code == 404
    assert scene.get(f"/faculty/{scene.f_id}", b).status_code == 404
    assert scene.get(f"/faculty-assignments/{scene.f_assignment}", b).status_code == 404
    assert scene.get(f"/sections/{w.ids['section_a']}", b).status_code == 404
    assert scene.get("/students", {**b, "X-School-ID": str(SCHOOL_A)}).status_code == 404


# ================================================================ multiple / lifecycle
def test_multiple_assignments_union_without_widening(scene: Scene) -> None:
    w = scene.w
    w.assign(scene.f_id, sec="section_a_b", subject="sci_a")
    w.assign(scene.f_id, cls="class2_a", sec="section2_a")
    assert scene.ids("/students") == {
        scene.s["in"],
        scene.s["other_section"],
        scene.s["other_class"],
    }
    assert scene.ids("/sections") == {
        w.ids["section_a"],
        w.ids["section_a_b"],
        w.ids["section2_a"],
    }
    # Still nothing from another year or another school.
    assert scene.get(f"/students/{scene.s['prev_year']}").status_code == 404
    assert scene.get(f"/students/{scene.s['school_b']}").status_code == 404


def test_deactivated_assignment_revokes_and_reactivation_restores(scene: Scene) -> None:
    url = f"/api/v1/faculty-assignments/{scene.f_assignment}"
    scene.w.client.patch(url, headers=scene.w.admin_a, json={"status": "inactive"})
    assert scene.ids("/students") == set()
    assert scene.ids("/sections") == set()
    assert scene.ids("/faculty/me/assignments") == set()
    # History stays visible to its owner.
    assert scene.ids("/faculty-assignments") == {scene.f_assignment}
    scene.w.client.patch(url, headers=scene.w.admin_a, json={"status": "active"})
    assert scene.ids("/students") == {scene.s["in"]}


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE faculty_assignments SET effective_from = CURRENT_DATE + 2 WHERE id = :a",
        "UPDATE faculty_assignments SET effective_from = CURRENT_DATE - 30, "
        "effective_to = CURRENT_DATE - 2 WHERE id = :a",
    ],
)
def test_assignment_outside_effective_window_grants_nothing(
    scene: Scene, db_session: Session, sql: str
) -> None:
    db_session.execute(text(sql), {"a": scene.f_assignment})
    assert scene.ids("/students") == set()
    assert scene.ids("/subjects") == set()
    assert scene.ids("/faculty/me/assignments") == set()
    db_session.execute(
        text(
            "UPDATE faculty_assignments SET effective_from = CURRENT_DATE - 2, "
            "effective_to = CURRENT_DATE + 2 WHERE id = :a"
        ),
        {"a": scene.f_assignment},
    )
    assert scene.ids("/students") == {scene.s["in"]}


def test_parent_and_student_scopes_unchanged(scene: Scene) -> None:
    parent = scene.w.parent_headers("+919877700001")
    assert scene.ids("/students", parent) == {scene.s["in"]}
    student = scene.w.student_headers("IN-1")
    assert scene.ids("/students", student) == {scene.s["in"]}
    assert scene.get(f"/students/{scene.s['other_section']}", student).status_code == 404
    assert scene.get("/faculty", parent).status_code == 403
    assert scene.get("/faculty-assignments", student).status_code == 403
