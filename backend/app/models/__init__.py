"""ORM models.

Import every model module here so ``Base.metadata`` is complete when Alembic
autogenerates migrations.
"""

from app.models.academic import AcademicYear, SchoolClass, Section, Subject
from app.models.attendance import (
    AttendanceRecord,
    AttendanceRecordChange,
    AttendanceSession,
    AttendanceStatus,
    SessionStatus,
    SessionType,
)
from app.models.audit import AuditLog
from app.models.auth import AuthSession, LoginAttempt, OtpChallenge, RefreshToken
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.faculty import AssignmentStatus, Faculty, FacultyAssignment, FacultyStatus
from app.models.people import (
    EnrollmentStatus,
    Parent,
    RelationshipType,
    Student,
    StudentEnrollment,
    StudentParent,
    StudentStatus,
)
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.school import School
from app.models.user import User, UserStatus

__all__ = [
    "AcademicYear",
    "AssignmentStatus",
    "AttendanceRecord",
    "AttendanceRecordChange",
    "AttendanceSession",
    "AttendanceStatus",
    "AuditLog",
    "AuthSession",
    "Base",
    "EnrollmentStatus",
    "Faculty",
    "FacultyAssignment",
    "FacultyStatus",
    "LoginAttempt",
    "OtpChallenge",
    "Parent",
    "Permission",
    "RefreshToken",
    "RelationshipType",
    "Role",
    "RolePermission",
    "School",
    "SchoolClass",
    "Section",
    "SessionStatus",
    "SessionType",
    "Student",
    "StudentEnrollment",
    "StudentParent",
    "StudentStatus",
    "Subject",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
    "UserRole",
    "UserStatus",
]
