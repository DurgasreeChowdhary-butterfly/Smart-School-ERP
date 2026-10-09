"""Resolve the school a request operates on (the tenant boundary).

Rules (one implementation used by every school-scoped endpoint):

* **School users** always act on their own ``users.school_id``. They cannot
  choose another school. If they send ``X-School-ID`` for a different school, the
  answer is 404, the same as for a non-existent school, so other schools'
  existence is not revealed.
* **SUPER_ADMIN** has no school of their own and must name one with the
  ``X-School-ID`` header. A header (not a query parameter) keeps the selection
  out of URLs and logs and applies uniformly to every endpoint.
* An **inactive school** is unavailable to its own users (403 ``school_inactive``).
  SUPER_ADMIN can still read it and reactivate it.

Records inside the school are then looked up with ``school_id`` in the WHERE
clause, so another school's record IDs simply return 404.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, NotFoundError, PermissionDeniedError
from app.models.school import School
from app.security.principal import Principal

SCHOOL_HEADER = "X-School-ID"


class SchoolInactiveError(PermissionDeniedError):
    code = "school_inactive"
    message = "This school is inactive."


class SchoolSelectionRequiredError(BadRequestError):
    code = "school_selection_required"
    message = f"Platform administrators must select a school with the {SCHOOL_HEADER} header."


@dataclass(frozen=True, slots=True)
class SchoolScope:
    school_id: uuid.UUID
    principal: Principal

    @property
    def actor_id(self) -> uuid.UUID:
        return self.principal.user_id


def resolve_school_scope(
    session: Session, principal: Principal, requested_school_id: uuid.UUID | None
) -> SchoolScope:
    if principal.is_super_admin:
        if requested_school_id is None:
            raise SchoolSelectionRequiredError()
        if session.get(School, requested_school_id) is None:
            raise NotFoundError("School not found.")
        return SchoolScope(school_id=requested_school_id, principal=principal)

    if principal.school_id is None:
        raise PermissionDeniedError("Your account is not associated with a school.")
    if requested_school_id is not None and requested_school_id != principal.school_id:
        raise NotFoundError("School not found.")
    school = session.get(School, principal.school_id)
    if school is None:  # pragma: no cover - prevented by the users.school_id foreign key
        raise NotFoundError("School not found.")
    if not school.is_active:
        raise SchoolInactiveError()
    return SchoolScope(school_id=school.id, principal=principal)
