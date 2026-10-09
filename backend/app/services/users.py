"""User account provisioning (used by the bootstrap CLI; HTTP user management comes later)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit.service import AuditAction, record_audit
from app.config import Settings
from app.core.errors import BusinessRuleError, ConflictError
from app.models.user import User, UserStatus
from app.repositories.rbac import RbacRepository
from app.repositories.school import SchoolRepository
from app.repositories.users import UserRepository
from app.security.identifiers import normalize_email, normalize_phone
from app.security.passwords import Argon2Params, hash_password, validate_password_policy
from app.security.permissions import RoleName


def create_user_account(
    session: Session,
    settings: Settings,
    *,
    full_name: str,
    roles: Sequence[RoleName],
    email: str | None = None,
    phone: str | None = None,
    password: str | None = None,
    school_id: uuid.UUID | None = None,
    created_by_id: uuid.UUID | None = None,
    source: str = "cli",
    require_contact: bool = True,
    extra_policy_inputs: tuple[str, ...] = (),
) -> User:
    """Create a user with roles. Validates identifiers and the password policy."""
    full_name = full_name.strip()
    if not full_name:
        raise BusinessRuleError("Full name is required.")
    normalized_email = normalize_email(email) if email else None
    if normalized_email is not None and "@" not in normalized_email:
        raise BusinessRuleError("Email address is invalid.")
    normalized_phone = None
    if phone:
        normalized_phone = normalize_phone(phone)
        if normalized_phone is None:
            raise BusinessRuleError("Phone must be in international format, e.g. +919876543210.")
    if require_contact and normalized_email is None and normalized_phone is None:
        raise BusinessRuleError("An email address or phone number is required.")
    if RoleName.SUPER_ADMIN not in roles and school_id is None:
        raise BusinessRuleError("School-level accounts require a school.")

    if school_id is not None:
        school = SchoolRepository(session).get(school_id)
        if school is None:
            raise BusinessRuleError("School not found.")
        if not school.is_active:
            raise BusinessRuleError("School is inactive.")

    users = UserRepository(session)
    if normalized_email and users.get_by_email(normalized_email):
        raise ConflictError("A user with this email already exists.")
    if normalized_phone and users.get_by_phone(normalized_phone):
        raise ConflictError("A user with this phone number already exists.")

    password_hash = None
    if password is not None:
        validate_password_policy(
            password,
            settings,
            user_inputs=(full_name, normalized_email, normalized_phone, *extra_policy_inputs),
        )
        password_hash = hash_password(password, Argon2Params.from_settings(settings))

    user = users.add(
        User(
            full_name=full_name,
            email=normalized_email,
            phone=normalized_phone,
            password_hash=password_hash,
            password_changed_at=datetime.now(UTC) if password_hash else None,
            school_id=school_id,
            status=UserStatus.ACTIVE.value,
        )
    )
    role_rows = RbacRepository(session).roles_by_names([role.value for role in roles])
    if len(role_rows) != len(set(roles)):
        raise BusinessRuleError("One or more roles do not exist. Run the migrations first.")
    users.replace_roles(user.id, role_rows, assigned_by_id=created_by_id)
    record_audit(
        session,
        AuditAction.USER_CREATED,
        user_id=created_by_id,
        resource_type="user",
        resource_id=user.id,
        details={"roles": sorted(role.value for role in roles), "source": source},
    )
    session.flush()
    return user
