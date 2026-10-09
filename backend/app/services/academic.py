"""Academic structure business rules (years, classes, sections, subjects).

Every operation takes a :class:`SchoolScope` and only ever touches rows of that
school; ids belonging to another school behave exactly like unknown ids (404).
Reads are further narrowed by :func:`structure_visibility`: faculty see only the
years, classes, sections and subjects named by their effective assignments.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear, SchoolClass, Section, Subject
from app.repositories.academic import (
    AcademicYearRepository,
    ClassRepository,
    SectionRepository,
    SubjectRepository,
)
from app.repositories.school import SchoolRepository
from app.schemas.academic import (
    AcademicYearCreate,
    AcademicYearUpdate,
    ClassCreate,
    ClassUpdate,
    SectionCreate,
    SectionUpdate,
    SubjectCreate,
    SubjectUpdate,
)
from app.schemas.common import ErrorDetail
from app.security.faculty_scope import structure_visibility
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes

YEAR_CONSTRAINTS = {
    "uq_academic_years_school_id_name": (
        "name",
        "An academic year with this name already exists.",
    ),
    "ex_academic_years_no_overlap": (
        "start_date",
        "The dates overlap an existing academic year.",
    ),
    "uq_academic_years_one_current_per_school": (
        "",
        "Another academic year was made current at the same time. Please retry.",
    ),
    "ck_academic_years_dates_ordered": ("end_date", "start_date must be before end_date."),
}
CLASS_CONSTRAINTS = {
    "uq_classes_school_id_code": ("code", "A class with this code already exists."),
}
SECTION_CONSTRAINTS = {
    "uq_sections_class_id_code": ("code", "A section with this code already exists in the class."),
    "fk_sections_class_same_school": ("class_id", "Class not found."),
}
SUBJECT_CONSTRAINTS = {
    "uq_subjects_school_id_code": ("code", "A subject with this code already exists."),
}


def _reference_error(field: str, message: str) -> BusinessRuleError:
    return BusinessRuleError(message, details=[ErrorDetail(loc=["body", field], message=message)])


def _active_filters(
    is_active_column: InstrumentedAttribute[bool], is_active: bool | None
) -> list[ColumnElement[bool]]:
    return [] if is_active is None else [is_active_column == is_active]


def _visible(
    scope: SchoolScope, model: type[AcademicYear | SchoolClass | Section | Subject]
) -> ColumnElement[bool]:
    return structure_visibility(scope.principal, scope.school_id, model)


def _activation_action(
    diff: list[dict[str, object]],
    updated: AuditAction,
    deactivated: AuditAction,
    reactivated: AuditAction,
) -> AuditAction:
    for change in diff:
        if change["field"] == "is_active":
            return reactivated if change["to"] else deactivated
    return updated


# =================================================================== academic years
class AcademicYearService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.years = AcademicYearRepository(session)
        self.schools = SchoolRepository(session)
        self.writer = ScopedWriter(session, context)

    def get(self, scope: SchoolScope, year_id: uuid.UUID) -> AcademicYear:
        year = self.years.get(scope.school_id, year_id, filters=[_visible(scope, AcademicYear)])
        if year is None:
            raise NotFoundError("Academic year not found.")
        return year

    def list(
        self,
        scope: SchoolScope,
        *,
        is_active: bool | None,
        is_current: bool | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[AcademicYear], int]:
        filters = [
            _visible(scope, AcademicYear),
            *_active_filters(AcademicYear.is_active, is_active),
        ]
        if is_current is not None:
            filters.append(AcademicYear.is_current == is_current)
        return self.years.list(scope.school_id, filters=filters, offset=offset, limit=limit)

    def create(self, scope: SchoolScope, data: AcademicYearCreate) -> AcademicYear:
        year = AcademicYear(school_id=scope.school_id, **data.model_dump())
        self.session.add(year)
        self.writer.flush(YEAR_CONSTRAINTS)
        self.writer.audit(
            AuditAction.ACADEMIC_YEAR_CREATED,
            actor_id=scope.actor_id,
            resource_type="academic_year",
            resource_id=year.id,
            school_id=scope.school_id,
            name=year.name,
            start_date=str(year.start_date),
            end_date=str(year.end_date),
        )
        self.session.commit()
        return year

    def update(
        self, scope: SchoolScope, year_id: uuid.UUID, data: AcademicYearUpdate
    ) -> AcademicYear:
        year = self.years.get(scope.school_id, year_id, for_update=True)
        if year is None:
            raise NotFoundError("Academic year not found.")
        changes = data.changes()
        # Neither date is nullable, so None means "not provided".
        start = data.start_date if data.start_date is not None else year.start_date
        end = data.end_date if data.end_date is not None else year.end_date
        if start >= end:
            raise BusinessRuleError(
                "start_date must be before end_date.",
                details=[
                    ErrorDetail(loc=["body", "end_date"], message="Must be after start_date.")
                ],
            )
        if changes.get("is_active") is False and year.is_current:
            raise BusinessRuleError(
                "The current academic year cannot be deactivated. Make another year current first."
            )
        diff = apply_changes(year, changes)
        if diff:
            self.writer.flush(YEAR_CONSTRAINTS)
            self.writer.audit(
                AuditAction.ACADEMIC_YEAR_UPDATED,
                actor_id=scope.actor_id,
                resource_type="academic_year",
                resource_id=year.id,
                school_id=scope.school_id,
                changes=diff,
            )
            self.session.commit()
        return year

    def set_current(self, scope: SchoolScope, year_id: uuid.UUID) -> AcademicYear:
        """Atomically make ``year_id`` the school's only current year.

        Concurrency: the school row is locked ``FOR UPDATE`` so concurrent switches
        for the same school run one after another (last one wins). Independently,
        the partial unique index ``uq_academic_years_one_current_per_school`` makes
        two current years impossible even if a code path forgot the lock.
        """
        self.schools.lock(scope.school_id)
        year = self.years.get(scope.school_id, year_id, for_update=True)
        if year is None:
            raise NotFoundError("Academic year not found.")
        if not year.is_active:
            raise BusinessRuleError("An inactive academic year cannot be made current.")
        previous = self.years.current(scope.school_id)
        if previous is not None and previous.id == year.id:
            self.session.commit()  # release the lock; nothing to change
            return year

        def switch() -> None:
            self.years.clear_current(scope.school_id)  # UPDATE runs before the flush below
            year.is_current = True

        self.writer.flush(YEAR_CONSTRAINTS, before=switch)
        self.writer.audit(
            AuditAction.ACADEMIC_YEAR_SET_CURRENT,
            actor_id=scope.actor_id,
            resource_type="academic_year",
            resource_id=year.id,
            school_id=scope.school_id,
            previous_current_id=str(previous.id) if previous else None,
        )
        self.session.commit()
        self.session.refresh(year)
        return year


# ========================================================================== classes
class ClassService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.writer = ScopedWriter(session, context)

    def get(self, scope: SchoolScope, class_id: uuid.UUID) -> SchoolClass:
        item = self.classes.get(scope.school_id, class_id, filters=[_visible(scope, SchoolClass)])
        if item is None:
            raise NotFoundError("Class not found.")
        return item

    def list(
        self, scope: SchoolScope, *, is_active: bool | None, offset: int, limit: int
    ) -> tuple[Sequence[SchoolClass], int]:
        return self.classes.list(
            scope.school_id,
            filters=[
                _visible(scope, SchoolClass),
                *_active_filters(SchoolClass.is_active, is_active),
            ],
            offset=offset,
            limit=limit,
        )

    def create(self, scope: SchoolScope, data: ClassCreate) -> SchoolClass:
        item = SchoolClass(school_id=scope.school_id, **data.model_dump())
        self.session.add(item)
        self.writer.flush(CLASS_CONSTRAINTS)
        self.writer.audit(
            AuditAction.CLASS_CREATED,
            actor_id=scope.actor_id,
            resource_type="class",
            resource_id=item.id,
            school_id=scope.school_id,
            name=item.name,
        )
        self.session.commit()
        return item

    def update(self, scope: SchoolScope, class_id: uuid.UUID, data: ClassUpdate) -> SchoolClass:
        item = self.classes.get(scope.school_id, class_id, for_update=True)
        if item is None:
            raise NotFoundError("Class not found.")
        changes = data.changes()
        # No silent cascade: sections must be deactivated explicitly first. The class row
        # lock (above) serialises this check against concurrent section creation.
        deactivating = changes.get("is_active") is False and item.is_active
        if deactivating and self.sections.count_active_in_class(item.id):
            raise ConflictError("Deactivate the class's active sections first.")
        diff = apply_changes(item, changes)
        if diff:
            self.writer.flush(CLASS_CONSTRAINTS)
            self.writer.audit(
                _activation_action(
                    diff,
                    AuditAction.CLASS_UPDATED,
                    AuditAction.CLASS_DEACTIVATED,
                    AuditAction.CLASS_REACTIVATED,
                ),
                actor_id=scope.actor_id,
                resource_type="class",
                resource_id=item.id,
                school_id=scope.school_id,
                changes=diff,
            )
            self.session.commit()
        return item


# ========================================================================= sections
class SectionService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.writer = ScopedWriter(session, context)

    def get(self, scope: SchoolScope, section_id: uuid.UUID) -> Section:
        item = self.sections.get(scope.school_id, section_id, filters=[_visible(scope, Section)])
        if item is None:
            raise NotFoundError("Section not found.")
        return item

    def list(
        self,
        scope: SchoolScope,
        *,
        class_id: uuid.UUID | None,
        is_active: bool | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[Section], int]:
        filters = [_visible(scope, Section), *_active_filters(Section.is_active, is_active)]
        if class_id is not None:
            filters.append(Section.class_id == class_id)
        return self.sections.list(scope.school_id, filters=filters, offset=offset, limit=limit)

    def create(self, scope: SchoolScope, data: SectionCreate) -> Section:
        # Lock the class so it cannot be deactivated while a section is added to it.
        parent = self.classes.get(scope.school_id, data.class_id, for_update=True)
        if parent is None:
            # Same answer for unknown ids and other schools' classes.
            raise _reference_error("class_id", "Class not found.")
        if not parent.is_active:
            raise _reference_error("class_id", "Class is inactive.")
        item = Section(school_id=scope.school_id, **data.model_dump())
        self.session.add(item)
        self.writer.flush(SECTION_CONSTRAINTS)
        self.writer.audit(
            AuditAction.SECTION_CREATED,
            actor_id=scope.actor_id,
            resource_type="section",
            resource_id=item.id,
            school_id=scope.school_id,
            class_id=str(parent.id),
            name=item.name,
        )
        self.session.commit()
        return item

    def update(self, scope: SchoolScope, section_id: uuid.UUID, data: SectionUpdate) -> Section:
        item = self.sections.get(scope.school_id, section_id, for_update=True)
        if item is None:
            raise NotFoundError("Section not found.")
        changes = data.changes()
        if changes.get("is_active") is True and not item.is_active:
            parent = self.classes.get(scope.school_id, item.class_id)
            if parent is None or not parent.is_active:
                raise BusinessRuleError("Reactivate the class before its sections.")
        diff = apply_changes(item, changes)
        if diff:
            self.writer.flush(SECTION_CONSTRAINTS)
            self.writer.audit(
                _activation_action(
                    diff,
                    AuditAction.SECTION_UPDATED,
                    AuditAction.SECTION_DEACTIVATED,
                    AuditAction.SECTION_REACTIVATED,
                ),
                actor_id=scope.actor_id,
                resource_type="section",
                resource_id=item.id,
                school_id=scope.school_id,
                changes=diff,
            )
            self.session.commit()
        return item


# ========================================================================= subjects
class SubjectService:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.subjects = SubjectRepository(session)
        self.writer = ScopedWriter(session, context)

    def get(self, scope: SchoolScope, subject_id: uuid.UUID) -> Subject:
        item = self.subjects.get(scope.school_id, subject_id, filters=[_visible(scope, Subject)])
        if item is None:
            raise NotFoundError("Subject not found.")
        return item

    def list(
        self, scope: SchoolScope, *, is_active: bool | None, offset: int, limit: int
    ) -> tuple[Sequence[Subject], int]:
        return self.subjects.list(
            scope.school_id,
            filters=[_visible(scope, Subject), *_active_filters(Subject.is_active, is_active)],
            offset=offset,
            limit=limit,
        )

    def create(self, scope: SchoolScope, data: SubjectCreate) -> Subject:
        item = Subject(school_id=scope.school_id, **data.model_dump())
        self.session.add(item)
        self.writer.flush(SUBJECT_CONSTRAINTS)
        self.writer.audit(
            AuditAction.SUBJECT_CREATED,
            actor_id=scope.actor_id,
            resource_type="subject",
            resource_id=item.id,
            school_id=scope.school_id,
            name=item.name,
        )
        self.session.commit()
        return item

    def update(self, scope: SchoolScope, subject_id: uuid.UUID, data: SubjectUpdate) -> Subject:
        item = self.subjects.get(scope.school_id, subject_id, for_update=True)
        if item is None:
            raise NotFoundError("Subject not found.")
        diff = apply_changes(item, data.changes())
        if diff:
            self.writer.flush(SUBJECT_CONSTRAINTS)
            self.writer.audit(
                _activation_action(
                    diff,
                    AuditAction.SUBJECT_UPDATED,
                    AuditAction.SUBJECT_DEACTIVATED,
                    AuditAction.SUBJECT_REACTIVATED,
                ),
                actor_id=scope.actor_id,
                resource_type="subject",
                resource_id=item.id,
                school_id=scope.school_id,
                changes=diff,
            )
            self.session.commit()
        return item
