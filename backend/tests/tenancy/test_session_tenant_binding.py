"""How the tenant reaches PostgreSQL, and that it never outlives its transaction.

``docs/data-model.md`` section 5.3 gives one reason for having deferred row-level
security: a pooled connection that keeps the previous request's household is a
leak *in the other direction*, silent and intermittent, and worse than having no
policy at all. That objection is answered by measurement here rather than by
argument.

The measurement matters because the reassuring answer is only true for the exact
mechanism used. ``SET LOCAL`` dies at ``COMMIT``; a plain ``SET`` does not, and
the two differ by one boolean in ``set_config``. The last test posts one of each
on the same backend and shows the difference, so the day somebody "simplifies"
that argument away, a test fails instead of an audit.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from chaudron.domain.models import Base, StorageKind, StorageLocation
from chaudron.infra.db import (
    TENANT_SETTING,
    Database,
    TenantScopeError,
    current_transaction_household,
    set_transaction_household,
)
from chaudron.infra.logging import household_id_var
from tests.conftest import build_test_settings
from tests.tenancy.conftest import TenantRows

pytestmark = pytest.mark.integration

_LOCATIONS = Base.metadata.tables["storage_location"]


def _database(url: str) -> Database:
    settings = build_test_settings(url).model_copy(update={"db_pool_size": 1, "db_max_overflow": 0})
    return Database(settings)


# --------------------------------------------------------------------------- #
# The explicit path
# --------------------------------------------------------------------------- #


async def test_session_posts_the_tenant_it_was_opened_with(
    app_role_url: str, tenant_rows: TenantRows
) -> None:
    database = _database(app_role_url)
    try:
        async with database.session(household_id=tenant_rows.household_a) as session:
            assert await current_transaction_household(session) == tenant_rows.household_a
            rows = (await session.execute(sa.select(_LOCATIONS))).all()
        assert len(rows) == 1
    finally:
        await database.dispose()


async def test_a_transaction_serves_one_household_only(
    app_role_url: str, tenant_rows: TenantRows
) -> None:
    """Rebinding mid-transaction is refused rather than silently honoured.

    A second household appearing inside one transaction means either a session
    shared between two requests or a household resolved twice with different
    answers. Overwriting would make both of those invisible.
    """
    database = _database(app_role_url)
    try:
        async with database.session(household_id=tenant_rows.household_a) as session:
            with pytest.raises(TenantScopeError):
                await set_transaction_household(session, tenant_rows.household_b)
    finally:
        await database.dispose()


# --------------------------------------------------------------------------- #
# The request-context path
# --------------------------------------------------------------------------- #


async def test_tenant_resolved_after_the_session_opened_is_still_posted(
    app_role_url: str, tenant_rows: TenantRows
) -> None:
    """The ordering the API actually has, reproduced exactly.

    ``api.deps.get_session`` opens the transaction; ``get_household_id`` resolves
    the household afterwards, and only then sets ``household_id_var``. Nothing at
    the point the transaction begins knows the tenant, so the listeners in
    ``infra.db`` post it at the first statement issued once it is known.
    """
    database = _database(app_role_url)
    token = household_id_var.set(None)
    try:
        async with database.session() as session:
            household_id_var.set(str(tenant_rows.household_a))
            rows = (await session.execute(sa.select(_LOCATIONS))).all()
            assert len(rows) == 1
            assert await current_transaction_household(session) == tenant_rows.household_a
    finally:
        household_id_var.reset(token)
        await database.dispose()


async def test_a_flush_with_no_prior_read_still_posts_the_tenant(
    app_role_url: str, tenant_rows: TenantRows
) -> None:
    """``session.add(...)`` then commit, with nothing read in between.

    The path a write-only handler takes. Without the ``before_flush`` listener the
    ``INSERT`` reaches ``WITH CHECK`` unscoped and fails -- loud rather than leaky,
    but broken.
    """
    database = _database(app_role_url)
    token = household_id_var.set(str(tenant_rows.household_a))
    try:
        async with database.session() as session:
            session.add(
                StorageLocation(
                    household_id=tenant_rows.household_a,
                    name=f"rls-{uuid.uuid7().hex[:8]}",
                    kind=StorageKind.OTHER,
                )
            )
            await session.flush()
            assert await current_transaction_household(session) == tenant_rows.household_a
            await session.rollback()
    finally:
        household_id_var.reset(token)
        await database.dispose()


# --------------------------------------------------------------------------- #
# The pool
# --------------------------------------------------------------------------- #


async def test_the_tenant_does_not_survive_into_the_next_transaction(
    app_role_url: str, tenant_rows: TenantRows
) -> None:
    """Borrow a connection, post a household, commit, borrow again: nothing left.

    ``pool_size=1`` and no overflow, so the second transaction provably lands on
    the same backend -- the assertion on ``pg_backend_pid()`` is what makes the
    empty result mean something. Without it the test would pass on a fresh
    connection and prove nothing.
    """
    database = _database(app_role_url)
    try:
        async with database.session(household_id=tenant_rows.household_a) as first:
            first_backend = await first.scalar(sa.select(sa.func.pg_backend_pid()))
            assert await current_transaction_household(first) == tenant_rows.household_a

        async with database.session() as second:
            second_backend = await second.scalar(sa.select(sa.func.pg_backend_pid()))
            leaked = await current_transaction_household(second)
            visible = (await second.execute(sa.select(_LOCATIONS))).all()

        assert second_backend == first_backend, "the pool handed out a different backend"
        assert leaked is None, f"the previous household leaked into a recycled connection: {leaked}"
        assert visible == [], "a recycled connection could still read the previous household"
    finally:
        await database.dispose()


async def test_a_session_level_set_would_have_leaked(app_engine: AsyncEngine) -> None:
    """The negative control: why ``is_local`` is not a detail.

    Same connection, same pool, one boolean apart. This is the failure the
    deferral argument in ``docs/data-model.md`` section 5.3 was built on, and it
    is real -- it is simply not what ``set_transaction_household`` does.
    """
    planted = str(uuid.uuid7())
    async with app_engine.begin() as connection:
        first_backend = await connection.scalar(sa.select(sa.func.pg_backend_pid()))
        await connection.execute(sa.select(sa.func.set_config(TENANT_SETTING, planted, False)))

    async with app_engine.begin() as connection:
        second_backend = await connection.scalar(sa.select(sa.func.pg_backend_pid()))
        survived = await connection.scalar(
            sa.select(sa.func.nullif(sa.func.current_setting(TENANT_SETTING, True), ""))
        )
        # Leave the connection clean for whatever borrows it next.
        await connection.execute(sa.select(sa.func.set_config(TENANT_SETTING, "", False)))

    assert second_backend == first_backend
    assert survived == planted, (
        "a session-level SET no longer survives a commit; if PostgreSQL changed "
        "this, the comment defending SET LOCAL needs rewriting, not deleting"
    )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


async def test_check_reports_the_owner_as_unenforced(initialised_database: str) -> None:
    """The misconfiguration with no symptom: the application connecting as owner.

    Every functional test passes, every endpoint answers, and no policy applies.
    ``Database.check_row_level_security`` is the only thing that would notice, so
    it is the thing that has to be right.
    """
    database = _database(initialised_database)
    try:
        report = await database.check_row_level_security()
        assert not report.is_enforced
        assert any("owns" in problem or "superuser" in problem for problem in report.problems)
    finally:
        await database.dispose()


async def test_check_reports_the_application_role_as_enforced(app_role_url: str) -> None:
    database = _database(app_role_url)
    try:
        report = await database.check_row_level_security()
        assert report.is_enforced, report.problems
        assert report.tables_without_policies == ()
    finally:
        await database.dispose()
