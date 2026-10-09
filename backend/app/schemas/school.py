from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from app.schemas.fields import (
    Code,
    Email,
    HttpsUrl,
    PatchInput,
    Phone,
    PostalCode,
    StrictInput,
    Text100,
    Text200,
    Text500,
    Timezone,
)


class SchoolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    phone: str | None
    email: str | None
    timezone: str
    logo_url: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SchoolCreate(StrictInput):
    """Used by the platform CLI; schools are not created over the HTTP API."""

    name: Text200
    code: Code
    address: Text500 | None = None
    city: Text100 | None = None
    state: Text100 | None = None
    postal_code: PostalCode | None = None
    country: Text100 | None = None
    phone: Phone | None = None
    email: Email | None = None
    timezone: Timezone = "UTC"
    logo_url: HttpsUrl | None = None


class SchoolUpdate(PatchInput):
    """``code`` and ``id`` are immutable and therefore not accepted here."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset(
        {"address", "city", "state", "postal_code", "country", "phone", "email", "logo_url"}
    )

    name: Text200 | None = None
    address: Text500 | None = None
    city: Text100 | None = None
    state: Text100 | None = None
    postal_code: PostalCode | None = None
    country: Text100 | None = None
    phone: Phone | None = None
    email: Email | None = None
    timezone: Timezone | None = None
    logo_url: HttpsUrl | None = None
    # Only SUPER_ADMIN may change this (enforced by the service).
    is_active: bool | None = None
