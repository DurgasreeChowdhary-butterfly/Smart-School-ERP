"""Parents: a domain record bound to a PARENT user (the authentication identity).

The parent's mobile number and email live on ``users`` (single source of truth
for OTP sign-in); this service reads and writes them there.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.config import Settings
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.people import Parent, Student, StudentParent
from app.models.user import User, UserStatus
from app.repositories.people import ParentRepository, StudentParentRepository
from app.schemas.people import ParentCreate, ParentUpdate
from app.security.people_scope import link_visibility, parent_visibility
from app.security.permissions import PermissionName, RoleName
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.users import create_user_account

USER_CONSTRAINTS = {
    "uq_users_phone": ("mobile", "A user with this mobile number already exists."),
    "uq_users_email": ("email", "A user with this email address already exists."),
}
PERSONAL_FIELDS = frozenset(
    {"phone", "email", "address", "city", "state", "postal_code", "country", "occupation"}
)
_USER_FIELDS = {"mobile": "phone", "email": "email"}


class ParentService:
    def __init__(
        self, session: Session, settings: Settings, context: RequestContext | None = None
    ) -> None:
        self.session = session
        self.settings = settings
        self.parents = ParentRepository(session)
        self.links = StudentParentRepository(session)
        self.writer = ScopedWriter(session, context)

    @staticmethod
    def _visible(scope: SchoolScope) -> ColumnElement[bool]:
        return parent_visibility(scope.principal, scope.school_id)

    def get(
        self, scope: SchoolScope, parent_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[Parent, User]:
        row = self.parents.get_visible(self._visible(scope), parent_id, for_update=for_update)
        if row is None:
            raise NotFoundError("Parent not found.")
        return row

    def list(
        self, scope: SchoolScope, *, is_active: bool | None, offset: int, limit: int
    ) -> tuple[list[tuple[Parent, User]], int]:
        filters = [] if is_active is None else [Parent.is_active.is_(is_active)]
        return self.parents.list_visible(self._visible(scope), filters, offset=offset, limit=limit)

    def create(self, scope: SchoolScope, data: ParentCreate) -> tuple[Parent, User]:
        fields = data.model_dump(exclude={"mobile", "email"})
        created: list[tuple[Parent, User]] = []

        def insert() -> None:
            user = create_user_account(
                self.session,
                self.settings,
                full_name=f"{data.first_name} {data.last_name}",
                phone=data.mobile,
                email=data.email,
                password=None,  # parents sign in with mobile OTP
                roles=[RoleName.PARENT],
                school_id=scope.school_id,
                created_by_id=scope.actor_id,
                source="api",
            )
            parent = Parent(school_id=scope.school_id, user_id=user.id, **fields)
            self.session.add(parent)
            created.append((parent, user))

        self.writer.flush(USER_CONSTRAINTS, before=insert)
        parent, user = created[0]
        self.writer.audit(
            AuditAction.PARENT_CREATED,
            actor_id=scope.actor_id,
            resource_type="parent",
            resource_id=parent.id,
            school_id=scope.school_id,
            user_id=str(user.id),
        )
        self.session.commit()
        return parent, user

    def update(
        self, scope: SchoolScope, parent_id: uuid.UUID, data: ParentUpdate
    ) -> tuple[Parent, User]:
        changes = data.changes()
        if "is_active" in changes and not scope.principal.has_permission(
            PermissionName.USER_DEACTIVATE
        ):
            raise PermissionDeniedError("Changing a parent's status requires user.deactivate.")
        parent, user = self.get(scope, parent_id, for_update=True)
        was_active = parent.is_active
        user_changes = {
            _USER_FIELDS[key]: changes.pop(key) for key in list(changes) if key in _USER_FIELDS
        }
        diff = apply_changes(parent, changes, redact=PERSONAL_FIELDS)
        diff += apply_changes(user, user_changes, redact=PERSONAL_FIELDS)
        if not diff:
            return parent, user
        user.full_name = parent.full_name
        user.status = UserStatus.ACTIVE.value if parent.is_active else UserStatus.DEACTIVATED.value
        self.writer.flush(USER_CONSTRAINTS)
        action = AuditAction.PARENT_UPDATED
        if was_active != parent.is_active:
            action = (
                AuditAction.PARENT_REACTIVATED
                if parent.is_active
                else AuditAction.PARENT_DEACTIVATED
            )
        self.writer.audit(
            action,
            actor_id=scope.actor_id,
            resource_type="parent",
            resource_id=parent.id,
            school_id=scope.school_id,
            changes=diff,
        )
        self.session.commit()
        return parent, user

    def children(
        self, scope: SchoolScope, parent_id: uuid.UUID
    ) -> Sequence[tuple[StudentParent, Student]]:
        self.get(scope, parent_id)
        return self.links.links_of_parent(parent_id, link_visibility(scope.principal))
