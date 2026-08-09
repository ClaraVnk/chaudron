"""The retention sweep, against a real database.

``scripts/purge_retained_data.py`` removes two things nothing had ever removed:
e-mail addresses in ``rate_limit_bucket``, and the frozen household inventory in
``recipe_suggestion.stock_snapshot``.

The risk here is the fix, not the problem, and it is a different risk in each
half. On the snapshots it is deleting an explanation somebody is currently looking
at -- so the negative is asserted first and at length: a suggestion made this
morning keeps its snapshot, and a row already purged is not counted again as work.
On the buckets it is deleting one that has *not* refilled, which is not a cleanup
at all but a refund: on the sign-in limiter, a refund is a password spray with the
limit taken off. That one is refused rather than tested for, and the refusal is
what this file checks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from scripts.purge_retained_data import (
    DEFAULT_BUCKET_IDLE_HOURS,
    DEFAULT_SNAPSHOT_DAYS,
    MIN_BUCKET_IDLE_HOURS,
    count,
    purge,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    LlmProviderMode,
    RateLimitBucket,
    RecipeStatus,
    RecipeSuggestion,
)

pytestmark = pytest.mark.integration

_LONG_AGO = timedelta(days=DEFAULT_SNAPSHOT_DAYS + 1)
_THIS_MORNING = timedelta(hours=6)

_SNAPSHOT: dict[str, Any] = {"items": [{"name": "Crème fraîche épaisse", "quantity": "1"}]}


@pytest.fixture
async def household(db_session: AsyncSession) -> Household:
    row = Household(name="Rétention")
    db_session.add(row)
    await db_session.flush()
    return row


async def _suggestion(
    session: AsyncSession, household: Household, *, age: timedelta, snapshot: Any = _SNAPSHOT
) -> RecipeSuggestion:
    row = RecipeSuggestion(
        household_id=household.id,
        title="Gratin",
        payload={"steps": []},
        stock_snapshot=snapshot,
        provider_mode=LlmProviderMode.OLLAMA,
        provider_code="ollama",
        model="qwen2.5",
        prompt_version="v1",
        status=RecipeStatus.GENERATED,
    )
    session.add(row)
    await session.flush()
    # `created_at` is a server default, so it has to be moved after the insert --
    # and moved in the database, because the predicate compares against `now()`
    # there rather than against whatever Python thinks the time is.
    #
    # `make_interval` rather than a bound `timedelta`: an untyped parameter beside
    # `now()` lets PostgreSQL resolve the operator to `timestamptz - timestamptz`,
    # which returns an interval and fails against the column. The same spelling
    # `infra/rate_limits.py` uses, for the same reason.
    await session.execute(
        sa.text(
            "UPDATE recipe_suggestion SET created_at = now() - make_interval(secs => :secs)"
            " WHERE id = :id"
        ),
        {"secs": age.total_seconds(), "id": row.id},
    )
    return row


async def _snapshot_of(session: AsyncSession, suggestion_id: uuid.UUID) -> Any:
    """The column's value, read past the identity map.

    A **column** rather than the entity, deliberately: an ORM query returning a row
    already in the session hands back the instance it is holding without
    overwriting the attributes it has loaded, so ``row.stock_snapshot`` would
    report what this test inserted whatever the database now says.
    """
    return await session.scalar(
        select(RecipeSuggestion.stock_snapshot).where(RecipeSuggestion.id == suggestion_id)
    )


async def _is_sql_null(session: AsyncSession, suggestion_id: uuid.UUID) -> bool:
    """Whether the column is SQL ``NULL`` -- which JSON ``null`` is not.

    The distinction is not pedantry, it is the bug this file caught. Writing a bare
    ``None`` into a JSONB column stores the JSON scalar ``null``: the value comes
    back to Python as ``None`` and passes any ``is None`` assertion, while the
    column still satisfies ``IS NOT NULL`` in SQL -- so the retention job re-purged
    every row it had ever purged, for ever, and reported them as work each time.
    Asserted in the database's own terms, because Python's cannot tell the two
    apart.
    """
    return bool(
        await session.scalar(
            sa.text("SELECT stock_snapshot IS NULL FROM recipe_suggestion WHERE id = :id"),
            {"id": suggestion_id},
        )
    )


async def _add_bucket(session: AsyncSession, scope: str, key: str, *, idle: timedelta) -> None:
    session.add(
        RateLimitBucket(scope=scope, bucket_key=key, tokens=1.0, updated_at=datetime.now(UTC))
    )
    await session.flush()
    await session.execute(
        sa.text(
            "UPDATE rate_limit_bucket SET updated_at = now() - make_interval(secs => :secs)"
            " WHERE scope = :scope AND bucket_key = :key"
        ),
        {"secs": idle.total_seconds(), "scope": scope, "key": key},
    )


# --------------------------------------------------------------------------- #
# What must survive
# --------------------------------------------------------------------------- #


async def test_a_recent_snapshot_is_kept(db_session: AsyncSession, household: Household) -> None:
    """The retention window, which is the whole reason this is not a trigger.

    The snapshot is what answers "why did it suggest that when I had no eggs?", and
    the person asking is looking at a suggestion from this morning.
    """
    fresh = await _suggestion(db_session, household, age=_THIS_MORNING)

    await purge(await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS)

    assert await _snapshot_of(db_session, fresh.id) == _SNAPSHOT


async def test_the_suggestion_itself_is_never_deleted(
    db_session: AsyncSession, household: Household
) -> None:
    """Only the column goes, and that distinction is the design.

    The title, the steps, the rating and the cost are what make the feature
    reviewable and none of them is an inventory of anybody's cupboards. A retention
    job that deleted the row would take the evidence with the exposure.
    """
    stale = await _suggestion(db_session, household, age=_LONG_AGO)

    await purge(await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS)

    # `populate_existing`: the instance is already in this session's identity map,
    # and without it the ORM hands it back with the attributes it loaded rather than
    # the ones the database now holds -- so this would assert against the fixture.
    row = await db_session.scalar(
        select(RecipeSuggestion)
        .where(RecipeSuggestion.id == stale.id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    assert row.title == "Gratin"
    assert row.payload == {"steps": []}
    assert row.stock_snapshot is None
    assert await _is_sql_null(db_session, stale.id)


async def test_a_bucket_still_inside_its_window_is_left_alone(db_session: AsyncSession) -> None:
    """Deleting one that has not refilled is a refund, not a cleanup."""
    scope = f"test-retention-{uuid.uuid4().hex[:8]}"
    await _add_bucket(db_session, scope, "recent@example.test", idle=timedelta(minutes=5))

    await purge(await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS)

    remaining = await db_session.scalar(
        sa.text("SELECT count(*) FROM rate_limit_bucket WHERE scope = :scope"), {"scope": scope}
    )
    assert remaining == 1


async def test_a_cutoff_shorter_than_a_limiter_window_is_refused(
    db_session: AsyncSession,
) -> None:
    """The one setting that would turn this job into a rate-limit bypass.

    Refused rather than clamped: an operator who asks for a five-minute sweep has
    misunderstood what the number means, and quietly doing something else is how
    they stay misunderstanding it.
    """
    with pytest.raises(ValueError, match="widest limiter window"):
        await purge(await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, MIN_BUCKET_IDLE_HOURS / 2)


# --------------------------------------------------------------------------- #
# What goes
# --------------------------------------------------------------------------- #


async def test_an_old_snapshot_is_cleared_and_the_column_says_so(
    db_session: AsyncSession, household: Household
) -> None:
    """NULL, and not ``{}``.

    An empty object would satisfy the old ``NOT NULL`` and record a *fact* -- that
    the household had nothing in stock -- which the "why did it suggest that?"
    screen would read back as an answer. Revision ``0027`` argues it; this is what
    holds the job to it.
    """
    stale = await _suggestion(db_session, household, age=_LONG_AGO)

    snapshots, _ = await purge(
        await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS
    )

    assert snapshots >= 1
    assert await _snapshot_of(db_session, stale.id) is None
    assert await _is_sql_null(db_session, stale.id), "the column holds JSON null, not SQL NULL"


async def test_an_idle_bucket_holding_an_address_is_swept(db_session: AsyncSession) -> None:
    """The rows no cascade reaches, and the reason this job exists at all.

    ``bucket_key`` holds normalised e-mail addresses of accounts that may never have
    existed -- every address typed into the sign-in form by somebody guessing. No
    erasure can reach those, because there is no account to erase.
    """
    scope = f"test-retention-{uuid.uuid4().hex[:8]}"
    await _add_bucket(db_session, scope, "jamais-inscrit@example.test", idle=timedelta(days=2))

    _, buckets = await purge(
        await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS
    )

    assert buckets >= 1
    remaining = await db_session.scalar(
        sa.text("SELECT count(*) FROM rate_limit_bucket WHERE scope = :scope"), {"scope": scope}
    )
    assert remaining == 0


async def test_an_already_purged_snapshot_is_not_counted_again(
    db_session: AsyncSession, household: Household
) -> None:
    """``IS NOT NULL`` in the predicate, and it is not redundant.

    Without it every nightly run would rewrite every row it had ever cleared and
    report them as work done -- so the number in the journal, which is the only
    thing an operator reads, would grow for ever and mean nothing.
    """
    await _suggestion(db_session, household, age=_LONG_AGO)

    first, _ = await purge(
        await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS
    )
    second, _ = await purge(
        await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS
    )

    assert first >= 1
    assert second == 0


async def test_the_dry_run_counts_the_same_rows_and_changes_none(
    db_session: AsyncSession, household: Household
) -> None:
    """A first run should be ``--dry-run``, so the two must agree.

    A counter using a different predicate from the statement it predicts would make
    the dry run reassuring and wrong, which is worse than having no dry run at all.
    """
    scope = f"test-retention-{uuid.uuid4().hex[:8]}"
    stale = await _suggestion(db_session, household, age=_LONG_AGO)
    await _add_bucket(db_session, scope, "idle@example.test", idle=timedelta(days=2))

    predicted = await count(
        await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS
    )
    assert predicted[0] >= 1
    assert predicted[1] >= 1
    # Counting is not writing.
    assert await _snapshot_of(db_session, stale.id) == _SNAPSHOT

    recounted = await count(
        await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS
    )
    assert recounted == predicted

    assert (
        await purge(await db_session.connection(), DEFAULT_SNAPSHOT_DAYS, DEFAULT_BUCKET_IDLE_HOURS)
        == predicted
    )
