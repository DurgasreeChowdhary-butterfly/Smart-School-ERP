"""Database-backed RBAC: seeded catalog, permission enforcement, role assignment."""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_permission, require_role
from app.models.audit import AuditLog
from app.models.rbac import Permission, Role, RolePermission
from app.security.permissions import ROLE_PERMISSIONS, PermissionName, RoleName
from app.security.principal import Principal
from tests.conftest import SCHOOL_B, UserFactory, bearer, login

pytestmark = pytest.mark.db


def test_database_catalog_matches_code(db_session: Session) -> None:
    """Fails if app/security/permissions.py changes without a migration."""
    assert set(db_session.scalars(select(Role.name))) == {r.value for r in RoleName}
    assert set(db_session.scalars(select(Permission.name))) == {p.value for p in PermissionName}
    rows = db_session.execute(
        select(Role.name, Permission.name)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
    ).all()
    actual: dict[str, set[str]] = {}
    for role, permission in rows:
        actual.setdefault(role, set()).add(permission)
    expected = {r.value: {p.value for p in perms} for r, perms in ROLE_PERMISSIONS.items()}
    assert actual == expected


def _token(client: TestClient, make_user: UserFactory, role: RoleName, **kwargs: Any) -> str:
    user = make_user(roles=(role,), **kwargs)
    assert user.email is not None
    return str(login(client, user.email)["access_token"])


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    """Test-only routes guarded exactly like real endpoints."""

    @app.post("/_test/marks/publish")
    def publish(
        _: Annotated[Principal, Depends(require_permission(PermissionName.MARKS_PUBLISH))],
    ) -> dict[str, str]:
        return {"published": "yes"}

    @app.post("/_test/attendance")
    def mark_attendance(
        _: Annotated[Principal, Depends(require_permission(PermissionName.ATTENDANCE_MANAGE))],
    ) -> dict[str, str]:
        return {"marked": "yes"}

    @app.get("/_test/super-only")
    def super_only(
        _: Annotated[Principal, Depends(require_role(RoleName.SUPER_ADMIN))],
    ) -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.mark.parametrize(
    ("role", "path", "expected"),
    [
        (RoleName.MANAGEMENT, "/_test/marks/publish", 200),
        (RoleName.SCHOOL_ADMIN, "/_test/marks/publish", 200),
        (RoleName.FACULTY, "/_test/marks/publish", 403),
        (RoleName.PARENT, "/_test/marks/publish", 403),
        (RoleName.STUDENT, "/_test/marks/publish", 403),
        (RoleName.FACULTY, "/_test/attendance", 200),
        (RoleName.PARENT, "/_test/attendance", 403),
        (RoleName.STUDENT, "/_test/attendance", 403),
        (RoleName.MANAGEMENT, "/_test/attendance", 403),
    ],
)
def test_permission_enforcement(
    probe_app: FastAPI,
    db_client: TestClient,
    make_user: UserFactory,
    role: RoleName,
    path: str,
    expected: int,
) -> None:
    token = _token(db_client, make_user, role)
    response = db_client.post(path, headers=bearer(token))
    assert response.status_code == expected
    if expected == 403:
        assert response.json()["error"]["code"] == "permission_denied"


def test_role_guard(probe_app: FastAPI, db_client: TestClient, make_user: UserFactory) -> None:
    admin = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    root = _token(db_client, make_user, RoleName.SUPER_ADMIN)
    assert db_client.get("/_test/super-only", headers=bearer(admin)).status_code == 403
    assert db_client.get("/_test/super-only", headers=bearer(root)).status_code == 200


def test_unauthenticated_requests_never_reach_permission_check(
    probe_app: FastAPI, db_client: TestClient
) -> None:
    assert db_client.post("/_test/marks/publish").status_code == 401


def test_permission_revocation_is_immediate(
    probe_app: FastAPI, db_client: TestClient, make_user: UserFactory, db_session: Session
) -> None:
    """Permissions are read from the database per request, never from the token."""
    token = _token(db_client, make_user, RoleName.FACULTY)
    assert db_client.post("/_test/attendance", headers=bearer(token)).status_code == 200
    faculty = db_session.scalars(select(Role).where(Role.name == "FACULTY")).one()
    attendance = db_session.scalars(
        select(Permission).where(Permission.name == "attendance.manage")
    ).one()
    db_session.query(RolePermission).filter_by(
        role_id=faculty.id, permission_id=attendance.id
    ).delete()
    assert db_client.post("/_test/attendance", headers=bearer(token)).status_code == 403


