"""Parent -> child and student -> self authorization, including IDOR-style attacks.

Every test signs in as a real parent (OTP) or student (admission number +
password) over HTTP and tries to reach records it must not see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tests.integration.conftest import STUDENT_PASSWORD, World

pytestmark = pytest.mark.db

PARENT_A_MOBILE = "+919811100001"
PARENT_B_MOBILE = "+919811100002"


@dataclass
class Family:
    child_a: dict[str, Any]  # child of parent A
    child_b: dict[str, Any]  # child of parent B (same school)
    child_other_school: dict[str, Any]
    parent_a: dict[str, Any]
    parent_b: dict[str, Any]
    enrollment_a: dict[str, Any]
    enrollment_b: dict[str, Any]
    parent_a_headers: dict[str, str]
    student_a_headers: dict[str, str]


@pytest.fixture
def family(world: World) -> Family:
    child_a = world.student("CA", password=STUDENT_PASSWORD).json()
    child_b = world.student("CB", password=STUDENT_PASSWORD).json()
    child_other = world.student("CX", world.admin_b, password=STUDENT_PASSWORD).json()
    parent_a = world.parent(mobile=PARENT_A_MOBILE).json()
    parent_b = world.parent(mobile=PARENT_B_MOBILE).json()
    world.link(child_a["id"], parent_a["id"])
    world.link(child_b["id"], parent_b["id"])
    enrollment_a = world.enroll(child_a["id"]).json()
    enrollment_b = world.enroll(child_b["id"]).json()
    return Family(
        child_a,
        child_b,
        child_other,
        parent_a,
        parent_b,
        enrollment_a,
        enrollment_b,
        world.parent_headers(PARENT_A_MOBILE),
        world.student_headers("CA"),
    )


def _get(world: World, headers: dict[str, str], path: str) -> Any:
    return world.client.get(f"/api/v1{path}", headers=headers)


# ============================================================================ parent
def test_parent_sees_only_own_child(world: World, family: Family) -> None:
    h = family.parent_a_headers
    assert _get(world, h, f"/students/{family.child_a['id']}").status_code == 200
    listing = _get(world, h, "/students").json()
    assert [s["id"] for s in listing["items"]] == [family.child_a["id"]]


@pytest.mark.parametrize("target", ["child_b", "child_other_school"])
def test_parent_cannot_read_other_children(world: World, family: Family, target: str) -> None:
    other = getattr(family, target)
    h = family.parent_a_headers
    for path in (f"/students/{other['id']}", f"/students/{other['id']}/parents"):
        response = _get(world, h, path)
        assert response.status_code == 404, path
    # Filters cannot widen scope.
    assert (
        _get(world, h, f"/students?admission_number={other['admission_number']}").json()["total"]
        == 0
    )
    assert _get(world, h, f"/enrollments?student_id={other['id']}").json()["total"] == 0


def test_parent_cannot_read_other_parents(world: World, family: Family) -> None:
    h = family.parent_a_headers
    assert _get(world, h, f"/parents/{family.parent_a['id']}").status_code == 200
    assert _get(world, h, f"/parents/{family.parent_b['id']}").status_code == 404
    assert _get(world, h, f"/parents/{family.parent_b['id']}/children").status_code == 404
    assert [p["id"] for p in _get(world, h, "/parents").json()["items"]] == [family.parent_a["id"]]


def test_parent_sees_only_own_link_not_co_parents(world: World, family: Family) -> None:
    co_parent = world.parent().json()
    world.link(family.child_a["id"], co_parent["id"], relationship_type="father")
    links = _get(world, family.parent_a_headers, f"/students/{family.child_a['id']}/parents").json()
    assert [link["parent_id"] for link in links] == [family.parent_a["id"]]
    children = _get(
        world, family.parent_a_headers, f"/parents/{family.parent_a['id']}/children"
    ).json()
    assert [c["student_id"] for c in children] == [family.child_a["id"]]


def test_parent_enrollment_access(world: World, family: Family) -> None:
    h = family.parent_a_headers
    assert _get(world, h, f"/enrollments/{family.enrollment_a['id']}").status_code == 200
    # IDOR: swapping the enrollment id for another child's.
    assert _get(world, h, f"/enrollments/{family.enrollment_b['id']}").status_code == 404
    items = _get(world, h, "/enrollments").json()["items"]
    assert [e["id"] for e in items] == [family.enrollment_a["id"]]


def test_parent_cannot_write_anything(world: World, family: Family) -> None:
    h = family.parent_a_headers
    child = family.child_a["id"]
    assert world.student("NEW", h).status_code == 403
    assert (
        world.client.patch(
            f"/api/v1/students/{child}", headers=h, json={"first_name": "X"}
        ).status_code
        == 403
    )
    assert world.parent(h).status_code == 403
    assert (
        world.client.patch(
            f"/api/v1/parents/{family.parent_a['id']}", headers=h, json={"first_name": "X"}
        ).status_code
        == 403
    )
    assert world.link(child, family.parent_b["id"], headers=h).status_code == 403
    assert world.enroll(child, h).status_code == 403
    assert (
        world.client.patch(
            f"/api/v1/enrollments/{family.enrollment_a['id']}",
            headers=h,
            json={"status": "withdrawn"},
        ).status_code
        == 403
    )


def test_parent_access_ends_when_link_or_parent_deactivated(world: World, family: Family) -> None:
    h = family.parent_a_headers
    link_path = f"/api/v1/students/{family.child_a['id']}/parents/{family.parent_a['id']}"
    world.client.patch(link_path, headers=world.admin_a, json={"is_active": False})
    assert _get(world, h, f"/students/{family.child_a['id']}").status_code == 404
    assert _get(world, h, "/students").json()["total"] == 0
    world.client.patch(link_path, headers=world.admin_a, json={"is_active": True})
    assert _get(world, h, f"/students/{family.child_a['id']}").status_code == 200

    # Deactivating the parent ends their session immediately and blocks OTP sign-in.
    world.client.patch(
        f"/api/v1/parents/{family.parent_a['id']}", headers=world.admin_a, json={"is_active": False}
    )
    assert _get(world, h, "/students").status_code == 401
    world.client.post("/api/v1/auth/otp/request", json={"phone": PARENT_A_MOBILE})
    sent_after = [phone for phone, _ in world.outbox.sent].count(PARENT_A_MOBILE)
    assert sent_after == 1  # only the original sign-in; no code for the inactive parent


def test_parent_school_mismatch_header_is_404(world: World, family: Family) -> None:
    headers = {**family.parent_a_headers, "X-School-ID": "00000000-0000-4000-8000-00000000000b"}
    assert _get(world, headers, "/students").status_code == 404


# =========================================================================== student
def test_student_sees_only_self(world: World, family: Family) -> None:
    h = family.student_a_headers
    assert _get(world, h, f"/students/{family.child_a['id']}").status_code == 200
    assert [s["id"] for s in _get(world, h, "/students").json()["items"]] == [family.child_a["id"]]
    for other in (family.child_b, family.child_other_school):
        assert _get(world, h, f"/students/{other['id']}").status_code == 404
        assert _get(world, h, f"/students/{other['id']}/parents").status_code == 404


def test_student_enrollment_access(world: World, family: Family) -> None:
    h = family.student_a_headers
    assert _get(world, h, f"/enrollments/{family.enrollment_a['id']}").status_code == 200
    assert _get(world, h, f"/enrollments/{family.enrollment_b['id']}").status_code == 404
    assert _get(world, h, f"/enrollments?student_id={family.child_b['id']}").json()["total"] == 0


def test_student_parent_records(world: World, family: Family) -> None:
    h = family.student_a_headers
    own = _get(world, h, f"/students/{family.child_a['id']}/parents")
    assert [link["parent_id"] for link in own.json()] == [family.parent_a["id"]]
    # Parent records themselves are not a student resource.
    assert _get(world, h, f"/parents/{family.parent_a['id']}").status_code == 403
    assert _get(world, h, "/parents").status_code == 403


def test_student_cannot_modify_anything(world: World, family: Family) -> None:
    h = family.student_a_headers
    own = f"/api/v1/students/{family.child_a['id']}"
    assert world.client.patch(own, headers=h, json={"first_name": "Me"}).status_code == 403
    assert world.client.patch(own, headers=h, json={"status": "graduated"}).status_code == 403
    assert world.student("NEW", h).status_code == 403
    assert world.enroll(family.child_a["id"], h).status_code == 403
    assert world.link(family.child_a["id"], family.parent_b["id"], headers=h).status_code == 403


def test_inactive_student_session_ends(world: World, family: Family) -> None:
    world.client.patch(
        f"/api/v1/students/{family.child_a['id']}",
        headers=world.admin_a,
        json={"status": "transferred"},
    )
    assert _get(world, family.student_a_headers, "/students").status_code == 401


# ====================================================================== school admin
def test_admin_cannot_reach_other_school(world: World, family: Family) -> None:
    other = family.child_other_school["id"]
    assert _get(world, world.admin_a, f"/students/{other}").status_code == 404
    assert (
        world.client.patch(
            f"/api/v1/students/{other}", headers=world.admin_a, json={"first_name": "X"}
        ).status_code
        == 404
    )
    response = world.enroll(other)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Student not found."


def test_admin_cannot_enroll_into_other_schools_structure(world: World, family: Family) -> None:
    child = world.student("FRESH").json()["id"]
    for field_name, key in (("year", "year_b"), ("cls", "class_b"), ("sec", "section_b")):
        response = world.enroll(child, **{field_name: key})
        assert response.status_code == 422, (field_name, response.text)
