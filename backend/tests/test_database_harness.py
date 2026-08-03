"""Tests of the test harness itself.

An unexercised fixture rots quietly: the first developer to write a real database
test discovers the container wiring never worked, and blames their own code. These
two tests are cheap and keep the harness honest.

The engine assertion is not ceremony either. "SQLite just for the tests" is the
compromise ADR-0003 rejects by name, and it is the kind of thing that gets
reintroduced to make a red build green on a Friday. Here it fails loudly.
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pantry.domain.models import Household
from tests.conftest import TenantPair


async def test_fixtures_run_against_postgresql_16(db_session: AsyncSession) -> None:
    assert db_session.bind is not None
    assert db_session.bind.dialect.name == "postgresql", (
        "the suite must exercise the production engine; there is no SQLite mode (ADR-0003)"
    )

    version = await db_session.scalar(text("SHOW server_version_num"))
    assert version is not None
    assert int(str(version)) >= 160000, f"expected PostgreSQL 16 or later, got {version}"

    # The trigram index on product.name does not exist without it, so a schema built
    # without the extension is not the schema production runs.
    extension = await db_session.scalar(
        text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    )
    assert extension == 1


async def test_each_test_starts_from_an_empty_database(
    db_session: AsyncSession, tenant_pair: TenantPair
) -> None:
    """Two households exist here, and the next test must not see either of them.

    ``commit()`` is called on purpose: with ``join_transaction_mode="create_savepoint"``
    it releases a savepoint rather than ending the outer transaction, so code under
    test may commit freely and still leave nothing behind.
    """
    await db_session.commit()

    households = await db_session.scalars(select(Household.id))
    assert set(households) == {tenant_pair.household_a.id, tenant_pair.household_b.id}


async def test_previous_test_left_nothing_behind(db_session: AsyncSession) -> None:
    # Depends on execution order only in the direction that matters: if the rollback
    # in `db_session` ever stops working, this is where it shows up.
    assert await db_session.scalar(select(func.count()).select_from(Household)) == 0
