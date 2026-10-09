"""Write audit records for security-sensitive actions.

Audit rows are added to the caller's session and committed with the surrounding
unit of work. Metadata is sanitised: any key that looks like it could carry a
secret is redacted, so a careless caller cannot persist credentials.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.core.request_context import RequestContext
from app.models.audit import AuditLog

logger = logging.getLogger("app.audit")

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|secret|token|otp|^code$|credential|authorization|cookie|hash", re.IGNORECASE
)
_MAX_DEPTH = 4


class AuditAction(StrEnum):
    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_THROTTLED = "auth.login.throttled"
    LOGOUT = "auth.logout"
    REFRESH_SUCCEEDED = "auth.refresh.succeeded"
    REFRESH_FAILED = "auth.refresh.failed"
    REFRESH_REUSE_DETECTED = "auth.refresh.reuse_detected"
    OTP_REQUESTED = "auth.otp.requested"
    OTP_THROTTLED = "auth.otp.throttled"
    OTP_VERIFIED = "auth.otp.verified"
    OTP_FAILED = "auth.otp.failed"
    ROLES_CHANGED = "rbac.user_roles.changed"
    USER_CREATED = "user.created"
    SCHOOL_CREATED = "school.created"
    SCHOOL_UPDATED = "school.updated"
    ACADEMIC_YEAR_CREATED = "academic_year.created"
    ACADEMIC_YEAR_UPDATED = "academic_year.updated"
    ACADEMIC_YEAR_SET_CURRENT = "academic_year.set_current"
    CLASS_CREATED = "class.created"
    CLASS_UPDATED = "class.updated"
    CLASS_DEACTIVATED = "class.deactivated"
    CLASS_REACTIVATED = "class.reactivated"
    SECTION_CREATED = "section.created"
    SECTION_UPDATED = "section.updated"
    SECTION_DEACTIVATED = "section.deactivated"
    SECTION_REACTIVATED = "section.reactivated"
    SUBJECT_CREATED = "subject.created"
    SUBJECT_UPDATED = "subject.updated"
    SUBJECT_DEACTIVATED = "subject.deactivated"
    SUBJECT_REACTIVATED = "subject.reactivated"
    STUDENT_CREATED = "student.created"
    STUDENT_UPDATED = "student.updated"
    STUDENT_DEACTIVATED = "student.deactivated"
    STUDENT_REACTIVATED = "student.reactivated"
    PARENT_CREATED = "parent.created"
    PARENT_UPDATED = "parent.updated"
    PARENT_DEACTIVATED = "parent.deactivated"
    PARENT_REACTIVATED = "parent.reactivated"
    STUDENT_PARENT_CREATED = "student_parent.created"
    STUDENT_PARENT_UPDATED = "student_parent.updated"
    STUDENT_PARENT_DEACTIVATED = "student_parent.deactivated"
    STUDENT_PARENT_REACTIVATED = "student_parent.reactivated"
    ENROLLMENT_CREATED = "enrollment.created"
    ENROLLMENT_UPDATED = "enrollment.updated"
    ENROLLMENT_CURRENT_CHANGED = "enrollment.current_changed"
    FACULTY_CREATED = "faculty.created"
    FACULTY_UPDATED = "faculty.updated"
    FACULTY_DEACTIVATED = "faculty.deactivated"
    FACULTY_REACTIVATED = "faculty.reactivated"
    FACULTY_ASSIGNMENT_CREATED = "faculty_assignment.created"
    FACULTY_ASSIGNMENT_UPDATED = "faculty_assignment.updated"
    FACULTY_ASSIGNMENT_DEACTIVATED = "faculty_assignment.deactivated"
    FACULTY_ASSIGNMENT_REACTIVATED = "faculty_assignment.reactivated"
    ATTENDANCE_SESSION_CREATED = "attendance_session.created"
    ATTENDANCE_SESSION_UPDATED = "attendance_session.updated"
    ATTENDANCE_SESSION_DEACTIVATED = "attendance_session.deactivated"
    ATTENDANCE_SESSION_REACTIVATED = "attendance_session.reactivated"
    ATTENDANCE_RECORD_CREATED = "attendance_record.created"
    ATTENDANCE_RECORD_BULK_CREATED = "attendance_record.bulk_created"
    ATTENDANCE_RECORD_UPDATED = "attendance_record.updated"
    ATTENDANCE_RECORD_CORRECTED = "attendance_record.corrected"
    EXAM_CREATED = "exam.created"
    EXAM_UPDATED = "exam.updated"
    EXAM_SCHEDULED = "exam.scheduled"
    EXAM_RETURNED_TO_DRAFT = "exam.returned_to_draft"
    EXAM_MARKS_ENTRY_OPENED = "exam.marks_entry_opened"
    EXAM_CANCELLED = "exam.cancelled"
    EXAM_PUBLISHED = "exam.published"
    EXAM_REOPENED = "exam.reopened"
    EXAM_SUBJECT_CREATED = "exam_subject.created"
    EXAM_SUBJECT_UPDATED = "exam_subject.updated"
    EXAM_SUBJECT_DELETED = "exam_subject.deleted"
    EXAM_MARK_CREATED = "exam_mark.created"
    EXAM_MARK_BULK_CREATED = "exam_mark.bulk_created"
    EXAM_MARK_CORRECTED = "exam_mark.corrected"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


def sanitize_metadata(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if _SENSITIVE_KEY.search(str(key))
            else sanitize_metadata(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [sanitize_metadata(item, depth + 1) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def record_audit(
    session: Session,
    action: AuditAction,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    context: RequestContext | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    context = context or RequestContext()
    entry = AuditLog(
        user_id=user_id,
        action=action.value,
        outcome=outcome.value,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        ip_address=context.ip_address,
        user_agent=context.user_agent,
        request_id=context.request_id,
        details=sanitize_metadata(details or {}),
        # Application clock, not now(): now() is the transaction start time.
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    logger.info(
        "audit event",
        extra={
            "audit_action": action.value,
            "audit_outcome": outcome.value,
            "audit_user_id": str(user_id) if user_id else None,
        },
    )
    return entry
