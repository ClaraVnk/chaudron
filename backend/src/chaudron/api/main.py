"""Application factory.

``create_app`` builds a fully wired application from a :class:`Settings` value
and nothing else -- no import-time singletons, no reading of the environment
half-way down a call stack. That is what lets the test suite build an app
against a throwaway database, and what makes "which configuration is this
process running?" a question with one answer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import RequestResponseEndpoint

from chaudron.api.errors import register_exception_handlers
from chaudron.api.routers import (
    health_router,
    inventory_router,
    locations_router,
    products_router,
    providers_router,
    recipes_router,
)
from chaudron.config import Settings, get_settings
from chaudron.infra.db import Database
from chaudron.infra.logging import configure_logging, household_id_var, request_id_var
from chaudron.infra.openfoodfacts import OpenFoodFactsCatalog

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Release the outbound resources the factory acquired.

    Nothing is *created* here. A dependency built in the lifespan is absent from
    an app driven by an ASGI transport that does not run it -- which is how the
    test client works, and how half the "works in production, None in tests"
    bugs start.
    """
    settings: Settings = app.state.settings
    logger.info("chaudron_started", extra={"env": settings.env, "version": app.version})
    try:
        yield
    finally:
        catalog: OpenFoodFactsCatalog = app.state.catalog
        await catalog.aclose()
        database: Database = app.state.database
        await database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Raises before the first request if misconfigured."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved.log_level)

    app = FastAPI(
        title="Chaudron",
        version="0.1.0",
        summary="Household food stock management.",
        lifespan=_lifespan,
        # Docs are a development affordance, not a production endpoint: they
        # describe every route and parameter to anyone who finds the host.
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
    )
    app.state.settings = resolved
    app.state.database = Database(resolved)
    app.state.catalog = OpenFoodFactsCatalog(resolved)

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=resolved.cors_allow_credentials,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Household-Id"],
            expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
            max_age=600,
        )

    @app.middleware("http")
    async def attach_request_context(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Give every request an identifier, and every log line that identifier.

        Accepting an inbound ``X-Request-Id`` lets a trace span the proxy and the
        application; generating one when it is absent means there is always
        something to quote in a support conversation.
        """
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and len(incoming) <= 200 else str(uuid.uuid4())
        request_token = request_id_var.set(request_id)
        household_token = household_id_var.set(None)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(request_token)
            household_id_var.reset(household_token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(locations_router)
    app.include_router(inventory_router)
    app.include_router(products_router)
    app.include_router(providers_router)
    app.include_router(recipes_router)
    return app


def __getattr__(name: str) -> Any:
    """Expose ``chaudron.api.main:app`` without building it at import time.

    The container entrypoint asks for that attribute, so it has to exist. Binding
    it eagerly would read the environment whenever *anything* imports this module
    -- including the test suite, which builds its own app and has no reason to
    need a valid production configuration to do so.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
