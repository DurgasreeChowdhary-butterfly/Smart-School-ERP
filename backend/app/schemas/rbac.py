from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security.permissions import RoleName


class RoleRead(BaseModel):
    name: str
    description: str
    permissions: list[str]


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str


class UserRolesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[RoleName] = Field(max_length=len(RoleName))

    @field_validator("roles")
    @classmethod
    def _unique(cls, value: list[RoleName]) -> list[RoleName]:
        if len(set(value)) != len(value):
            raise ValueError("Roles must be unique.")
        return value


class UserRolesRead(BaseModel):
    user_id: uuid.UUID
    roles: list[str]
