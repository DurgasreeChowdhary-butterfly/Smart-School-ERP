"""Shared response envelopes used across the API.

Conventions:
* A single resource is returned as the resource object itself.
* Collections are returned as :class:`Page` (``items`` + pagination metadata).
* Every error is returned as :class:`ErrorResponse`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    loc: list[str | int] | None = Field(
        default=None, description="Location of the offending field, if applicable."
    )
    message: str
    type: str | None = None


class ErrorBody(BaseModel):
    code: str = Field(description="Stable, machine-readable error code.")
    message: str = Field(description="Human-readable summary, safe to display.")
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class Page[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    pages: int = Field(ge=0)

    @classmethod
    def create(cls, items: Sequence[T], *, total: int, page: int, page_size: int) -> Page[T]:
        return cls(
            items=list(items),
            total=total,
            page=page,
            page_size=page_size,
            pages=math.ceil(total / page_size) if total else 0,
        )
