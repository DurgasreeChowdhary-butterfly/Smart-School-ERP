from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import DatabaseDep, SettingsDep
from app.schemas.common import ErrorResponse
from app.schemas.health import HealthResponse, HealthStatus, LivenessResponse
from app.services import health as health_service

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "",
    response_model=HealthResponse,
    summary="Application health including database connectivity",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthResponse,
            "description": "A dependency is unavailable",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def health(settings: SettingsDep, database: DatabaseDep, response: Response) -> HealthResponse:
    result = health_service.readiness(settings, database)
    response.headers["Cache-Control"] = "no-store"
    if result.status is not HealthStatus.OK:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@router.get(
    "/live",
    response_model=LivenessResponse,
    summary="Liveness probe (process is up; no dependency checks)",
)
def live(settings: SettingsDep, response: Response) -> LivenessResponse:
    response.headers["Cache-Control"] = "no-store"
    return health_service.liveness(settings)