# ------------------------------------------------------------------ catalog endpoints
def test_list_roles(db_client: TestClient, make_user: UserFactory) -> None:
    token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    response = db_client.get("/api/v1/roles", headers=bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 6
    parent = next(r for r in body["items"] if r["name"] == "PARENT")
    assert "parent.read_child" in parent["permissions"]
    assert "marks.manage" not in parent["permissions"]


def test_list_permissions_paginated(db_client: TestClient, make_user: UserFactory) -> None:
    token = _token(db_client, make_user, RoleName.SUPER_ADMIN)
    response = db_client.get("/api/v1/permissions?page=2&page_size=10", headers=bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(PermissionName)
    assert body["page"] == 2
    assert len(body["items"]) == 10


@pytest.mark.parametrize("role", [RoleName.FACULTY, RoleName.PARENT, RoleName.STUDENT])
def test_personas_cannot_read_rbac_catalog(
    db_client: TestClient, make_user: UserFactory, role: RoleName
) -> None:
    token = _token(db_client, make_user, role)
    assert db_client.get("/api/v1/roles", headers=bearer(token)).status_code == 403
    assert db_client.get("/api/v1/permissions", headers=bearer(token)).status_code == 403


# ------------------------------------------------------------------- role assignment
def _set_roles(client: TestClient, token: str, user_id: object, roles: list[str]) -> Any:
    return client.put(
        f"/api/v1/users/{user_id}/roles", json={"roles": roles}, headers=bearer(token)
    )


def test_school_admin_assigns_roles_and_change_is_audited(
    db_client: TestClient, make_user: UserFactory, db_session: Session
) -> None:
    admin_token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    target = make_user(roles=(RoleName.FACULTY,))

    response = _set_roles(db_client, admin_token, target.id, ["FACULTY", "MANAGEMENT"])
    assert response.status_code == 200
    assert response.json() == {"user_id": str(target.id), "roles": ["FACULTY", "MANAGEMENT"]}

    read = db_client.get(f"/api/v1/users/{target.id}/roles", headers=bearer(admin_token))
    assert read.json()["roles"] == ["FACULTY", "MANAGEMENT"]

    event = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "rbac.user_roles.changed")
    ).one()
    assert event.resource_id == str(target.id)
    assert event.details["added"] == ["MANAGEMENT"]
    assert event.details["removed"] == []


def test_new_role_applies_to_existing_session(
    probe_app: FastAPI, db_client: TestClient, make_user: UserFactory
) -> None:
    admin_token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    teacher = make_user(roles=(RoleName.FACULTY,))
    assert teacher.email is not None
    teacher_token = login(db_client, teacher.email)["access_token"]
    assert db_client.post("/_test/marks/publish", headers=bearer(teacher_token)).status_code == 403
    _set_roles(db_client, admin_token, teacher.id, ["FACULTY", "MANAGEMENT"])
    assert db_client.post("/_test/marks/publish", headers=bearer(teacher_token)).status_code == 200


def test_school_admin_cannot_grant_super_admin(
    db_client: TestClient, make_user: UserFactory
) -> None:
    admin_token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    target = make_user(roles=(RoleName.FACULTY,))
    response = _set_roles(db_client, admin_token, target.id, ["SUPER_ADMIN"])
    assert response.status_code == 403


def test_school_admin_cannot_demote_super_admin(
    db_client: TestClient, make_user: UserFactory, db_session: Session
) -> None:
    admin_token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    root = make_user(roles=(RoleName.SUPER_ADMIN,))
    root.school_id = make_user(roles=(RoleName.FACULTY,)).school_id  # same school for the test
    db_session.flush()
    assert _set_roles(db_client, admin_token, root.id, ["FACULTY"]).status_code == 403


def test_cannot_change_own_roles(db_client: TestClient, make_user: UserFactory) -> None:
    root = make_user(roles=(RoleName.SUPER_ADMIN,))
    assert root.email is not None
    token = login(db_client, root.email)["access_token"]
    assert _set_roles(db_client, token, root.id, []).status_code == 403


def test_cross_school_assignment_is_not_found(
    db_client: TestClient, make_user: UserFactory
) -> None:
    admin_token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    other_school_user = make_user(roles=(RoleName.FACULTY,), school_id=SCHOOL_B)
    assert _set_roles(db_client, admin_token, other_school_user.id, ["PARENT"]).status_code == 404
    assert (
        db_client.get(
            f"/api/v1/users/{other_school_user.id}/roles", headers=bearer(admin_token)
        ).status_code
        == 404
    )


def test_super_admin_can_manage_any_school(db_client: TestClient, make_user: UserFactory) -> None:
    root_token = _token(db_client, make_user, RoleName.SUPER_ADMIN)
    target = make_user(roles=(RoleName.FACULTY,), school_id=SCHOOL_B)
    assert _set_roles(db_client, root_token, target.id, ["SCHOOL_ADMIN"]).status_code == 200


@pytest.mark.parametrize("role", [RoleName.FACULTY, RoleName.MANAGEMENT, RoleName.PARENT])
def test_non_admins_cannot_assign_roles(
    db_client: TestClient, make_user: UserFactory, role: RoleName
) -> None:
    token = _token(db_client, make_user, role)
    target = make_user(roles=(RoleName.STUDENT,))
    assert _set_roles(db_client, token, target.id, ["SCHOOL_ADMIN"]).status_code == 403


@pytest.mark.parametrize("roles", [["NOT_A_ROLE"], ["PARENT", "PARENT"]])
def test_role_assignment_validation(
    db_client: TestClient, make_user: UserFactory, roles: list[str]
) -> None:
    token = _token(db_client, make_user, RoleName.SCHOOL_ADMIN)
    target = make_user(roles=(RoleName.STUDENT,))
    assert _set_roles(db_client, token, target.id, roles).status_code == 422


def test_unknown_user_is_404(db_client: TestClient, make_user: UserFactory) -> None:
    token = _token(db_client, make_user, RoleName.SUPER_ADMIN)
    response = _set_roles(db_client, token, "00000000-0000-4000-8000-000000000999", ["PARENT"])
    assert response.status_code == 404
