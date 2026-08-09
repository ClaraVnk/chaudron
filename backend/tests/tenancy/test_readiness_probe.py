"""What ``/readyz`` refuses, and the two places that now act on its answer.

Two controls, tested together because one endpoint publishes both: row-level
security, below, and the schema revision, under "The schema revision" — the
latter added after an audit found ``ops/README.md`` §1.6 describing this probe as
"database reachable, migrations applied" while nothing in it looked at a
migration. Those tests point a real application at a real database stamped at the
wrong revision rather than at a stubbed classifier, because the query was the
part that did not exist.


``Database.check_row_level_security`` was written for a readiness probe and then
called by nobody, which made it a comment with a docstring. This file is what
makes it a control: it drives the probe against both roles the suite can provide
-- the table owner, which bypasses every policy, and the provisioned application
role, which does not -- and asserts that the difference reaches ``/readyz`` and,
in production, stops the process from starting.

Why it matters more than its size suggests: an instance whose
``CHAUDRON_DATABASE_URL`` names the owner answers every request correctly, passes
every functional test, and isolates nothing. Migration ``0004`` deliberately does
not set ``FORCE ROW LEVEL SECURITY``, so the owner is exempt from its own
policies. There is no request whose answer differs, which is precisely why the
only thing that can catch it is a probe that asks the catalogue directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from chaudron.api.main import (
    RowLevelSecurityNotEnforcedError,
    create_app,
    verify_row_level_security,
)
from chaudron.config import Settings
from chaudron.infra.db import Database
from chaudron.infra.schema_revision import REQUIRED_SCHEMA_REVISION
from tests.conftest import build_test_settings

pytestmark = pytest.mark.integration


def _settings(database_url: str, *, production: bool = False, env: str | None = None) -> Settings:
    base = build_test_settings(database_url)
    chosen = env if env is not None else ("production" if production else None)
    if chosen is None:
        return base
    # `https` and a non-DEBUG level are what the production validators demand;
    # neither is what this file is about. Applied to `staging` too, harmlessly.
    return base.model_copy(update={"env": chosen, "base_url": "https://chaudron.test"})


@asynccontextmanager
async def _database(database_url: str, *, production: bool = False) -> AsyncIterator[Database]:
    database = Database(_settings(database_url, production=production))
    try:
        yield database
    finally:
        await database.dispose()


@asynccontextmanager
async def _stamped_at(database_url: str, revision: str | None) -> AsyncIterator[None]:
    """Rewrite ``alembic_version`` for the body of the block, then put it back.

    The probe reads exactly one row of one table, so rewriting that row is the
    whole of "point it at a database at the wrong revision" -- and it is
    reversible, which downgrading the real schema is not. The suite shares one
    migrated database across the session (``initialised_database``), so the
    restore in ``finally`` is not tidiness: without it every later test in the
    run would see the tampered stamp.

    ``revision=None`` empties the table instead, which is one of the three ways
    :meth:`Database.current_schema_revision` reports ``None``.

    Connects as the **owner**: ``alembic_version`` belongs to it, and this is
    test scaffolding rather than something the application does.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            original = (
                await connection.scalars(text("SELECT version_num FROM alembic_version"))
            ).all()
            await connection.execute(text("DELETE FROM alembic_version"))
            if revision is not None:
                await connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )
        try:
            yield
        finally:
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM alembic_version"))
                for stamp in original:
                    await connection.execute(
                        text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                        {"revision": stamp},
                    )
    finally:
        await engine.dispose()


async def _readyz(settings: Settings) -> httpx.Response:
    """``/readyz`` on a real application built from *settings*.

    No dependency override: the point is the database this configuration would
    actually connect to, so the fixture session -- which is the owner's -- must
    not be substituted in.
    """
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/readyz")
    finally:
        await app.state.catalog.aclose()
        await app.state.database.dispose()


# --------------------------------------------------------------------------- #
# The probe itself
# --------------------------------------------------------------------------- #


async def test_the_owner_is_reported_as_bypassing_every_policy(
    initialised_database: str,
) -> None:
    """The misconfiguration with no symptom, given a symptom."""
    async with _database(initialised_database) as database:
        report = await database.check_row_level_security()

    assert not report.is_enforced
    assert report.problems
    assert any("bypass" in problem for problem in report.problems)


async def test_the_application_role_is_reported_as_subject_to_the_policies(
    app_role_url: str,
) -> None:
    """The other half: without it, a probe that always says "bypassed" would pass.

    ``scripts/provision_app_role.py`` produces this role, so a drift between the
    documented procedure and what the probe accepts fails here.
    """
    async with _database(app_role_url) as database:
        report = await database.check_row_level_security()

    assert report.is_enforced, report.problems
    assert report.problems == ()


# --------------------------------------------------------------------------- #
# /readyz
# --------------------------------------------------------------------------- #


