"""RBAC catalog: the single source of truth for roles, permissions and their mapping.

The database copy is seeded by Alembic migrations (a literal snapshot, so old
migrations never change when this file does). ``tests/integration`` asserts the
database matches this catalog, so adding a permission requires both a code
change here *and* a new migration.

Permissions grant an *action type*. Persona permissions such as
``attendance.read`` held by PARENT/STUDENT/FACULTY are additionally limited to a
resource scope (linked children / self / assigned classes) by the resource
policies in :mod:`app.security.authorization`.
"""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    SCHOOL_ADMIN = "SCHOOL_ADMIN"
    MANAGEMENT = "MANAGEMENT"
    FACULTY = "FACULTY"
    PARENT = "PARENT"
    STUDENT = "STUDENT"


class PermissionName(StrEnum):
    # authentication
    AUTH_LOGIN = "auth.login"
    AUTH_REFRESH = "auth.refresh"
    AUTH_LOGOUT = "auth.logout"
    AUTH_ME = "auth.me"
    # users
    USER_READ = "user.read"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DEACTIVATE = "user.deactivate"
    USER_ASSIGN_ROLE = "user.assign_role"
    # roles / permissions
    ROLE_READ = "role.read"
    ROLE_MANAGE = "role.manage"
    PERMISSION_READ = "permission.read"
    PERMISSION_MANAGE = "permission.manage"
    # school
    SCHOOL_READ = "school.read"
    SCHOOL_MANAGE = "school.manage"
    # students
    STUDENT_READ_SELF = "student.read_self"
    STUDENT_READ = "student.read"
    STUDENT_CREATE = "student.create"
    STUDENT_UPDATE = "student.update"
    STUDENT_DEACTIVATE = "student.deactivate"
    # parents
    PARENT_READ_SELF = "parent.read_self"
    PARENT_READ_CHILD = "parent.read_child"
    # faculty
    FACULTY_READ_SELF = "faculty.read_self"
    FACULTY_READ_ASSIGNED_STUDENTS = "faculty.read_assigned_students"
    FACULTY_MANAGE = "faculty.manage"
    # attendance
    ATTENDANCE_READ = "attendance.read"
    ATTENDANCE_MANAGE = "attendance.manage"
    # academic structure
    ACADEMIC_READ = "academic.read"
    ACADEMIC_MANAGE = "academic.manage"
    # marks
    MARKS_READ = "marks.read"
    MARKS_MANAGE = "marks.manage"
    MARKS_PUBLISH = "marks.publish"
    # assignments
    ASSIGNMENT_READ = "assignment.read"
    ASSIGNMENT_MANAGE = "assignment.manage"
    ASSIGNMENT_SUBMIT = "assignment.submit"
    # timetable
    TIMETABLE_READ = "timetable.read"
    TIMETABLE_MANAGE = "timetable.manage"
    # announcements
    ANNOUNCEMENT_READ = "announcement.read"
    ANNOUNCEMENT_MANAGE = "announcement.manage"
    # notifications
    NOTIFICATION_READ = "notification.read"
    NOTIFICATION_MANAGE = "notification.manage"
    # reports / analytics / AI
    REPORT_READ = "report.read"
    ANALYTICS_READ = "analytics.read"
    AI_READ = "ai.read"
    AI_MANAGE = "ai.manage"


ROLE_DESCRIPTIONS: dict[RoleName, str] = {
    RoleName.SUPER_ADMIN: "Platform-wide administrator (all schools).",
    RoleName.SCHOOL_ADMIN: "Administers one school: users, structure and records.",
    RoleName.MANAGEMENT: "School leadership: oversight, reporting and result publication.",
    RoleName.FACULTY: "Teaching staff: works with assigned classes and students.",
    RoleName.PARENT: "Parent or guardian: views linked children's information.",
    RoleName.STUDENT: "Student: views own information.",
}

