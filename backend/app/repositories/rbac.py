from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.rbac import Permission, Role, RolePermission


class RbacRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def roles_by_names(self, names: Collection[str]) -> list[Role]:
        if not names:
            return []
        return list(self.session.scalars(select(Role).where(Role.name.in_(names))))

    def count_roles(self) -> int:
        return self.session.scalar(select(func.count()).select_from(Role)) or 0

    def list_roles(self, *, offset: int, limit: int) -> Sequence[Role]:
        return self.session.scalars(
            select(Role).order_by(Role.name).offset(offset).limit(limit)
        ).all()

    def permissions_by_role(self, role_ids: Collection[object]) -> dict[object, list[str]]:
        result: dict[object, list[str]] = {role_id: [] for role_id in role_ids}
        if not role_ids:
            return result
        rows = self.session.execute(
            select(RolePermission.role_id, Permission.name)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id.in_(role_ids))
            .order_by(Permission.name)
        )
        for role_id, name in rows:
            result[role_id].append(name)
        return result

    def count_permissions(self) -> int:
        return self.session.scalar(select(func.count()).select_from(Permission)) or 0

    def list_permissions(self, *, offset: int, limit: int) -> Sequence[Permission]:
        return self.session.scalars(
            select(Permission).order_by(Permission.name).offset(offset).limit(limit)
        ).all()
