from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.academic import AcademicYear
from app.models.audit import AuditLog
from app.security.permissions import RoleName
from tests.conftest import SCHOOL_A, SCHOOL_B, HeadersFactory

pytestmark = pytest.mark.db

URL = "/api/v1/academic-years"
R = RoleName


@pytest.fixture
def admin(auth_headers: HeadersFactory) -> dict[str, str]:
    return auth_headers(R.SCHOOL_ADMIN)


@pytest.fixture
def school_b_root(auth_headers: HeadersFactory) -> dict[str, str]:
    return auth_headers(R.SUPER_ADMIN, SCHOOL_B)


def _create(
    client: TestClient,
    headers: dict[str, str],
    name: str = "2026-2027",
    start: str = "2026-06-01",
    end: str = "2027-05-31",
) -> Any:
    return client.post(
        URL, headers=headers, json={"name": name, "start_date": start, "end_date": end}
    )


def test_create_read_list(db_client: TestClient, admin: dict[str, str]) -> None:
    created = _create(db_client, admin)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["school_id"] == str(SCHOOL_A)
    assert body["is_current"] is False
    assert body["is_active"] is True

    assert db_client.get(f"{URL}/{body['id']}", headers=admin).json() == body
    listing = db_client.get(URL, headers=admin).json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == body["id"]


def test_list_is_newest_first_and_filterable(db_client: TestClient, admin: dict[str, str]) -> None:
    old = _create(db_client, admin, "2025-2026", "2025-06-01", "2026-05-31").json()
    new = _create(db_client, admin).json()
    db_client.post(f"{URL}/{new['id']}/set-current", headers=admin)
    db_client.patch(f"{URL}/{old['id']}", headers=admin, json={"is_active": False})

    names = [y["name"] for y in db_client.get(URL, headers=admin).json()["items"]]
    assert names == ["2026-2027", "2025-2026"]
    current = db_client.get(f"{URL}?is_current=true", headers=admin).json()
    assert [y["id"] for y in current["items"]] == [new["id"]]
    inactive = db_client.get(f"{URL}?is_active=false", headers=admin).json()
    assert [y["id"] for y in inactive["items"]] == [old["id"]]
    paged = db_client.get(f"{URL}?page=2&page_size=1", headers=admin).json()
    assert paged["total"] == 2
    assert paged["pages"] == 2
    assert [y["name"] for y in paged["items"]] == ["2025-2026"]


@pytest.mark.parametrize(
    ("start", "end"),
    [("2027-05-31", "2026-06-01"), ("2026-06-01", "2026-06-01")],
)
def test_invalid_date_range_rejected(
    db_client: TestClient, admin: dict[str, str], start: str, end: str
) -> None:
    response = _create(db_client, admin, start=start, end=end)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"name": "   ", "start_date": "2026-06-01", "end_date": "2027-05-31"},
        {"name": "x" * 51, "start_date": "2026-06-01", "end_date": "2027-05-31"},
        {"name": "Y", "start_date": "not-a-date", "end_date": "2027-05-31"},
        {"name": "Y", "start_date": "2026-06-01"},
        {"name": "Y", "start_date": "2026-06-01", "end_date": "2027-05-31", "is_current": True},
    ],
)
def test_create_validation(
    db_client: TestClient, admin: dict[str, str], body: dict[str, Any]
) -> None:
    assert db_client.post(URL, headers=admin, json=body).status_code == 422


def test_duplicate_name_rejected(db_client: TestClient, admin: dict[str, str]) -> None:
    _create(db_client, admin)
    response = _create(db_client, admin, start="2030-06-01", end="2031-05-31")
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["loc"] == ["body", "name"]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-09-01", "2027-08-31"),  # partial overlap
        ("2026-07-01", "2027-03-31"),  # contained
        ("2025-01-01", "2028-12-31"),  # containing
        ("2027-05-31", "2028-05-30"),  # shares a single day
    ],
)
def test_overlapping_years_rejected(
    db_client: TestClient, admin: dict[str, str], start: str, end: str
) -> None:
    _create(db_client, admin)
    response = _create(db_client, admin, "Other", start, end)
    assert response.status_code == 409
    assert "overlap" in response.json()["error"]["message"]


def test_adjacent_years_allowed(db_client: TestClient, admin: dict[str, str]) -> None:
    _create(db_client, admin)
    assert _create(db_client, admin, "2027-2028", "2027-06-01", "2028-05-31").status_code == 201


