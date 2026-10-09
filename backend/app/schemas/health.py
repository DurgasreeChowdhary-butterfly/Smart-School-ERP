from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class ComponentHealth(BaseModel):
    status: HealthStatus
    latency_ms: float = Field(ge=0)


class LivenessResponse(BaseModel):
    status: HealthStatus
    service: str
    version: str
    timestamp: datetime


class HealthResponse(LivenessResponse):
    checks: dict[str, ComponentHealth]
