"""The authenticated caller, as seen by authorization code."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.security.permissions import PermissionName, RoleName


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    session_id: uuid.UUID
    school_id: uuid.UUID | None
    roles: frozenset[str]
    permissions: frozenset[str]

    def has_permission(self, permission: PermissionName) -> bool:
        return permission in self.permissions

    def has_any_role(self, *roles: RoleName) -> bool:
        return any(role in self.roles for role in roles)

    @property
    def is_super_admin(self) -> bool:
        return RoleName.SUPER_ADMIN in self.roles
