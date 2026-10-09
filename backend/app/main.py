"""FastAPI application factory and ASGI entrypoint (``app.main:app``)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.core.errors import REQUEST_ID_HEADER, register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.database import Database
from app.security.otp_delivery import build_otp_sender
from app.security.school_scope import SCHOOL_HEADER

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    database = Database(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "application starting",
            extra={"environment": settings.app_env.value, "version": __version__},
        )
        if settings.otp_delivery_backend == "development_console":
            logger.warning("OTP delivery uses the DEVELOPMENT STUB (codes are logged)")
        try:
            yield
        finally:
            database.dispose()
            logger.info("application stopped")

    docs_enabled = settings.api_docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
        openapi_url=f"{settings.api_v1_prefix}/openapi.json" if docs_enabled else None,
        docs_url=f"{settings.api_v1_prefix}/docs" if docs_enabled else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.otp_sender = build_otp_sender(settings.otp_delivery_backend, settings)

    register_exception_handlers(app)

    # Middleware added last runs first: request context wraps everything.
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER, SCHOOL_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
