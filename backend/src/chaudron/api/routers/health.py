"""Liveness and readiness, deliberately separate.

``/healthz`` answers from the process alone. If it touched the database, a brief
outage would make the orchestrator kill a perfectly healthy container and turn a
degradation into a restart loop.

``/readyz`` does touch it, because "ready" means "able to serve", and an API
without its database can serve nothing.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from chaudron.api.deps import get_database
from chaudron.api.schemas import HealthOut, ReadinessOut
from chaudron.infra.db import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthOut, summary="Liveness probe")
async def healthz() -> HealthOut:
    return HealthOut()


@router.get(
    "/readyz",
    response_model=ReadinessOut,
    summary="Readiness probe",
    responses={503: {"model": ReadinessOut}},
)
async def readyz(database: Annotated[Database, Depends(get_database)]) -> JSONResponse:
    """Check the database on a connection of its own.

    Not the request-scoped session: that dependency opens a transaction and
    would fail *before* this function runs, turning an honest 503 into a 500.
    """
    try:
        async with database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("readiness_check_failed", extra={"error": type(exc).__name__})
        return JSONResponse(
            ReadinessOut(status="degraded", checks={"database": "unavailable"}).model_dump(),
            status_code=503,
        )
    return JSONResponse(
        ReadinessOut(status="ready", checks={"database": "ok"}).model_dump(), status_code=200
    )
