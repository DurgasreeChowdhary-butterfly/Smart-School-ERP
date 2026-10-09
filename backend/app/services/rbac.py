"""Role catalog queries and user role assignment."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.audit.service import AuditAction, record_audit
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.rbac import Permission, Role
from app.repositories.rbac import RbacRepository
from app.repositories.users import UserRepository
from app.security.authorization import ensure_school_access
from app.security.permissions import ASSIGNABLE_ROLES, RoleName
from app.security.principal import Principal


@dataclass(frozen=True, slots=True)
class RoleWithPermissions:
    role: Role
    permissions: list[str]


class RbacService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.context = context or RequestContext()
        self.rbac = RbacRepository(session)
        self.users = UserRepository(session)

    def list_roles(self, *, offset: int, limit: int) -> tuple[list[RoleWithPermissions], int]:
        roles = self.rbac.list_roles(offset=offset, limit=limit)
        permissions = self.rbac.permissions_by_role([role.id for role in roles])
        items = [RoleWithPermissions(role, permissions[role.id]) for role in roles]
        return items, self.rbac.count_roles()

    def list_permissions(self, *, offset: int, limit: int) -> tuple[Sequence[Permission], int]:
        return self.rbac.list_permissions(offset=offset, limit=limit), self.rbac.count_permissions()

    def get_user_roles(self, actor: Principal, user_id: uuid.UUID) -> list[str]:
        user = self.users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        ensure_school_access(actor, user.school_id)
        return sorted(self.users.role_names(user_id))

    def set_user_roles(
        self, actor: Principal, user_id: uuid.UUID, role_names: Sequence[RoleName]
    ) -> list[str]:
        """Replace a user's roles.

        Rules: the target must be in the actor's school (404 otherwise); actors cannot
        change their own roles; every role added *or removed* must be assignable by
        one of the actor's roles (so only SUPER_ADMIN can touch SUPER_ADMIN).
        """
        user = self.users.get(user_id)
        if user is None:
            raise NotFoundError("User not found.")
        ensure_school_access(actor, user.school_id)
        if user_id == actor.user_id:
            raise PermissionDeniedError("You cannot change your own roles.")

        current = self.users.role_names(user_id)
        requested = frozenset(role.value for role in role_names)
        assignable: set[str] = set()
        for actor_role in actor.roles:
            if actor_role in RoleName.__members__:
                assignable |= {r.value for r in ASSIGNABLE_ROLES.get(RoleName(actor_role), set())}
        changed = current ^ requested
        if not changed <= assignable:
            raise PermissionDeniedError("You are not allowed to assign or remove these roles.")
        if not changed:
            return sorted(current)

        roles = self.rbac.roles_by_names(requested)
        self.users.replace_roles(user_id, roles, assigned_by_id=actor.user_id)
        record_audit(
            self.session,
            AuditAction.ROLES_CHANGED,
            user_id=actor.user_id,
            resource_type="user",
            resource_id=user_id,
            context=self.context,
            details={
                "previous_roles": sorted(current),
                "new_roles": sorted(requested),
                "added": sorted(requested - current),
                "removed": sorted(current - requested),
            },
        )
        self.session.commit()
        return sorted(requested)
