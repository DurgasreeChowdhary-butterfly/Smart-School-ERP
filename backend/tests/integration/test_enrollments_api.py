"""Enrollment history, moves, current-enrollment switching and consistency rules."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from tests.integration.conftest import World

pytestmark = pytest.mark.db


def _list(world: World, query: str = "") -> list[dict[str, Any]]:
    response = world.client.get(f"/api/v1/enrollments{query}", headers=world.admin_a)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _patch(world: World, enrollment_id: str, body: dict[str, Any]) -> Any:
    return world.client.patch(
        f"/api/v1/enrollments/{enrollment_id}", headers=world.admin_a, json=body
    )


def test_create_and_read(world: World) -> None:
    student = world.student("S1").json()
    response = world.enroll(student["id"], enrollment_number="R-12")
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["status"], body["is_current"], body["enrollment_number"]) == (
        "active",
        True,
        "R-12",
    )
    fetched = world.client.get(f"/api/v1/enrollments/{body['id']}", headers=world.admin_a).json()
    assert fetched == body


def test_history_across_years_is_preserved(world: World, db_session: Session) -> None:
    student = world.student("S1").json()
    prev = world.enroll(student["id"], year="year_a_prev", date="2025-06-15").json()
    _patch(world, prev["id"], {"status": "completed", "end_date": "2026-05-31"})
    new = world.enroll(student["id"], cls="class2_a", sec="section2_a").json()

    history = _list(world, f"?student_id={student['id']}")
    assert [(e["academic_year_id"], e["status"], e["is_current"]) for e in history] == [
        (world.ids["year_a"], "active", True),
        (world.ids["year_a_prev"], "completed", False),
    ]
    assert new["class_id"] == world.ids["class2_a"]
    assert prev["class_id"] == world.ids["class_a"]  # never overwritten


def test_new_current_enrollment_unsets_previous(world: World, db_session: Session) -> None:
    student = world.student("S1").json()
    prev = world.enroll(student["id"], year="year_a_prev", date="2025-06-15").json()
    new = world.enroll(student["id"]).json()
    assert (
        world.client.get(f"/api/v1/enrollments/{prev['id']}", headers=world.admin_a).json()[
            "is_current"
        ]
        is False
    )
    assert [e["id"] for e in _list(world, "?current=true")] == [new["id"]]

    # Switch back explicitly via PATCH.
    response = _patch(world, prev["id"], {"is_current": True})
    assert response.json()["is_current"] is True
    assert [e["id"] for e in _list(world, "?current=true")] == [prev["id"]]
    changes = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "enrollment.current_changed")
        .order_by(AuditLog.created_at)
    ).all()
    assert [c.details["previous_current_id"] for c in changes] == [None, prev["id"], new["id"]]


def test_backfilled_history_does_not_take_current(world: World) -> None:
    student = world.student("S1").json()
    current = world.enroll(student["id"]).json()
    past = world.enroll(student["id"], year="year_a_prev", date="2025-06-15", current=False).json()
    assert past["is_current"] is False
    assert [e["id"] for e in _list(world, "?current=true")] == [current["id"]]


def test_second_active_enrollment_in_same_year_is_rejected(world: World) -> None:
    student = world.student("S1").json()
    world.enroll(student["id"])
    response = world.enroll(student["id"], cls="class2_a", sec="section2_a")
    assert response.status_code == 409
    assert "supersedes_enrollment_id" in response.json()["error"]["message"]


def test_mid_year_move_supersedes_without_losing_history(world: World, db_session: Session) -> None:
    student = world.student("S1").json()
    first = world.enroll(student["id"]).json()
    moved = world.enroll(
        student["id"],
        cls="class2_a",
        sec="section2_a",
        date="2026-11-01",
        supersedes_enrollment_id=first["id"],
        current=False,
    )
    assert moved.status_code == 201, moved.text
    old = world.client.get(f"/api/v1/enrollments/{first['id']}", headers=world.admin_a).json()
    assert (old["status"], old["end_date"], old["is_current"]) == (
        "superseded",
        "2026-11-01",
        False,
    )
    # Currency carries over to the replacement even though is_current was not requested.
    assert moved.json()["is_current"] is True
    assert (
        len(_list(world, f"?student_id={student['id']}&academic_year_id={world.ids['year_a']}"))
        == 2
    )
    event = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "enrollment.created")
        .order_by(AuditLog.created_at.desc())
    ).first()
    assert event is not None
    assert event.details["superseded_enrollment_id"] == first["id"]


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ({"supersedes_enrollment_id": "00000000-0000-4000-8000-000000000001"}, 422),  # no match
        ({"date": "2026-06-01"}, 422),  # before the superseded enrollment started
    ],
)
def test_invalid_supersede(world: World, extra: dict[str, Any], status: int) -> None:
    student = world.student("S1").json()
    first = world.enroll(student["id"], date="2026-06-10").json()
    body = {"supersedes_enrollment_id": first["id"], **extra}
    assert (
        world.enroll(student["id"], cls="class2_a", sec="section2_a", **body).status_code == status
    )


def test_supersedes_without_existing_active_enrollment(world: World) -> None:
    student = world.student("S1").json()
    response = world.enroll(
        student["id"], supersedes_enrollment_id="00000000-0000-4000-8000-000000000001"
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"sec": "section2_a"}, "section_id"),  # section of a different class
        ({"date": "2025-01-01"}, "enrollment_date"),  # outside the academic year
        ({"year": "year_b"}, "academic_year_id"),
        ({"cls": "class_b", "sec": "section_b"}, "class_id"),
    ],
)
def test_placement_consistency(world: World, overrides: dict[str, str], field: str) -> None:
    student = world.student("S1").json()
    response = world.enroll(student["id"], **overrides)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", field]


def test_inactive_structure_or_student_rejected(world: World) -> None:
    student = world.student("S1").json()
    section_path = f"/api/v1/sections/{world.ids['section_a']}"
    world.client.patch(section_path, headers=world.admin_a, json={"is_active": False})
    assert world.enroll(student["id"]).status_code == 422
    world.client.patch(section_path, headers=world.admin_a, json={"is_active": True})
    world.client.patch(
        f"/api/v1/students/{student['id']}", headers=world.admin_a, json={"status": "inactive"}
    )
    response = world.enroll(student["id"])
    assert response.status_code == 422
    assert "active students" in response.json()["error"]["message"]


def test_status_change_ends_currency(world: World) -> None:
    student = world.student("S1").json()
    enrollment = world.enroll(student["id"]).json()
    response = _patch(world, enrollment["id"], {"status": "withdrawn", "end_date": "2026-12-01"})
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["is_current"]) == ("withdrawn", False)
    assert _patch(world, enrollment["id"], {"is_current": True}).status_code == 422
    # Reactivating restores it as an active (not current) enrollment.
    assert (
        _patch(world, enrollment["id"], {"status": "active", "end_date": None}).json()["status"]
        == "active"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"class_id": "00000000-0000-4000-8000-000000000001"},
        {"section_id": "00000000-0000-4000-8000-000000000001"},
        {"academic_year_id": "00000000-0000-4000-8000-000000000001"},
        {"student_id": "00000000-0000-4000-8000-000000000001"},
        {"status": "superseded"},
        {"is_current": None},
        {},
    ],
)
def test_placement_is_immutable(world: World, body: dict[str, Any]) -> None:
    student = world.student("S1").json()
    enrollment = world.enroll(student["id"]).json()
    assert _patch(world, enrollment["id"], body).status_code == 422


def test_update_date_rules(world: World) -> None:
    student = world.student("S1").json()
    enrollment = world.enroll(student["id"]).json()
    assert _patch(world, enrollment["id"], {"enrollment_date": "2028-01-01"}).status_code == 422
    assert _patch(world, enrollment["id"], {"end_date": "2026-06-01"}).status_code == 422
    ok = _patch(world, enrollment["id"], {"enrollment_number": "R-7", "end_date": "2027-05-31"})
    assert ok.status_code == 200


def test_superseded_enrollment_is_read_only(world: World) -> None:
    student = world.student("S1").json()
    first = world.enroll(student["id"]).json()
    world.enroll(
        student["id"],
        cls="class2_a",
        sec="section2_a",
        date="2026-11-01",
        supersedes_enrollment_id=first["id"],
    )
    assert _patch(world, first["id"], {"status": "active"}).status_code == 422
    assert _patch(world, first["id"], {"is_current": True}).status_code == 422
    assert _patch(world, first["id"], {"enrollment_number": "OLD-1"}).status_code == 200


def test_reactivating_second_active_in_year_conflicts(world: World) -> None:
    student = world.student("S1").json()
    first = world.enroll(student["id"]).json()
    _patch(world, first["id"], {"status": "withdrawn"})
    world.enroll(student["id"], cls="class2_a", sec="section2_a")
    assert _patch(world, first["id"], {"status": "active"}).status_code == 409


def test_list_filters(world: World) -> None:
    s1, s2 = world.student("S1").json(), world.student("S2").json()
    e1 = world.enroll(s1["id"]).json()
    world.enroll(s2["id"], cls="class2_a", sec="section2_a")
    assert [e["id"] for e in _list(world, f"?class_id={world.ids['class_a']}")] == [e1["id"]]
    assert len(_list(world, f"?section_id={world.ids['section2_a']}")) == 1
    assert len(_list(world, f"?academic_year_id={world.ids['year_a']}&status=active")) == 2
    assert _list(world, f"?class_id={world.ids['class_b']}") == []


def test_enrollment_audit(world: World, db_session: Session) -> None:
    student = world.student("S1").json()
    enrollment = world.enroll(student["id"]).json()
    _patch(world, enrollment["id"], {"enrollment_number": "R-1"})
    actions = db_session.scalars(
        select(AuditLog.action)
        .where(AuditLog.action.like("enrollment.%"))
        .order_by(AuditLog.created_at)
    ).all()
    assert actions == ["enrollment.created", "enrollment.current_changed", "enrollment.updated"]


def test_cross_school_enrollment_is_invisible(world: World) -> None:
    b_student = world.student("SB", world.admin_b).json()
    b_enrollment = world.enroll(
        b_student["id"], world.admin_b, year="year_b", cls="class_b", sec="section_b"
    ).json()
    path = f"/api/v1/enrollments/{b_enrollment['id']}"
    assert world.client.get(path, headers=world.admin_a).status_code == 404
    assert (
        world.client.patch(path, headers=world.admin_a, json={"status": "withdrawn"}).status_code
        == 404
    )
    assert _list(world) == []
