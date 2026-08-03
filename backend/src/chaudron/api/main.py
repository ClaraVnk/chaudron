"""Application factory.

``create_app`` builds a fully wired application from a :class:`Settings` value
and nothing else -- no import-time singletons, no reading of the environment
half-way down a call stack. That is what lets the test suite build an app
against a throwaway database, and what makes "which configuration is this
process running?" a question with one answer.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import RequestResponseEndpoint

from chaudron.api.errors import register_exception_handlers
from chaudron.api.middleware import RequestSizeLimitMiddleware, SecurityHeadersMiddleware
from chaudron.api.routers import (
    health_router,
    inventory_router,
    locations_router,
    products_router,
    providers_router,
    recipes_router,
)
from chaudron.api.throttling import ConcurrencyLimiter, RateLimiter, Throttles
from chaudron.config import Settings, get_settings
from chaudron.infra.db import Database
from chaudron.infra.logging import configure_logging, household_id_var, request_id_var
from chaudron.infra.openfoodfacts import OpenFoodFactsCatalog

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

#: What an inbound ``X-Request-Id`` must look like to be worth writing to a log.
#: Deliberately narrow -- UUIDs and W3C trace identifiers both fit, and nothing
#: else can. The value is never returned to the client and never used as an
#: incident identifier, so this only has to keep the logs readable.
_UPSTREAM_REQUEST_ID: Final = re.compile(r"\A[0-9A-Za-z._-]{1,128}\Z")

#: The two windows the limits are expressed over. Fixed in code rather than
#: configurable: the *counts* are a product decision an operator may reasonably
#: retune, the units they are quoted in are not.
_RECIPE_WINDOW_SECONDS: Final = 3600.0
_PRODUCT_LOOKUP_WINDOW_SECONDS: Final = 60.0


def build_throttles(settings: Settings) -> Throttles:
    """The per-process limiters this application instance owns."""
    return Throttles(
        recipe_suggestions=RateLimiter(
            limit=settings.recipe_suggestions_per_hour,
            window_seconds=_RECIPE_WINDOW_SECONDS,
        ),
        recipe_inferences=ConcurrencyLimiter(
            per_key=settings.recipe_max_concurrent_per_household,
            total=settings.recipe_max_concurrent_total,
        ),
        product_lookups=RateLimiter(
            limit=settings.product_lookups_per_minute,
            window_seconds=_PRODUCT_LOOKUP_WINDOW_SECONDS,
        ),
    )


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
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
    )
    app.state.settings = resolved
    app.state.database = Database(resolved)
    app.state.catalog = OpenFoodFactsCatalog(resolved)
    app.state.throttles = build_throttles(resolved)

    # Middleware order matters and is not obvious: `add_middleware` *prepends*, so
    # the last call below is the outermost layer. Outer to inner, the stack ends up
    # as: request context, security headers, CORS, size limit. That is deliberate.
    # The size limit sits innermost so its 413 still passes back out through CORS
    # (a browser must be able to read it) and still collects a request identifier;
    # the security headers sit outside CORS so they reach preflight responses too.
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=resolved.max_request_body_bytes)

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

    app.add_middleware(SecurityHeadersMiddleware, production=resolved.is_production)

    @app.middleware("http")
    async def attach_request_context(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Give every request an identifier, and every log line that identifier.

        The identifier is **generated here, always**. It used to be whatever the
        client sent, which made it useless for the one job it has: an unauthenticated
        caller could give a million requests the same identifier to defeat aggregation,
        reuse an identifier seen in someone else's response to blend into their trail,
        or hand an investigator a plausible-looking value it had invented (audit
        AUD-014). An incident identifier a stranger chooses is evidence of nothing.

        An inbound value is not thrown away -- correlating with a proxy is a real
        need -- it is written to the log under a different name, once, after
        validation, and never reflected back to the client.
        """
        request_id = str(uuid.uuid4())
        request_token = request_id_var.set(request_id)
        household_token = household_id_var.set(None)
        incoming = request.headers.get(REQUEST_ID_HEADER)
        if incoming is not None and _UPSTREAM_REQUEST_ID.match(incoming):
            logger.info("upstream_request_id", extra={"upstream_request_id": incoming})
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
