"""School-scoped data access. Every query filters by ``school_id``."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models.academic import AcademicYear, SchoolClass, Section, Subject

type ScopedModel = AcademicYear | SchoolClass | Section | Subject


class ScopedRepository[M: ScopedModel]:
    """Generic access to a model that has ``id`` and ``school_id`` columns."""

    def __init__(
        self,
        session: Session,
        model: type[M],
        order_by: Sequence[InstrumentedAttribute[Any]],
    ) -> None:
        self.session = session
        self.model = model
        self.order_by = order_by

    def get(
        self,
        school_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        for_update: bool = False,
        filters: Sequence[ColumnElement[bool]] = (),
    ) -> M | None:
        stmt = select(self.model).where(
            self.model.id == item_id, self.model.school_id == school_id, *filters
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).one_or_none()

    def list(
        self,
        school_id: uuid.UUID,
        *,
        filters: Sequence[ColumnElement[bool]] = (),
        offset: int,
        limit: int,
    ) -> tuple[Sequence[M], int]:
        conditions = [self.model.school_id == school_id, *filters]
        items = self.session.scalars(
            select(self.model)
            .where(*conditions)
            .order_by(*self.order_by)
            .offset(offset)
            .limit(limit)
        ).all()
        total = (
            self.session.scalar(select(func.count()).select_from(self.model).where(*conditions))
            or 0
        )
        return items, total


class AcademicYearRepository(ScopedRepository[AcademicYear]):
    def __init__(self, session: Session) -> None:
        super().__init__(
            session,
            AcademicYear,
            (AcademicYear.start_date.desc(), AcademicYear.name),  # type: ignore[arg-type]
        )

    def current(self, school_id: uuid.UUID) -> AcademicYear | None:
        return self.session.scalars(
            select(AcademicYear).where(
                AcademicYear.school_id == school_id, AcademicYear.is_current.is_(True)
            )
        ).one_or_none()

    def clear_current(self, school_id: uuid.UUID) -> None:
        self.session.execute(
            update(AcademicYear)
            .where(AcademicYear.school_id == school_id, AcademicYear.is_current.is_(True))
            .values(is_current=False, updated_at=func.now())
        )


class ClassRepository(ScopedRepository[SchoolClass]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SchoolClass, (SchoolClass.display_order, SchoolClass.code))


class SectionRepository(ScopedRepository[Section]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Section, (Section.class_id, Section.code))

    def count_active_in_class(self, class_id: uuid.UUID) -> int:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(Section)
                .where(Section.class_id == class_id, Section.is_active.is_(True))
            )
            or 0
        )


class SubjectRepository(ScopedRepository[Subject]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Subject, (Subject.name, Subject.code))
