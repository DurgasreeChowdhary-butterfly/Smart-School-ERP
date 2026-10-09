"""Translate PostgreSQL integrity violations into API errors.

Uniqueness, overlap and referential rules are enforced by the database so they
hold under concurrent requests. Services let the database decide and map the
violated constraint to a stable, user-safe error here. Raw driver messages are
never returned to clients (they can contain other rows' values).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError, BusinessRuleError, ConflictError
from app.schemas.common import ErrorDetail

logger = logging.getLogger(__name__)

_UNIQUE_VIOLATION = "23505"
_EXCLUSION_VIOLATION = "23P01"
_FOREIGN_KEY_VIOLATION = "23503"
_CHECK_VIOLATION = "23514"


def constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def sqlstate(exc: IntegrityError) -> str | None:
    state = getattr(exc.orig, "sqlstate", None)
    return state if isinstance(state, str) else None


def translate_integrity_error(
    exc: IntegrityError, messages: Mapping[str, tuple[str, str]]
) -> AppError:
    """Map a constraint name to ``(field, message)``; unknown violations become generic.

    Unique/exclusion violations -> 409, foreign-key/check violations -> 422.
    """
    name = constraint_name(exc)
    state = sqlstate(exc)
    field, message = messages.get(name or "", ("", ""))
    details = [ErrorDetail(loc=["body", field], message=message)] if field else []

    if state in (_UNIQUE_VIOLATION, _EXCLUSION_VIOLATION):
        return ConflictError(message or None, details=details)
    if state in (_FOREIGN_KEY_VIOLATION, _CHECK_VIOLATION):
        return BusinessRuleError(message or None, details=details)
    logger.error("unexpected integrity error", extra={"constraint": name, "sqlstate": state})
    return ConflictError()