async def test_readyz_reports_the_policies_as_in_force(app_role_url: str) -> None:
    response = await _readyz(_settings(app_role_url))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "row_level_security": "enforced", "migrations": "ok"},
    }


async def test_readyz_names_the_bypass_outside_production(initialised_database: str) -> None:
    """Reported, not refused: the suite and every developer connect as the owner.

    A 503 here would be a red light nobody could act on, and a red light nobody
    can act on is one people stop reading -- including on the deployment where it
    means something.
    """
    response = await _readyz(_settings(initialised_database))

    assert response.status_code == 200
    assert response.json()["checks"]["row_level_security"] == "bypassed"


async def test_readyz_refuses_readiness_in_production_when_the_policies_are_bypassed(
    initialised_database: str,
) -> None:
    response = await _readyz(_settings(initialised_database, production=True))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "row_level_security": "bypassed", "migrations": "ok"},
    }


async def test_readyz_refuses_readiness_in_staging_when_the_policies_are_bypassed(
    initialised_database: str,
) -> None:
    """The one this file did not assert, and the one that shipped (audit AUD-029).

    ``/readyz`` used to refuse only when ``env == "production"`` exactly, so a
    staging instance pointed at the owner DSN isolated nothing between households,
    reported ``row_level_security: bypassed`` in its own body, and answered
    ``200`` -- which is what a load balancer reads. The project already makes this
    argument for ``/docs`` two properties away in ``config.py``: *a staging
    instance carries real data far more often than anyone admits*. It had simply
    not been applied here.
    """
    response = await _readyz(_settings(initialised_database, env="staging"))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "row_level_security": "bypassed", "migrations": "ok"},
    }


async def test_readyz_is_ready_in_staging_when_the_policies_apply(app_role_url: str) -> None:
    """The other half: staging is refused for the bypass, not for being staging."""
    response = await _readyz(_settings(app_role_url, env="staging"))

    assert response.status_code == 200
    assert response.json()["checks"]["row_level_security"] == "enforced"


async def test_a_staging_instance_refuses_to_start_without_enforcement(
    initialised_database: str,
) -> None:
    """Startup takes the same decision as the probe, from the same property.

    Two places used to read ``is_production`` and they had to move together: an
    instance that booted and then never became ready would be a restart loop with
    extra steps, and one that became ready without booting is impossible.
    """
    settings = _settings(initialised_database, env="staging")
    async with _database(initialised_database) as database:
        with pytest.raises(RowLevelSecurityNotEnforcedError):
            await verify_row_level_security(settings, database)


@pytest.mark.parametrize("env", ["local", "ci"])
async def test_the_two_deliberate_environments_still_start_and_report(
    initialised_database: str, env: str
) -> None:
    """``local`` and ``ci`` connect as the owner on purpose and must not be stopped.

    A developer runs migrations and the API from one DSN; ``tests/tenancy`` needs
    the owner *and* the provisioned role side by side to prove the policies apply
    to one and not the other. A 503 there would be a permanent red light nobody
    could act on, and a permanent red light is one people stop reading.
    """
    settings = _settings(initialised_database, env=env)
    async with _database(initialised_database) as database:
        await verify_row_level_security(settings, database)

    response = await _readyz(settings)
    assert response.status_code == 200
    assert response.json()["checks"]["row_level_security"] == "bypassed"


async def test_readyz_says_nothing_about_roles_or_tables(initialised_database: str) -> None:
    """The reasons name the role and the tables; the body is read by strangers."""
    response = await _readyz(_settings(initialised_database, production=True))

    body = response.text
    assert "postgres" not in body
    assert "inventory_lot" not in body


# --------------------------------------------------------------------------- #
# The schema revision
# --------------------------------------------------------------------------- #
#
# `ops/README.md` §1.6 described `/readyz` as "database reachable, migrations
# applied" from the day the endpoint existed, and only the first half was true.
# These exercise the second half against a real database whose `alembic_version`
# says something other than what this build needs -- not against a stubbed
# classifier, because the thing that was missing was the query.


async def test_the_migrated_database_reports_the_revision_this_build_needs(
    initialised_database: str,
) -> None:
    """The floor under every assertion below: the fixture really is at head."""
    async with _database(initialised_database) as database:
        assert await database.current_schema_revision() == REQUIRED_SCHEMA_REVISION


async def test_the_application_role_can_read_the_stamp(app_role_url: str) -> None:
    """Not a formality. The probe runs as the *application* role in production.

    `provision_app_role.py` grants `SELECT ... ON ALL TABLES IN SCHEMA public`,
    which covers `alembic_version` because §6.1 applies the migration before
    creating the role. If that ordering ever changed, this check would answer
    `unknown` on every poll and no production instance would ever become ready --
    a fail-closed that takes the whole deployment with it. So it is asserted.
    """
    async with _database(app_role_url) as database:
        assert await database.current_schema_revision() == REQUIRED_SCHEMA_REVISION


