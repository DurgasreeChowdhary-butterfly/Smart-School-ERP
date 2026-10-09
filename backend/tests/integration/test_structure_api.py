"""Classes, sections and subjects over the API."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory

pytestmark = pytest.mark.db

R = RoleName
MISSING = "00000000-0000-4000-8000-000000000777"


@pytest.fixture
def admin(auth_headers: HeadersFactory) -> dict[str, str]:
    return auth_headers(R.SCHOOL_ADMIN)


@pytest.fixture
def school_b_root(auth_headers: HeadersFactory) -> dict[str, str]:
    return auth_headers(R.SUPER_ADMIN, SCHOOL_B)


def _class(client: TestClient, headers: dict[str, str], code: str = "C10", **extra: Any) -> Any:
    body = {"name": f"Class {code}", "code": code, **extra}
    return client.post("/api/v1/classes", headers=headers, json=body)


def _section(
    client: TestClient, headers: dict[str, str], class_id: str, code: str = "A", **extra: Any
) -> Any:
    body = {"class_id": class_id, "name": code, "code": code, **extra}
    return client.post("/api/v1/sections", headers=headers, json=body)


def _subject(client: TestClient, headers: dict[str, str], code: str = "MATH", **extra: Any) -> Any:
    body = {"name": "Mathematics", "code": code, **extra}
    return client.post("/api/v1/subjects", headers=headers, json=body)


def _actions(db_session: Session, prefix: str) -> list[str]:
    return list(
        db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.action.like(f"{prefix}.%"))
            .order_by(AuditLog.created_at)
        )
    )


# ========================================================================== classes
def test_class_create_normalises_and_lists_in_display_order(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    created = _class(db_client, admin, " c10 ", display_order=10)
    assert created.status_code == 201
    assert created.json()["code"] == "C10"
    _class(db_client, admin, "C1", display_order=1)
    _class(db_client, admin, "C2", display_order=2)
    items = db_client.get("/api/v1/classes", headers=admin).json()["items"]
    assert [c["code"] for c in items] == ["C1", "C2", "C10"]
    assert all(c["school_id"] == str(SCHOOL_A) for c in items)


def test_class_duplicate_code_rejected_even_with_different_case(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    _class(db_client, admin, "C10")
    response = _class(db_client, admin, "c10")
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["loc"] == ["body", "code"]


def test_class_code_unique_per_school_only(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    assert _class(db_client, admin).status_code == 201
    assert _class(db_client, school_b_root).status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Class 1", "code": "has space"},
        {"name": "Class 1", "code": "-C1"},
        {"name": "Class 1", "code": "   "},
        {"name": "   ", "code": "C1"},
        {"name": "Class 1", "code": "C1", "display_order": -1},
        {"name": "Class 1", "code": "C1", "school_id": str(SCHOOL_B)},
    ],
)
def test_class_validation(
    db_client: TestClient, admin: dict[str, str], body: dict[str, Any]
) -> None:
    assert db_client.post("/api/v1/classes", headers=admin, json=body).status_code == 422


def test_class_update_and_deactivate(
    db_client: TestClient, admin: dict[str, str], db_session: Session
) -> None:
    item = _class(db_client, admin).json()
    path = f"/api/v1/classes/{item['id']}"
    assert (
        db_client.patch(path, headers=admin, json={"name": "Grade 10"}).json()["name"] == "Grade 10"
    )
    assert (
        db_client.patch(path, headers=admin, json={"is_active": False}).json()["is_active"] is False
    )
    active = db_client.get("/api/v1/classes?is_active=true", headers=admin).json()
    inactive = db_client.get("/api/v1/classes?is_active=false", headers=admin).json()
    assert (active["total"], inactive["total"]) == (0, 1)
    assert db_client.patch(path, headers=admin, json={"is_active": True}).status_code == 200
    assert _actions(db_session, "class") == [
        "class.created",
        "class.updated",
        "class.deactivated",
        "class.reactivated",
    ]


def test_class_with_active_sections_cannot_be_deactivated(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    item = _class(db_client, admin).json()
    section = _section(db_client, admin, item["id"]).json()
    path = f"/api/v1/classes/{item['id']}"
    assert db_client.patch(path, headers=admin, json={"is_active": False}).status_code == 409
    db_client.patch(f"/api/v1/sections/{section['id']}", headers=admin, json={"is_active": False})
    assert db_client.patch(path, headers=admin, json={"is_active": False}).status_code == 200


def test_class_update_duplicate_code_conflicts(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    _class(db_client, admin, "C9")
    item = _class(db_client, admin, "C10").json()
    response = db_client.patch(f"/api/v1/classes/{item['id']}", headers=admin, json={"code": "c9"})
    assert response.status_code == 409


def test_class_cross_school_isolation(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    b_class = _class(db_client, school_b_root).json()
    path = f"/api/v1/classes/{b_class['id']}"
    assert db_client.get(path, headers=admin).status_code == 404
    assert db_client.patch(path, headers=admin, json={"name": "hijack"}).status_code == 404
    assert db_client.get("/api/v1/classes", headers=admin).json()["total"] == 0
    assert db_client.get(path, headers=school_b_root).json()["name"] == "Class C10"


# ========================================================================= sections
def test_section_create_and_filter_by_class(db_client: TestClient, admin: dict[str, str]) -> None:
    c9 = _class(db_client, admin, "C9").json()
    c10 = _class(db_client, admin, "C10").json()
    created = _section(db_client, admin, c10["id"], "a", capacity=40)
    assert created.status_code == 201
    assert created.json()["code"] == "A"
    assert created.json()["capacity"] == 40
    _section(db_client, admin, c10["id"], "B")
    _section(db_client, admin, c9["id"], "A")  # same code, different class: allowed

    only_c10 = db_client.get(f"/api/v1/sections?class_id={c10['id']}", headers=admin).json()
    assert [s["code"] for s in only_c10["items"]] == ["A", "B"]
    assert db_client.get("/api/v1/sections", headers=admin).json()["total"] == 3


def test_section_duplicate_code_in_class_rejected(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    item = _class(db_client, admin).json()
    _section(db_client, admin, item["id"], "A")
    assert _section(db_client, admin, item["id"], "a").status_code == 409


@pytest.mark.parametrize("capacity", [0, -5, 10_001, "many"])
def test_section_capacity_must_be_positive(
    db_client: TestClient, admin: dict[str, str], capacity: object
) -> None:
    item = _class(db_client, admin).json()
    assert _section(db_client, admin, item["id"], capacity=capacity).status_code == 422


def test_section_capacity_can_be_cleared(db_client: TestClient, admin: dict[str, str]) -> None:
    item = _class(db_client, admin).json()
    section = _section(db_client, admin, item["id"], capacity=30).json()
    response = db_client.patch(
        f"/api/v1/sections/{section['id']}", headers=admin, json={"capacity": None}
    )
    assert response.json()["capacity"] is None


def test_section_cannot_use_another_schools_class(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    b_class = _class(db_client, school_b_root).json()
    foreign = _section(db_client, admin, b_class["id"])
    unknown = _section(db_client, admin, MISSING)
    # Identical answers: a foreign class is indistinguishable from a missing one.
    assert foreign.status_code == unknown.status_code == 422
    assert (
        foreign.json()["error"]["message"]
        == unknown.json()["error"]["message"]
        == "Class not found."
    )
    assert foreign.json()["error"]["details"][0]["loc"] == ["body", "class_id"]
    assert db_client.get("/api/v1/sections", headers=school_b_root).json()["total"] == 0


def test_section_under_inactive_class_rejected(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    item = _class(db_client, admin).json()
    db_client.patch(f"/api/v1/classes/{item['id']}", headers=admin, json={"is_active": False})
    response = _section(db_client, admin, item["id"])
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Class is inactive."


def test_section_reactivation_requires_active_class(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    item = _class(db_client, admin).json()
    section = _section(db_client, admin, item["id"]).json()
    spath = f"/api/v1/sections/{section['id']}"
    db_client.patch(spath, headers=admin, json={"is_active": False})
    db_client.patch(f"/api/v1/classes/{item['id']}", headers=admin, json={"is_active": False})
    assert db_client.patch(spath, headers=admin, json={"is_active": True}).status_code == 422


@pytest.mark.parametrize("body", [{"class_id": MISSING}, {"capacity": 0}, {}, {"code": None}])
def test_section_update_validation(
    db_client: TestClient, admin: dict[str, str], body: dict[str, Any]
) -> None:
    item = _class(db_client, admin).json()
    section = _section(db_client, admin, item["id"]).json()
    response = db_client.patch(f"/api/v1/sections/{section['id']}", headers=admin, json=body)
    assert response.status_code == 422


def test_section_cross_school_isolation(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    b_class = _class(db_client, school_b_root).json()
    b_section = _section(db_client, school_b_root, b_class["id"]).json()
    path = f"/api/v1/sections/{b_section['id']}"
    assert db_client.get(path, headers=admin).status_code == 404
    assert db_client.patch(path, headers=admin, json={"capacity": 99}).status_code == 404
    listing = db_client.get(f"/api/v1/sections?class_id={b_class['id']}", headers=admin).json()
    assert listing["total"] == 0


def test_section_events_audited(
    db_client: TestClient, admin: dict[str, str], db_session: Session
) -> None:
    item = _class(db_client, admin).json()
    section = _section(db_client, admin, item["id"]).json()
    spath = f"/api/v1/sections/{section['id']}"
    db_client.patch(spath, headers=admin, json={"name": "Alpha"})
    db_client.patch(spath, headers=admin, json={"is_active": False})
    assert _actions(db_session, "section") == [
        "section.created",
        "section.updated",
        "section.deactivated",
    ]


# ========================================================================= subjects
def test_subject_crud(db_client: TestClient, admin: dict[str, str], db_session: Session) -> None:
    created = _subject(db_client, admin, " math ", description="  Core maths ")
    assert created.status_code == 201
    body = created.json()
    assert (body["code"], body["description"]) == ("MATH", "Core maths")

    path = f"/api/v1/subjects/{body['id']}"
    updated = db_client.patch(path, headers=admin, json={"name": "Maths", "description": None})
    assert updated.json()["name"] == "Maths"
    assert updated.json()["description"] is None
    db_client.patch(path, headers=admin, json={"is_active": False})

    assert db_client.get("/api/v1/subjects?is_active=true", headers=admin).json()["total"] == 0
    assert db_client.get(path, headers=admin).json()["is_active"] is False
    assert _actions(db_session, "subject") == [
        "subject.created",
        "subject.updated",
        "subject.deactivated",
    ]


def test_subjects_list_sorted_by_name(db_client: TestClient, admin: dict[str, str]) -> None:
    db_client.post("/api/v1/subjects", headers=admin, json={"name": "Physics", "code": "PHY"})
    db_client.post("/api/v1/subjects", headers=admin, json={"name": "English", "code": "ENG"})
    items = db_client.get("/api/v1/subjects", headers=admin).json()["items"]
    assert [s["name"] for s in items] == ["English", "Physics"]


def test_subject_duplicate_code_rejected(db_client: TestClient, admin: dict[str, str]) -> None:
    _subject(db_client, admin)
    assert _subject(db_client, admin, "Math").status_code == 409


def test_subject_validation(db_client: TestClient, admin: dict[str, str]) -> None:
    assert _subject(db_client, admin, description="x" * 1001).status_code == 422
    assert _subject(db_client, admin, code="M@TH").status_code == 422


def test_subject_cross_school_isolation(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    b_subject = _subject(db_client, school_b_root).json()
    path = f"/api/v1/subjects/{b_subject['id']}"
    assert db_client.get(path, headers=admin).status_code == 404
    assert db_client.patch(path, headers=admin, json={"name": "hijack"}).status_code == 404
    assert db_client.get("/api/v1/subjects", headers=admin).json()["total"] == 0


# ================================================================== RBAC (all three)
@pytest.mark.parametrize("role", [R.MANAGEMENT, R.FACULTY, R.PARENT, R.STUDENT])
def test_non_managers_read_but_cannot_write(
    db_client: TestClient, auth_headers: HeadersFactory, admin: dict[str, str], role: RoleName
) -> None:
    headers = auth_headers(role)
    item = _class(db_client, admin).json()
    section = _section(db_client, admin, item["id"]).json()
    subject = _subject(db_client, admin).json()
    for path in ("/api/v1/classes", "/api/v1/sections", "/api/v1/subjects"):
        assert db_client.get(path, headers=headers).status_code == 200
    assert _class(db_client, headers, "C11").status_code == 403
    assert _section(db_client, headers, item["id"], "Z").status_code == 403
    assert _subject(db_client, headers, "BIO").status_code == 403
    for path in (
        f"/api/v1/classes/{item['id']}",
        f"/api/v1/sections/{section['id']}",
        f"/api/v1/subjects/{subject['id']}",
    ):
        assert db_client.patch(path, headers=headers, json={"name": "X"}).status_code == 403


def test_super_admin_manages_structure_in_selected_school(
    db_client: TestClient, auth_headers: HeadersFactory, admin: dict[str, str]
) -> None:
    root_a = auth_headers(R.SUPER_ADMIN, SCHOOL_A)
    created = _class(db_client, root_a)
    assert created.json()["school_id"] == str(SCHOOL_A)
    assert db_client.get("/api/v1/classes", headers=admin).json()["total"] == 1
    root_none = auth_headers(R.SUPER_ADMIN, select_school=False)
    assert db_client.get("/api/v1/classes", headers=root_none).status_code == 400
