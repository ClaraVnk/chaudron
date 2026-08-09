"""Liveness and readiness, deliberately separate.

``/healthz`` answers from the process alone. If it touched the database, a brief
outage would make the orchestrator kill a perfectly healthy container and turn a
degradation into a restart loop.

``/readyz`` does touch it, because "ready" means "able to serve", and an API
without its database can serve nothing. It also asks whether row-level security
is genuinely in force on the connection this process holds -- see
:meth:`chaudron.infra.db.Database.check_row_level_security`. An instance whose
DSN names the table owner rather than the application role passes every
functional test and isolates nothing, and that misconfiguration has no symptom a
request could reveal. A probe is the only thing that can see it.

Since this revision it asks a third question: **is the schema the one this build
was written against?** ``ops/README.md`` §1.6 had claimed "database reachable,
migrations applied" for as long as the endpoint has existed, and only the first
half was ever true -- an instance deployed ahead of its migration answered
``200 ready`` and then failed on the first request that touched a column the
migration had not yet added. See
:mod:`chaudron.infra.schema_revision` for why the answer has four values rather
than two, and in particular why a database *ahead* of the code is ready and one
*behind* it is not.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from chaudron.api.deps import get_database, get_settings_dep
from chaudron.api.schemas import HealthOut, ReadinessOut
from chaudron.config import Settings
from chaudron.infra.db import Database
from chaudron.infra.schema_revision import REQUIRED_SCHEMA_REVISION, classify_schema_revision

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
async def readyz(
    database: Annotated[Database, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> JSONResponse:
    """Check the database on a connection of its own.

    Not the request-scoped session: that dependency opens a transaction and
    would fail *before* this function runs, turning an honest 503 into a 500.

    The row-level security answer is reported in every environment and *refuses*
    readiness everywhere except ``local`` and ``ci``. Those two connect as the
    table owner on purpose -- a developer runs migrations and the API from one
    DSN, and ``tests/tenancy/conftest.py`` needs the owner to prove the
    provisioned role behaves differently -- so a 503 there would be a permanent
    red light nobody could act on, and a permanent red light is one people learn
    to ignore, including on the deployment where it means something.

    ``staging`` used to be on the permissive side of that line, because the test
    was ``is_production`` and ``is_production`` is ``env == "production"``
    exactly. An instance deployed there with the owner DSN isolated nothing
    between households and answered this probe ``200``, so a load balancer put it
    into service (audit AUD-029). ``Settings.requires_row_level_security`` is now
    what decides, and it names the two exemptions rather than the one refusal.

    The schema check has **no** such exemption, and deliberately so. The two
    environments excused above connect as the owner for a reason that is true of
    them and of nowhere else; there is no environment in which running this code
    against a schema it predates is intended. A developer who has not run
    ``alembic upgrade head`` wants to be told that, and being told it here costs
    one row of ``alembic_version``.
    """
    try:
        report = await database.check_row_level_security()
        revision = await database.current_schema_revision()
    except (SQLAlchemyError, OSError) as exc:
        # `OSError` too: a refused connection or an unresolvable host arrives from
        # asyncpg's socket rather than through the dialect, and a readiness probe
        # that answered 500 to "the database is down" would be reporting on itself.
        #
        # This deliberately does not claim anything about the migrations: a
        # database nobody can reach has told us nothing about its schema, and
        # reporting `migrations: unknown` alongside `database: unavailable` would
        # be two lines of alarm for one fault.
        logger.warning("readiness_check_failed", extra={"error": type(exc).__name__})
        return JSONResponse(
            ReadinessOut(status="degraded", checks={"database": "unavailable"}).model_dump(),
            status_code=503,
        )

    schema = classify_schema_revision(revision)
    checks = {
        "database": "ok",
        "row_level_security": "enforced" if report.is_enforced else "bypassed",
        "migrations": schema.value,
    }

    # Logged before the verdict, so both faults are on the record even when the
    # first of them is the one that decides the status code.
    if not report.is_enforced:
        # The reasons name roles and tables, so they go to the log the operator
        # reads and not to the body an unauthenticated caller reads.
        logger.error("row_level_security_not_enforced", extra={"problems": list(report.problems)})
    if not schema.is_serviceable:
        # The body carries the verdict; the two revision numbers stay here. Not
        # because a revision is a secret -- it is a build identifier -- but
        # because the body is read by an unauthenticated caller and the numbers
        # are only useful to whoever can also read the log and act on it.
        logger.error(
            "schema_revision_not_serviceable",
            extra={
                "applied": revision,
                "required": REQUIRED_SCHEMA_REVISION,
                "state": schema.value,
            },
        )

    rls_refuses = not report.is_enforced and settings.requires_row_level_security
    if rls_refuses or not schema.is_serviceable:
        return JSONResponse(
            ReadinessOut(status="degraded", checks=checks).model_dump(), status_code=503
        )
    return JSONResponse(ReadinessOut(status="ready", checks=checks).model_dump(), status_code=200)