async def test_readyz_refuses_a_schema_behind_this_build(app_role_url: str) -> None:
    """New code, old schema -- the deploy that used to answer `200 ready`.

    Stamped at the first revision in the chain, which is as far behind as this
    database can be while still having been migrated at all.
    """
    async with _stamped_at(app_role_url, "0001"):
        response = await _readyz(_settings(app_role_url))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "database": "ok",
            "row_level_security": "enforced",
            "migrations": "outdated",
        },
    }


async def test_readyz_serves_a_schema_ahead_of_this_build(app_role_url: str) -> None:
    """The expand phase of §5.4, which is a supported state and not a fault.

    The migration has landed and this process has not been replaced yet. Refusing
    here would take a healthy instance out of service in the middle of the
    procedure the runbook prescribes.
    """
    async with _stamped_at(app_role_url, "9999_not_yet_written"):
        response = await _readyz(_settings(app_role_url))

    assert response.status_code == 200
    assert response.json()["checks"]["migrations"] == "ahead"


async def test_readyz_refuses_a_database_that_was_never_migrated(app_role_url: str) -> None:
    """ "Cannot tell" fails closed, and says something different from "outdated".

    The two send an operator to different steps: one has not run the migration at
    all, the other deployed ahead of it.
    """
    async with _stamped_at(app_role_url, None):
        response = await _readyz(_settings(app_role_url))

    assert response.status_code == 503
    assert response.json()["checks"]["migrations"] == "unknown"


async def test_readyz_refuses_a_branched_history(app_role_url: str) -> None:
    """Two stamped heads is not an answer, so it is not treated as one."""
    engine = create_async_engine(app_role_url, poolclass=NullPool)
    try:
        async with _stamped_at(app_role_url, REQUIRED_SCHEMA_REVISION):
            async with _database(app_role_url) as database:
                # A second head, added on top of the one `_stamped_at` installed.
                # Restored with the rest when the block exits.
                async with database.engine.begin() as connection:
                    await connection.execute(
                        text("INSERT INTO alembic_version (version_num) VALUES ('0001')")
                    )
                assert await database.current_schema_revision() is None
            response = await _readyz(_settings(app_role_url))
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json()["checks"]["migrations"] == "unknown"


async def test_a_stale_schema_is_refused_even_where_the_bypass_is_excused(
    initialised_database: str,
) -> None:
    """`local` and `ci` are excused the RLS bypass. They are not excused this.

    The two exemptions exist because those environments connect as the table
    owner *on purpose*. Nothing makes running this code against a schema it
    predates intentional anywhere, so the schema check has no exemption list --
    and a developer who has not run `alembic upgrade head` is told so.
    """
    async with _stamped_at(initialised_database, "0001"):
        response = await _readyz(_settings(initialised_database, env="local"))

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "database": "ok",
            "row_level_security": "bypassed",
            "migrations": "outdated",
        },
    }


async def test_readyz_does_not_claim_a_schema_verdict_when_the_database_is_unreachable() -> None:
    """One fault, one line. `database: unavailable` says all there is to say."""
    settings = build_test_settings("postgresql+asyncpg://nobody@127.0.0.1:1/none").model_copy(
        update={"database_url": SecretStr("postgresql+asyncpg://nobody@127.0.0.1:1/none")}
    )
    response = await _readyz(settings)

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "checks": {"database": "unavailable"}}


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


async def test_a_production_instance_refuses_to_start_without_enforcement(
    initialised_database: str,
) -> None:
    settings = _settings(initialised_database, production=True)
    async with _database(initialised_database, production=True) as database:
        with pytest.raises(RowLevelSecurityNotEnforcedError) as raised:
            await verify_row_level_security(settings, database)

    assert "CHAUDRON_DATABASE_URL" in str(raised.value)


async def test_a_production_instance_starts_when_the_policies_apply(app_role_url: str) -> None:
    settings = _settings(app_role_url, production=True)
    async with _database(app_role_url, production=True) as database:
        await verify_row_level_security(settings, database)


async def test_a_non_production_instance_starts_and_says_so(initialised_database: str) -> None:
    """A developer connecting as the owner is not stopped from working."""
    settings = _settings(initialised_database)
    async with _database(initialised_database) as database:
        await verify_row_level_security(settings, database)


async def test_a_database_that_cannot_be_reached_does_not_stop_a_production_start() -> None:
    """ "Unable to verify" and "verified insecure" are different facts.

    Refusing to boot on the first turns a database thirty seconds late into a
    restart loop. The instance still never becomes *ready*, because ``/readyz``
    runs the same probe on every poll.
    """
    settings = build_test_settings("postgresql+asyncpg://nobody@127.0.0.1:1/none").model_copy(
        update={
            "env": "production",
            "base_url": "https://chaudron.test",
            "database_url": SecretStr("postgresql+asyncpg://nobody@127.0.0.1:1/none"),
        }
    )
    database = Database(settings)
    try:
        await verify_row_level_security(settings, database)
    finally:
        await database.dispose()
