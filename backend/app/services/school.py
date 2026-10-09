"""School profile: read/update within scope, platform-level listing and creation."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.school import School
from app.repositories.school import SchoolRepository
from app.schemas.school import SchoolCreate, SchoolUpdate
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes

SCHOOL_CONSTRAINTS = {
    "uq_schools_code": ("code", "A school with this code already exists."),
}


class SchoolService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.schools = SchoolRepository(session)
        self.writer = ScopedWriter(session, context)

    def get(self, scope: SchoolScope) -> School:
        school = self.schools.get(scope.school_id)
        if school is None:  # pragma: no cover - scope resolution already checked existence
            raise NotFoundError("School not found.")
        return school

    def update(self, scope: SchoolScope, data: SchoolUpdate) -> School:
        changes = data.changes()
        if "is_active" in changes and not scope.principal.is_super_admin:
            raise PermissionDeniedError(
                "Only platform administrators can activate or deactivate a school."
            )
        school = self.schools.get(scope.school_id, for_update=True)
        if school is None:  # pragma: no cover
            raise NotFoundError("School not found.")
        diff = apply_changes(school, changes)
        if diff:
            self.writer.flush(SCHOOL_CONSTRAINTS)
            self.writer.audit(
                AuditAction.SCHOOL_UPDATED,
                actor_id=scope.actor_id,
                resource_type="school",
                resource_id=school.id,
                school_id=school.id,
                changes=diff,
            )
            self.session.commit()
        return school

    def list_all(self, *, offset: int, limit: int) -> tuple[Sequence[School], int]:
        """Platform-wide listing; callers must restrict this to SUPER_ADMIN."""
        return self.schools.list(offset=offset, limit=limit)

    def create(self, data: SchoolCreate, *, actor_id: uuid.UUID | None, source: str) -> School:
        school = School(**data.model_dump())
        self.session.add(school)
        self.writer.flush(SCHOOL_CONSTRAINTS)
        self.writer.audit(
            AuditAction.SCHOOL_CREATED,
            actor_id=actor_id,
            resource_type="school",
            resource_id=school.id,
            school_id=school.id,
            source=source,
        )
        return school