PERMISSION_DESCRIPTIONS: dict[PermissionName, str] = {
    PermissionName.AUTH_LOGIN: "Sign in.",
    PermissionName.AUTH_REFRESH: "Renew a session with a refresh token.",
    PermissionName.AUTH_LOGOUT: "Sign out.",
    PermissionName.AUTH_ME: "View own account profile.",
    PermissionName.USER_READ: "View user accounts.",
    PermissionName.USER_CREATE: "Create user accounts.",
    PermissionName.USER_UPDATE: "Update user accounts.",
    PermissionName.USER_DEACTIVATE: "Deactivate user accounts.",
    PermissionName.USER_ASSIGN_ROLE: "Assign or remove user roles.",
    PermissionName.ROLE_READ: "View roles.",
    PermissionName.ROLE_MANAGE: "Create or modify role definitions.",
    PermissionName.PERMISSION_READ: "View permissions.",
    PermissionName.PERMISSION_MANAGE: "Modify the permission catalog.",
    PermissionName.SCHOOL_READ: "View school information.",
    PermissionName.SCHOOL_MANAGE: "Manage school settings.",
    PermissionName.STUDENT_READ_SELF: "View own student record.",
    PermissionName.STUDENT_READ: "View student records within scope.",
    PermissionName.STUDENT_CREATE: "Create student records.",
    PermissionName.STUDENT_UPDATE: "Update student records.",
    PermissionName.STUDENT_DEACTIVATE: "Deactivate student records.",
    PermissionName.PARENT_READ_SELF: "View own parent profile.",
    PermissionName.PARENT_READ_CHILD: "View linked children's records.",
    PermissionName.FACULTY_READ_SELF: "View own faculty profile.",
    PermissionName.FACULTY_READ_ASSIGNED_STUDENTS: "View students in assigned classes.",
    PermissionName.FACULTY_MANAGE: "Manage faculty records and assignments.",
    PermissionName.ATTENDANCE_READ: "View attendance within scope.",
    PermissionName.ATTENDANCE_MANAGE: "Record or correct attendance within scope.",
    PermissionName.ACADEMIC_READ: "View academic structure (years, classes, subjects).",
    PermissionName.ACADEMIC_MANAGE: "Manage academic structure.",
    PermissionName.MARKS_READ: "View marks/results within scope.",
    PermissionName.MARKS_MANAGE: "Enter or correct marks within scope.",
    PermissionName.MARKS_PUBLISH: "Publish exam results.",
    PermissionName.ASSIGNMENT_READ: "View assignments within scope.",
    PermissionName.ASSIGNMENT_MANAGE: "Create assignments and review submissions within scope.",
    PermissionName.ASSIGNMENT_SUBMIT: "Submit own work for assignments.",
    PermissionName.TIMETABLE_READ: "View timetables within scope.",
    PermissionName.TIMETABLE_MANAGE: "Manage timetables.",
    PermissionName.ANNOUNCEMENT_READ: "View announcements.",
    PermissionName.ANNOUNCEMENT_MANAGE: "Create and publish announcements.",
    PermissionName.NOTIFICATION_READ: "View own notifications.",
    PermissionName.NOTIFICATION_MANAGE: "Send and manage notifications.",
    PermissionName.REPORT_READ: "View reports.",
    PermissionName.ANALYTICS_READ: "View analytics.",
    PermissionName.AI_READ: "View AI insights within scope.",
    PermissionName.AI_MANAGE: "Configure AI features.",
}

P = PermissionName

_AUTH_BASE = frozenset({P.AUTH_LOGIN, P.AUTH_REFRESH, P.AUTH_LOGOUT, P.AUTH_ME})

# Read access shared by every school persona (scope-limited by resource policies).
_PERSONA_READ = frozenset(
    {
        P.SCHOOL_READ,
        P.ACADEMIC_READ,
        P.ATTENDANCE_READ,
        P.MARKS_READ,
        P.ASSIGNMENT_READ,
        P.TIMETABLE_READ,
        P.ANNOUNCEMENT_READ,
        P.NOTIFICATION_READ,
    }
)

# Persona "self/linked/assigned" permissions are meaningless for administrators.
_PERSONA_ONLY = frozenset(
    {
        P.STUDENT_READ_SELF,
        P.PARENT_READ_SELF,
        P.PARENT_READ_CHILD,
        P.FACULTY_READ_SELF,
        P.FACULTY_READ_ASSIGNED_STUDENTS,
        P.ASSIGNMENT_SUBMIT,
    }
)

_ALL = frozenset(PermissionName)

ROLE_PERMISSIONS: dict[RoleName, frozenset[PermissionName]] = {
    RoleName.SUPER_ADMIN: _ALL - _PERSONA_ONLY,
    RoleName.SCHOOL_ADMIN: _ALL - _PERSONA_ONLY - {P.ROLE_MANAGE, P.PERMISSION_MANAGE},
    RoleName.MANAGEMENT: _AUTH_BASE
    | _PERSONA_READ
    | {
        P.USER_READ,
        P.ROLE_READ,
        P.STUDENT_READ,
        P.MARKS_PUBLISH,
        P.ANNOUNCEMENT_MANAGE,
        P.REPORT_READ,
        P.ANALYTICS_READ,
        P.AI_READ,
    },
    RoleName.FACULTY: _AUTH_BASE
    | _PERSONA_READ
    | {
        P.FACULTY_READ_SELF,
        P.FACULTY_READ_ASSIGNED_STUDENTS,
        P.ATTENDANCE_MANAGE,
        P.MARKS_MANAGE,
        P.ASSIGNMENT_MANAGE,
    },
    RoleName.PARENT: _AUTH_BASE | _PERSONA_READ | {P.PARENT_READ_SELF, P.PARENT_READ_CHILD},
    RoleName.STUDENT: _AUTH_BASE | _PERSONA_READ | {P.STUDENT_READ_SELF, P.ASSIGNMENT_SUBMIT},
}

# Which roles each role may grant or revoke (anti-privilege-escalation).
# Only SUPER_ADMIN can create or modify other SUPER_ADMINs.
ASSIGNABLE_ROLES: dict[RoleName, frozenset[RoleName]] = {
    RoleName.SUPER_ADMIN: frozenset(RoleName),
    RoleName.SCHOOL_ADMIN: frozenset(RoleName) - {RoleName.SUPER_ADMIN},
}
