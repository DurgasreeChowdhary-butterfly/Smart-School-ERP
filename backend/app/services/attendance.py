"""Attendance sessions, records, corrections and summaries.

Rules
-----
* A session names a school-local date inside its academic year and never in the future
  (in the school's time zone). Its year, class, section and subject must belong to the
  school and be active, and the section must belong to the class (also enforced by
  composite foreign keys). Placement, date, subject and period are immutable;
  ``status`` (de)activates. Nothing is deleted.
* A record can only be created for a student whose enrollment placed them in the
  session's year/class/section **on the session date** (late joiners, leavers and
  students who moved are handled by the enrollment history, not by their current
  placement). The record is pinned to that enrollment (composite foreign key), so later
  moves never change past attendance.
* Bulk marking is all-or-nothing and create-only: one invalid, duplicate or
  already-marked student rejects the whole request. Marking may be incomplete; the
  ``unmarked`` endpoint lists eligible students without a record. Missing records are
  never treated as present or absent.
* Changing a record appends to ``attendance_record_changes``. A status change is a
  *correction*: it needs a reason and sets ``corrected_*``; ``original_status`` keeps
  the first recorded value.

Concurrency: every record write locks the session row first (``SELECT ... FOR
UPDATE``), then the record, so writes to one session are serialised and a session
cannot be deactivated mid-write. Partial unique indexes (sessions) and ``UNIQUE
(session_id, student_id)`` (records) are the final authority.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.orm import Session

from app.audit.service import AuditAction
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.request_context import RequestContext
from app.models.academic import AcademicYear
from app.models.attendance import (
    AttendanceRecord,
    AttendanceRecordChange,
    AttendanceSession,
    AttendanceStatus,
    SessionStatus,
    SessionType,
)
from app.models.people import Student
from app.repositories.academic import (
    AcademicYearRepository,
    ClassRepository,
    SectionRepository,
    SubjectRepository,
)
from app.repositories.attendance import (
    AttendanceRecordRepository,
    AttendanceSessionRepository,
    date_range,
)
from app.schemas.attendance import (
    AttendanceBulkCreate,
    AttendanceRecordCreate,
    AttendanceRecordRead,
    AttendanceRecordUpdate,
    AttendanceSessionCreate,
    AttendanceSessionUpdate,
    AttendanceSummary,
)
from app.schemas.common import ErrorDetail
from app.security.attendance_scope import (
    can_create_session,
    is_school_wide,
    record_visibility,
    session_manageability,
    session_visibility,
    today_in_school,
)
from app.security.people_scope import student_visibility
from app.security.school_scope import SchoolScope
from app.services._scoped import ScopedWriter, apply_changes
from app.services.students import body_error

S = AttendanceSession
R = AttendanceRecord

SESSION_CONSTRAINTS = {
    "uq_attendance_sessions_daily_active": (
        "session_date",
        "An active daily attendance session already exists for this section, date and period.",
    ),
    "uq_attendance_sessions_subject_active": (
        "session_date",
        "An active attendance session already exists for this section, subject, date and period.",
    ),
    "fk_attendance_sessions_section_in_class": (
        "section_id",
        "Section does not belong to the class.",
    ),
}
RECORD_CONSTRAINTS = {
    "uq_attendance_records_session_id_student_id": (
        "student_id",
        "Attendance is already recorded for this student in this session.",
    ),
    "fk_attendance_records_enrollment_placement": (
        "student_id",
        "Student is not enrolled in this section on the session date.",
    ),
    "uq_attendance_record_changes_record_id_sequence": (
        "status",
        "The record changed concurrently. Please retry.",
    ),
}
NOT_ENROLLED = "Student is not enrolled in this section on the session date."

type RecordRows = list[tuple[AttendanceRecord, AttendanceSession]]
type UnmarkedRows = list[tuple[Student, uuid.UUID]]


def _now() -> datetime:
    return datetime.now(UTC)


def _status_action(
    before: str,
    after: str,
    updated: AuditAction,
    deactivated: AuditAction,
    reactivated: AuditAction,
) -> AuditAction:
    if before == after:
        return updated
    return reactivated if after == SessionStatus.ACTIVE else deactivated


def _placement(item: AttendanceSession) -> dict[str, object]:
    return {
        "session_id": str(item.id),
        "academic_year_id": str(item.academic_year_id),
        "class_id": str(item.class_id),
        "section_id": str(item.section_id),
        "subject_id": str(item.subject_id) if item.subject_id else None,
        "session_date": str(item.session_date),
        "period": item.period,
    }


def record_read(record: AttendanceRecord, session: AttendanceSession) -> AttendanceRecordRead:
    return AttendanceRecordRead(
        id=record.id,
        school_id=record.school_id,
        session_id=record.session_id,
        student_id=record.student_id,
        enrollment_id=record.enrollment_id,
        academic_year_id=record.academic_year_id,
        class_id=record.class_id,
        section_id=record.section_id,
        subject_id=session.subject_id,
        session_type=session.session_type,
        session_date=session.session_date,
        period=session.period,
        session_status=session.status,
        status=record.status,
        original_status=record.original_status,
        remarks=record.remarks,
        marked_by_user_id=record.marked_by_user_id,
        marked_at=record.marked_at,
        corrected_by_user_id=record.corrected_by_user_id,
        corrected_at=record.corrected_at,
        correction_reason=record.correction_reason,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class _Base:
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        self.session = session
        self.sessions = AttendanceSessionRepository(session)
        self.records = AttendanceRecordRepository(session)
        self.writer = ScopedWriter(session, context)

    def _visible_session(
        self, scope: SchoolScope, session_id: uuid.UUID, *, for_update: bool = False
    ) -> AttendanceSession:
        item = self.sessions.get_visible(
            session_visibility(scope.principal, scope.school_id), session_id, for_update=for_update
        )
        if item is None:
            raise NotFoundError("Attendance session not found.")
        return item

    def _require_manageable(self, scope: SchoolScope, item: AttendanceSession) -> None:
        manageable = self.session.scalar(
            select(
                exists().where(
                    S.id == item.id, session_manageability(scope.principal, scope.school_id)
                )
            )
        )
        if not manageable:
            raise PermissionDeniedError("You may not change attendance for this session.")


# ========================================================================== sessions
class AttendanceSessionService(_Base):
    def __init__(self, session: Session, context: RequestContext | None = None) -> None:
        super().__init__(session, context)
        self.years = AcademicYearRepository(session)
        self.classes = ClassRepository(session)
        self.sections = SectionRepository(session)
        self.subjects = SubjectRepository(session)

    def get(self, scope: SchoolScope, session_id: uuid.UUID) -> AttendanceSession:
        return self._visible_session(scope, session_id)

    def list(
        self,
        scope: SchoolScope,
        *,
        academic_year_id: uuid.UUID | None,
        class_id: uuid.UUID | None,
        section_id: uuid.UUID | None,
        subject_id: uuid.UUID | None,
        session_type: SessionType | None,
        session_date: date | None,
        from_date: date | None,
        to_date: date | None,
        status: SessionStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[Sequence[AttendanceSession], int]:
        # Filters only narrow the authorised set; they are ANDed with visibility.
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (S.academic_year_id, academic_year_id),
                (S.class_id, class_id),
                (S.section_id, section_id),
                (S.subject_id, subject_id),
                (S.session_type, session_type.value if session_type else None),
                (S.status, status.value if status else None),
            )
            if value is not None
        ]
        filters += date_range(S.session_date, on=session_date, start=from_date, end=to_date)
        return self.sessions.list_visible(
            session_visibility(scope.principal, scope.school_id),
            filters,
            offset=offset,
            limit=limit,
        )

    # --------------------------------------------------------------- validation
    def _authorize_create(self, scope: SchoolScope, data: AttendanceSessionCreate) -> None:
        """Checked before anything is looked up, so a refusal reveals nothing about which
        of the referenced records exist."""
        allowed = can_create_session(
            self.session,
            scope.principal,
            scope.school_id,
            academic_year_id=data.academic_year_id,
            class_id=data.class_id,
            section_id=data.section_id,
            subject_id=data.subject_id,
            session_date=data.session_date,
        )
        if allowed:
            return
        if data.subject_id is None and not is_school_wide(scope.principal):
            raise PermissionDeniedError("Daily attendance is recorded by school staff.")
        raise PermissionDeniedError(
            "You are not assigned to this section and subject on that date."
        )

    def _validate_placement(
        self, scope: SchoolScope, data: AttendanceSessionCreate
    ) -> AcademicYear:
        year = self.years.get(scope.school_id, data.academic_year_id)
        if year is None:
            raise body_error("academic_year_id", "Academic year not found.")
        if not year.is_active:
            raise body_error("academic_year_id", "Academic year is inactive.")
        school_class = self.classes.get(scope.school_id, data.class_id)
        if school_class is None:
            raise body_error("class_id", "Class not found.")
        if not school_class.is_active:
            raise body_error("class_id", "Class is inactive.")
        section = self.sections.get(scope.school_id, data.section_id)
        if section is None:
            raise body_error("section_id", "Section not found.")
        if section.class_id != school_class.id:
            raise body_error("section_id", "Section does not belong to the class.")
        if not section.is_active:
            raise body_error("section_id", "Section is inactive.")
        if data.subject_id is not None:
            subject = self.subjects.get(scope.school_id, data.subject_id)
            if subject is None:
                raise body_error("subject_id", "Subject not found.")
            if not subject.is_active:
                raise body_error("subject_id", "Subject is inactive.")
        return year

    def _validate_date(self, scope: SchoolScope, year: AcademicYear, on: date) -> None:
        if not year.start_date <= on <= year.end_date:
            raise body_error("session_date", "Must fall within the academic year.")
        if on > today_in_school(self.session, scope.school_id):
            raise body_error("session_date", "Must not be in the future (school time zone).")

    # ------------------------------------------------------------------- writes
    def create(self, scope: SchoolScope, data: AttendanceSessionCreate) -> AttendanceSession:
        self._authorize_create(scope, data)
        year = self._validate_placement(scope, data)
        self._validate_date(scope, year, data.session_date)
        item = AttendanceSession(
            school_id=scope.school_id, created_by_user_id=scope.actor_id, **data.model_dump()
        )
        self.writer.flush(SESSION_CONSTRAINTS, before=lambda: self.session.add(item))
        self.writer.audit(
            AuditAction.ATTENDANCE_SESSION_CREATED,
            actor_id=scope.actor_id,
            resource_type="attendance_session",
            resource_id=item.id,
            school_id=scope.school_id,
            **_placement(item),
        )
        self.session.commit()
        return item

    def update(
        self, scope: SchoolScope, session_id: uuid.UUID, data: AttendanceSessionUpdate
    ) -> AttendanceSession:
        item = self._visible_session(scope, session_id, for_update=True)
        self._require_manageable(scope, item)
        changes = data.changes()
        if isinstance(changes.get("status"), SessionStatus):
            changes["status"] = str(changes["status"])
        start = changes.get("start_time", item.start_time)
        end = changes.get("end_time", item.end_time)
        if start is not None and end is not None and start >= end:  # type: ignore[operator]
            raise body_error("end_time", "Must be after start_time.")
        previous_status = item.status
        diff: list[dict[str, object]] = []
        self.writer.flush(
            SESSION_CONSTRAINTS, before=lambda: diff.extend(apply_changes(item, changes))
        )
        if diff:
            self.writer.audit(
                _status_action(
                    previous_status,
                    item.status,
                    AuditAction.ATTENDANCE_SESSION_UPDATED,
                    AuditAction.ATTENDANCE_SESSION_DEACTIVATED,
                    AuditAction.ATTENDANCE_SESSION_REACTIVATED,
                ),
                actor_id=scope.actor_id,
                resource_type="attendance_session",
                resource_id=item.id,
                school_id=scope.school_id,
                changes=diff,
                **_placement(item),
            )
        self.session.commit()
        self.session.refresh(item)
        return item


# =========================================================================== records
class AttendanceRecordService(_Base):
    def _session_for_write(self, scope: SchoolScope, session_id: uuid.UUID) -> AttendanceSession:
        """Lock the session (serialises every write to it), then authorise the write."""
        item = self._visible_session(scope, session_id, for_update=True)
        self._require_manageable(scope, item)
        if item.status != SessionStatus.ACTIVE:
            raise BusinessRuleError("The attendance session is inactive.")
        return item

    def _new_record(
        self,
        scope: SchoolScope,
        item: AttendanceSession,
        data: AttendanceRecordCreate,
        enrollment_id: uuid.UUID,
        now: datetime,
    ) -> AttendanceRecord:
        return AttendanceRecord(
            school_id=scope.school_id,
            session_id=item.id,
            enrollment_id=enrollment_id,
            student_id=data.student_id,
            academic_year_id=item.academic_year_id,
            class_id=item.class_id,
            section_id=item.section_id,
            status=data.status.value,
            original_status=data.status.value,
            remarks=data.remarks,
            marked_by_user_id=scope.actor_id,
            marked_at=now,
        )

    # -------------------------------------------------------------------- reads
    def get(
        self, scope: SchoolScope, record_id: uuid.UUID
    ) -> tuple[AttendanceRecord, AttendanceSession]:
        row = self.records.get_visible(
            record_visibility(scope.principal, scope.school_id), record_id
        )
        if row is None:
            raise NotFoundError("Attendance record not found.")
        return row

    def list(
        self,
        scope: SchoolScope,
        *,
        session_id: uuid.UUID | None = None,
        student_id: uuid.UUID | None = None,
        academic_year_id: uuid.UUID | None = None,
        class_id: uuid.UUID | None = None,
        section_id: uuid.UUID | None = None,
        subject_id: uuid.UUID | None = None,
        session_type: SessionType | None = None,
        status: AttendanceStatus | None = None,
        session_date: date | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        offset: int,
        limit: int,
    ) -> tuple[RecordRows, int]:
        filters: list[ColumnElement[bool]] = [
            column == value
            for column, value in (
                (R.session_id, session_id),
                (R.student_id, student_id),
                (R.academic_year_id, academic_year_id),
                (R.class_id, class_id),
                (R.section_id, section_id),
                (S.subject_id, subject_id),
                (S.session_type, session_type.value if session_type else None),
                (R.status, status.value if status else None),
            )
            if value is not None
        ]
        filters += date_range(S.session_date, on=session_date, start=from_date, end=to_date)
        return self.records.list_visible(
            record_visibility(scope.principal, scope.school_id),
            filters,
            offset=offset,
            limit=limit,
        )

    def list_for_session(
        self,
        scope: SchoolScope,
        session_id: uuid.UUID,
        *,
        status: AttendanceStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[RecordRows, int]:
        self._visible_session(scope, session_id)  # 404 for sessions outside the scope
        return self.list(scope, session_id=session_id, status=status, offset=offset, limit=limit)

    def unmarked(
        self, scope: SchoolScope, session_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[UnmarkedRows, int]:
        item = self._visible_session(scope, session_id)
        return self.records.unmarked(item, offset=offset, limit=limit)

    def history(self, scope: SchoolScope, record_id: uuid.UUID) -> Sequence[AttendanceRecordChange]:
        record, _ = self.get(scope, record_id)
        return self.records.changes(record.id)

    # ------------------------------------------------------------------- writes
    def create(
        self, scope: SchoolScope, session_id: uuid.UUID, data: AttendanceRecordCreate
    ) -> tuple[AttendanceRecord, AttendanceSession]:
        item = self._session_for_write(scope, session_id)
        if self.records.students_with_records(item.id, [data.student_id]):
            raise ConflictError(
                "Attendance is already recorded for this student; correct it instead.",
                details=[ErrorDetail(loc=["body", "student_id"], message="Already recorded.")],
            )
        enrollments = self.records.eligible_enrollments(item, [data.student_id])
        if data.student_id not in enrollments:
            raise body_error("student_id", NOT_ENROLLED)
        record = self._new_record(scope, item, data, enrollments[data.student_id], _now())
        self.writer.flush(RECORD_CONSTRAINTS, before=lambda: self.session.add(record))
        self.writer.audit(
            AuditAction.ATTENDANCE_RECORD_CREATED,
            actor_id=scope.actor_id,
            resource_type="attendance_record",
            resource_id=record.id,
            school_id=scope.school_id,
            student_id=str(record.student_id),
            status=record.status,
            **_placement(item),
        )
        self.session.commit()
        return record, item

    def bulk_create(
        self, scope: SchoolScope, session_id: uuid.UUID, data: AttendanceBulkCreate
    ) -> tuple[Sequence[AttendanceRecord], AttendanceSession]:
        item = self._session_for_write(scope, session_id)
        student_ids = [entry.student_id for entry in data.records]

        def problems(indexes: Sequence[int], message: str) -> list[ErrorDetail]:
            return [
                ErrorDetail(loc=["body", "records", i, "student_id"], message=message)
                for i in indexes
            ]

        first_index: dict[uuid.UUID, int] = {}
        repeated = [i for i, sid in enumerate(student_ids) if first_index.setdefault(sid, i) != i]
        if repeated:
            raise BusinessRuleError(
                "Each student may appear only once.",
                details=problems(repeated, "Duplicate student in this request."),
            )
        enrollments = self.records.eligible_enrollments(item, student_ids)
        ineligible = [i for i, sid in enumerate(student_ids) if sid not in enrollments]
        if ineligible:
            raise BusinessRuleError(
                "Some students are not enrolled in this section on the session date.",
                details=problems(ineligible, NOT_ENROLLED),
            )
        existing = self.records.students_with_records(item.id, student_ids)
        if existing:
            raise ConflictError(
                "Attendance is already recorded for some students; correct those records instead.",
                details=problems(
                    [i for i, sid in enumerate(student_ids) if sid in existing],
                    "Already recorded.",
                ),
            )
        now = _now()
        created = [
            self._new_record(scope, item, entry, enrollments[entry.student_id], now)
            for entry in data.records
        ]
        self.writer.flush(RECORD_CONSTRAINTS, before=lambda: self.session.add_all(created))
        counts = {status.value: 0 for status in AttendanceStatus}
        for record in created:
            counts[record.status] += 1
        self.writer.audit(
            AuditAction.ATTENDANCE_RECORD_BULK_CREATED,
            actor_id=scope.actor_id,
            resource_type="attendance_session",
            resource_id=item.id,
            school_id=scope.school_id,
            records=len(created),
            status_counts=counts,
            **_placement(item),
        )
        self.session.commit()
        return created, item

    def update(
        self, scope: SchoolScope, record_id: uuid.UUID, data: AttendanceRecordUpdate
    ) -> tuple[AttendanceRecord, AttendanceSession]:
        found, _ = self.get(scope, record_id)
        # Lock order: session first, then the record (same as create/bulk).
        item = self._session_for_write(scope, found.session_id)
        row = self.records.get_visible(
            record_visibility(scope.principal, scope.school_id), record_id, for_update=True
        )
        assert row is not None  # noqa: S101 - records are never deleted
        record = row[0]
        changes = data.changes()
        reason = changes.pop("reason", None)
        new_status = str(changes.get("status", record.status))
        new_remarks = changes.get("remarks", record.remarks)
        if new_status == record.status and new_remarks == record.remarks:
            return record, item
        corrected = new_status != record.status
        if corrected and reason is None:
            raise body_error("reason", "A reason is required to change an attendance status.")
        now = _now()
        change = AttendanceRecordChange(
            school_id=scope.school_id,
            record_id=record.id,
            sequence=self.records.next_change_sequence(record.id),
            previous_status=record.status,
            new_status=new_status,
            previous_remarks=record.remarks,
            new_remarks=new_remarks,
            reason=reason,
            changed_by_user_id=scope.actor_id,
            changed_at=now,
        )
        previous_status = record.status

        def apply() -> None:
            self.session.add(change)
            record.status = new_status
            record.remarks = new_remarks  # type: ignore[assignment]
            if corrected:
                record.corrected_by_user_id = scope.actor_id
                record.corrected_at = now
                record.correction_reason = reason  # type: ignore[assignment]

        self.writer.flush(RECORD_CONSTRAINTS, before=apply)
        self.writer.audit(
            AuditAction.ATTENDANCE_RECORD_CORRECTED
            if corrected
            else AuditAction.ATTENDANCE_RECORD_UPDATED,
            actor_id=scope.actor_id,
            resource_type="attendance_record",
            resource_id=record.id,
            school_id=scope.school_id,
            student_id=str(record.student_id),
            change_id=str(change.id),
            previous_status=previous_status,
            new_status=new_status,
            reason=reason,
            # Remarks can hold personal (e.g. health) details: recorded by name only.
            remarks_changed=new_remarks != change.previous_remarks,
            **_placement(item),
        )
        self.session.commit()
        self.session.refresh(record)
        return record, item

    # ------------------------------------------------------------------ summary
    def summary(
        self,
        scope: SchoolScope,
        student_id: uuid.UUID,
        *,
        academic_year_id: uuid.UUID | None,
        from_date: date | None,
        to_date: date | None,
        subject_id: uuid.UUID | None,
        session_type: SessionType | None,
    ) -> AttendanceSummary:
        visible_student = self.session.scalar(
            select(
                exists().where(
                    Student.id == student_id,
                    student_visibility(scope.principal, scope.school_id),
                )
            )
        )
        if not visible_student:
            raise NotFoundError("Student not found.")
        filters: list[ColumnElement[bool]] = [
            R.student_id == student_id,
            S.status == SessionStatus.ACTIVE.value,
            *[
                column == value
                for column, value in (
                    (R.academic_year_id, academic_year_id),
                    (S.subject_id, subject_id),
                    (S.session_type, session_type.value if session_type else None),
                )
                if value is not None
            ],
            *date_range(S.session_date, start=from_date, end=to_date),
        ]
        counts = self.records.status_counts(
            record_visibility(scope.principal, scope.school_id), filters
        )
        return build_summary(
            student_id,
            counts,
            academic_year_id=academic_year_id,
            from_date=from_date,
            to_date=to_date,
            subject_id=subject_id,
            session_type=session_type,
        )


def build_summary(
    student_id: uuid.UUID,
    counts: dict[str, int],
    *,
    academic_year_id: uuid.UUID | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    subject_id: uuid.UUID | None = None,
    session_type: SessionType | None = None,
) -> AttendanceSummary:
    """The single attendance-percentage rule: LATE counts as attended, EXCUSED is left
    out of the denominator, unmarked sessions are not counted at all."""
    present = counts[AttendanceStatus.PRESENT]
    late = counts[AttendanceStatus.LATE]
    excused = counts[AttendanceStatus.EXCUSED]
    total = sum(counts.values())
    attended = present + late
    counted = total - excused
    return AttendanceSummary(
        student_id=student_id,
        academic_year_id=academic_year_id,
        from_date=from_date,
        to_date=to_date,
        subject_id=subject_id,
        session_type=session_type.value if session_type else None,
        total_sessions=total,
        present=present,
        absent=counts[AttendanceStatus.ABSENT],
        late=late,
        excused=excused,
        attended=attended,
        counted_sessions=counted,
        attendance_percentage=round(attended * 100 / counted, 2) if counted else None,
    )
