from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.people import Student
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.school import School
from app.models.user import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalars(select(User).where(User.email == email)).one_or_none()

    def get_by_phone(self, phone: str) -> User | None:
        return self.session.scalars(select(User).where(User.phone == phone)).one_or_none()

    def get_student_login(self, school_code: str, admission_number: str) -> User | None:
        """Resolve a student's login identity (school code + admission number)."""
        return self.session.scalars(
            select(User)
            .join(Student, Student.user_id == User.id)
            .join(School, School.id == Student.school_id)
            .where(School.code == school_code, Student.admission_number == admission_number)
        ).one_or_none()

    def school_is_active(self, user: User) -> bool:
        """True for platform accounts (no school) or when the user's school is active."""
        if user.school_id is None:
            return True
        return bool(
            self.session.scalar(select(School.is_active).where(School.id == user.school_id))
        )

    def add(self, user: User) -> User:
        self.session.add(user)
        self.session.flush()
        return user

    def role_names(self, user_id: uuid.UUID) -> frozenset[str]:
        rows = self.session.scalars(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        )
        return frozenset(rows)

    def permission_names(self, user_id: uuid.UUID) -> frozenset[str]:
        rows = self.session.scalars(
            select(Permission.name)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user_id)
            .distinct()
        )
        return frozenset(rows)

    def replace_roles(
        self, user_id: uuid.UUID, roles: Iterable[Role], *, assigned_by_id: uuid.UUID | None
    ) -> None:
        self.session.execute(delete(UserRole).where(UserRole.user_id == user_id))
        for role in roles:
            self.session.add(
                UserRole(user_id=user_id, role_id=role.id, assigned_by_id=assigned_by_id)
            )
        self.session.flush()