def test_same_name_and_dates_allowed_in_another_school(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    assert _create(db_client, admin).status_code == 201
    assert _create(db_client, school_b_root).status_code == 201


def test_update(db_client: TestClient, admin: dict[str, str], db_session: Session) -> None:
    year = _create(db_client, admin).json()
    response = db_client.patch(
        f"{URL}/{year['id']}", headers=admin, json={"name": "AY 2026-27", "end_date": "2027-04-30"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "AY 2026-27"
    assert response.json()["end_date"] == "2027-04-30"
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "academic_year.updated")
    ).one()
    assert {c["field"] for c in event.details["changes"]} == {"name", "end_date"}


def test_update_dates_checked_against_stored_values(
    db_client: TestClient, admin: dict[str, str]
) -> None:
    year = _create(db_client, admin).json()
    response = db_client.patch(
        f"{URL}/{year['id']}", headers=admin, json={"end_date": "2026-01-01"}
    )
    assert response.status_code == 422


def test_update_into_overlap_rejected(db_client: TestClient, admin: dict[str, str]) -> None:
    _create(db_client, admin)
    later = _create(db_client, admin, "2027-2028", "2027-06-01", "2028-05-31").json()
    response = db_client.patch(
        f"{URL}/{later['id']}", headers=admin, json={"start_date": "2027-01-01"}
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "body", [{}, {"name": None}, {"is_current": True}, {"school_id": str(SCHOOL_B)}]
)
def test_update_validation(
    db_client: TestClient, admin: dict[str, str], body: dict[str, Any]
) -> None:
    year = _create(db_client, admin).json()
    assert db_client.patch(f"{URL}/{year['id']}", headers=admin, json=body).status_code == 422


# ------------------------------------------------------------------- current year
def test_set_current_switches_atomically(
    db_client: TestClient, admin: dict[str, str], db_session: Session
) -> None:
    first = _create(db_client, admin, "2025-2026", "2025-06-01", "2026-05-31").json()
    second = _create(db_client, admin).json()

    response = db_client.post(f"{URL}/{first['id']}/set-current", headers=admin)
    assert response.status_code == 200
    assert response.json()["is_current"] is True

    response = db_client.post(f"{URL}/{second['id']}/set-current", headers=admin)
    assert response.json()["is_current"] is True
    previous = db_client.get(f"{URL}/{first['id']}", headers=admin).json()
    assert previous["is_current"] is False

    current = db_session.scalars(
        select(AcademicYear).where(
            AcademicYear.school_id == SCHOOL_A, AcademicYear.is_current.is_(True)
        )
    ).all()
    assert [y.id for y in current] == [uuid.UUID(second["id"])]

    events = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "academic_year.set_current")
        .order_by(AuditLog.created_at)
    ).all()
    assert [e.details["previous_current_id"] for e in events] == [None, first["id"]]


def test_set_current_is_idempotent(db_client: TestClient, admin: dict[str, str]) -> None:
    year = _create(db_client, admin).json()
    db_client.post(f"{URL}/{year['id']}/set-current", headers=admin)
    again = db_client.post(f"{URL}/{year['id']}/set-current", headers=admin)
    assert again.status_code == 200
    assert again.json()["is_current"] is True


def test_current_years_are_per_school(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str]
) -> None:
    a = _create(db_client, admin).json()
    b = _create(db_client, school_b_root).json()
    db_client.post(f"{URL}/{a['id']}/set-current", headers=admin)
    db_client.post(f"{URL}/{b['id']}/set-current", headers=school_b_root)
    assert db_client.get(f"{URL}/{a['id']}", headers=admin).json()["is_current"] is True


def test_inactive_year_cannot_be_current(db_client: TestClient, admin: dict[str, str]) -> None:
    year = _create(db_client, admin).json()
    db_client.patch(f"{URL}/{year['id']}", headers=admin, json={"is_active": False})
    assert db_client.post(f"{URL}/{year['id']}/set-current", headers=admin).status_code == 422


def test_current_year_cannot_be_deactivated(db_client: TestClient, admin: dict[str, str]) -> None:
    year = _create(db_client, admin).json()
    db_client.post(f"{URL}/{year['id']}/set-current", headers=admin)
    response = db_client.patch(f"{URL}/{year['id']}", headers=admin, json={"is_active": False})
    assert response.status_code == 422


# ---------------------------------------------------------------- isolation & RBAC
def test_cross_school_access_is_not_found(
    db_client: TestClient, admin: dict[str, str], school_b_root: dict[str, str], db_session: Session
) -> None:
    b_year = _create(db_client, school_b_root).json()
    path = f"{URL}/{b_year['id']}"
    assert db_client.get(path, headers=admin).status_code == 404
    assert db_client.patch(path, headers=admin, json={"name": "hijack"}).status_code == 404
    assert db_client.post(f"{path}/set-current", headers=admin).status_code == 404
    assert db_client.get(URL, headers=admin).json()["total"] == 0
    stored = db_session.get(AcademicYear, uuid.UUID(b_year["id"]))
    assert stored is not None
    assert (stored.name, stored.is_current) == ("2026-2027", False)


def test_unknown_id_is_not_found(db_client: TestClient, admin: dict[str, str]) -> None:
    missing = "00000000-0000-4000-8000-000000000123"
    assert db_client.get(f"{URL}/{missing}", headers=admin).status_code == 404
    assert db_client.get(f"{URL}/not-a-uuid", headers=admin).status_code == 422


@pytest.mark.parametrize("role", [R.MANAGEMENT, R.FACULTY, R.PARENT, R.STUDENT])
def test_read_only_roles_cannot_write(
    db_client: TestClient, auth_headers: HeadersFactory, admin: dict[str, str], role: RoleName
) -> None:
    headers = auth_headers(role)
    year = _create(db_client, admin).json()
    assert db_client.get(URL, headers=headers).status_code == 200  # academic.read
    assert _create(db_client, headers, "X", "2030-01-01", "2030-12-31").status_code == 403
    assert (
        db_client.patch(f"{URL}/{year['id']}", headers=headers, json={"name": "X"}).status_code
        == 403
    )
    assert db_client.post(f"{URL}/{year['id']}/set-current", headers=headers).status_code == 403


def test_create_is_audited(
    db_client: TestClient, admin: dict[str, str], db_session: Session
) -> None:
    year = _create(db_client, admin).json()
    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "academic_year.created")
    ).one()
    assert event.resource_id == year["id"]
    assert event.details["school_id"] == str(SCHOOL_A)
    assert event.user_id is not None
