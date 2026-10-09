"""Shared mechanics for services that write school-owned records."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import AuditAction, record_audit
from app.core.db_errors import translate_integrity_error
from app.core.request_context import RequestContext

ConstraintMessages = Mapping[str, tuple[str, str]]


def _audit_value(value: object) -> object:
    if isinstance(value, date | uuid.UUID):
        return str(value)
    return value


def apply_changes(
    entity: object, changes: Mapping[str, object], *, redact: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Set attributes; return ``[{"field", "from", "to"}]`` for values that changed.

    The audit shape deliberately avoids using field names as JSON keys: the audit
    sanitiser redacts keys such as ``code``, which is a legitimate field here.
    Fields in ``redact`` (personal data) are recorded by name only.
    """
    diff: list[dict[str, Any]] = []
    for field, new in changes.items():
        old = getattr(entity, field)
        if old != new:
            setattr(entity, field, new)
            if field in redact:
                diff.append({"field": field, "redacted": True})
            else:
                diff.append({"field": field, "from": _audit_value(old), "to": _audit_value(new)})
    return diff


class ScopedWriter:
    """Flush inside a SAVEPOINT and translate constraint violations.

    The database is the arbiter for uniqueness/overlap/ownership, so concurrent
    requests cannot both succeed; the loser gets a clean 409/422 and the outer
    transaction stays usable.
    """

    def __init__(self, session: Session, context: RequestContext | None) -> None:
        self.session = session
        self.context = context or RequestContext()

    def flush(self, messages: ConstraintMessages, before: Callable[[], None] | None = None) -> None:
        try:
            with self.session.begin_nested():
                if before is not None:
                    before()
                self.session.flush()
        except IntegrityError as exc:
            raise translate_integrity_error(exc, messages) from exc

    def audit(
        self,
        action: AuditAction,
        *,
        actor_id: uuid.UUID | None,
        resource_type: str,
        resource_id: uuid.UUID,
        school_id: uuid.UUID,
        **details: object,
    ) -> None:
        record_audit(
            self.session,
            action,
            user_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            context=self.context,
            details={"school_id": str(school_id), **details},
        )
