from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.school import School


class SchoolRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, school_id: uuid.UUID, *, for_update: bool = False) -> School | None:
        return self.session.get(School, school_id, with_for_update=for_update)

    def lock(self, school_id: uuid.UUID) -> School | None:
        """Row-lock the school: serialises school-wide invariants (e.g. current year)."""
        return self.session.scalars(
            select(School).where(School.id == school_id).with_for_update()
        ).one_or_none()

    def is_active(self, school_id: uuid.UUID) -> bool:
        return bool(self.session.scalar(select(School.is_active).where(School.id == school_id)))

    def list(self, *, offset: int, limit: int) -> tuple[Sequence[School], int]:
        items = self.session.scalars(
            select(School).order_by(School.name, School.code).offset(offset).limit(limit)
        ).all()
        total = self.session.scalar(select(func.count()).select_from(School)) or 0
        return items, total
