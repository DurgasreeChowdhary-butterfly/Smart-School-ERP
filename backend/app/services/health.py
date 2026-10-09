from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from app import __version__
from app.config import Settings
from app.database import Database
from app.schemas.health import ComponentHealth, HealthResponse, HealthStatus, LivenessResponse

logger = logging.getLogger(__name__)


def check_database(database: Database) -> ComponentHealth:
    start = time.perf_counter()
    try:
        database.ping()
        status = HealthStatus.OK
    except Exception as exc:  # noqa: BLE001 - any failure means "unavailable"
        # Details stay in the server log; clients only see the status.
        logger.warning("database health check failed", extra={"error": type(exc).__name__})
        status = HealthStatus.UNAVAILABLE
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return ComponentHealth(status=status, latency_ms=latency_ms)


def liveness(settings: Settings) -> LivenessResponse:
    return LivenessResponse(
        status=HealthStatus.OK,
        service=settings.app_name,
        version=__version__,
        timestamp=datetime.now(UTC),
    )


def readiness(settings: Settings, database: Database) -> HealthResponse:
    checks = {"database": check_database(database)}
    overall = (
        HealthStatus.OK
        if all(check.status is HealthStatus.OK for check in checks.values())
        else HealthStatus.UNAVAILABLE
    )
    return HealthResponse(
        status=overall,
        service=settings.app_name,
        version=__version__,
        timestamp=datetime.now(UTC),
        checks=checks,
    )
