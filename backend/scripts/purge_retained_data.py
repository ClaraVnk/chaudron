"""Drop the two things this database holds longer than it has any reason to.

A sibling of ``scripts/purge_expired_credentials.py``, deliberately not a third
sweep inside it, and the reason is **cadence**. That job removes rows thirty days
after they stop authenticating anybody, so it runs weekly and could run monthly.
The two sweeps here have useful lives measured in hours, and a weekly run would
leave a personal e-mail address in ``rate_limit_bucket`` for six days longer than
anything needs it. One job cannot have two cadences; two jobs of the same shape
can, and the shape -- one script, one quadlet, one timer, the owner DSN, two
statements in one transaction -- is copied rather than reinvented.

``rate_limit_bucket``
---------------------

Revision ``0018`` moved the rate limiters into PostgreSQL and its own index
comment stated the consequence: *"Without the sweep the table grows one row per
distinct client address, for ever."* The sweep was written, and until now nothing
in the entire repository called it except the test that proved it worked -- no
timer, no route, no script.

Growth is the smaller half. ``bucket_key`` holds *"a household id, a client
address, or a normalised e-mail"* (``domain/models.py``), so the table accumulates
**e-mail addresses of people who have never had an account here** -- every address
typed into the sign-in form, every address a stranger asked a password-reset link
for. Those rows carry no ``household_id`` by design, so no cascade reaches them and
no erasure can: ``services/privacy.py`` deletes the ones belonging to an account
being erased, and this is what bounds all the rest.

Deleting a bucket is safe by arithmetic rather than by heuristic: one untouched for
a full window has refilled to capacity, so deleting it and re-creating it full are
the same thing. That argument holds only while the cutoff is at least as wide as
the widest policy, which is why :data:`MIN_BUCKET_IDLE_HOURS` exists and why
:func:`~chaudron.infra.rate_limits.sweep_idle_buckets` refuses anything shorter.

``recipe_suggestion.stock_snapshot``
------------------------------------

``docs/security-model.md`` section 8.2 names this the item that must carry *"the
shortest retention period in the system"*: it is a household's complete inventory,
frozen, in JSONB, and it was sent to a model provider. It has had **no** retention
at all -- the column was ``NOT NULL``, so there was no way to stop holding it short
of deleting the suggestion, and deleting the suggestion loses the title, the steps
and the rating that make the feature reviewable and that name nobody's cupboard.
Revision ``0027`` makes the column nullable so this job can clear it.

**The period is not decided here, and this script does not pretend otherwise.**
Section 8.4's retention table is explicitly *"proposals to be arbitrated, not
decisions"*, and arbitrating it is the operator's, not this file's. What is not
arbitrable is that "for ever" is the wrong answer for this column, so the default
is the number that table already proposes -- thirty days -- and ``--snapshot-days``
moves it. A default taken from the project's own written proposal is the smallest
invention available; picking a different number would have been a policy smuggled
in as a constant.

Run from ``backend/``, against the **owner** DSN::

    uv run python scripts/purge_retained_data.py --dry-run
    uv run python scripts/purge_retained_data.py

The owner, and not for convenience. ``recipe_suggestion`` is under row-level
security keyed on a transaction-local tenant (revision ``0004``), and a retention
sweep is by definition not scoped to one household: the application role would post
no tenant, match no row, and report that it cleared nothing -- silently, with an
exit code of 0. Same argument, same identity and same quadlet shape as
``purge_expired_credentials.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, and_, func, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from chaudron.config import ConfigurationError, get_settings
from chaudron.domain.models import RecipeSuggestion
from chaudron.infra.rate_limits import (
    MAX_POLICY_WINDOW_SECONDS,
    count_idle_buckets,
    sweep_idle_buckets,
)

logger = logging.getLogger("chaudron.purge")

_SECONDS_PER_HOUR: Final = 3600.0

#: How long a household's frozen inventory is kept. Thirty days is what
#: ``docs/security-model.md`` section 8.4 proposes for this column, and the module
#: docstring argues why the default is that number rather than a better one
#: somebody here invented.
DEFAULT_SNAPSHOT_DAYS: Final = 30

#: How long a rate-limit bucket is left alone before it is swept. Twenty-four
#: hours, against a widest window of one: the extra twenty-three buy nothing except
#: the certainty that no clock skew, no long-running transaction and no future
#: limiter with a slightly wider window can make the sweep refund a budget.
DEFAULT_BUCKET_IDLE_HOURS: Final = 24

#: The floor the flag will accept, in hours, derived from the limiters themselves
#: rather than written down twice. Below it a swept bucket has not refilled, and
#: deleting it hands its key a fresh full budget -- on the sign-in limiter, that is
#: an unlimited password spray, which is what the shared buckets exist to prevent.
MIN_BUCKET_IDLE_HOURS: Final = MAX_POLICY_WINDOW_SECONDS / _SECONDS_PER_HOUR


def _stale_snapshots(retention_days: int) -> ColumnElement[bool]:
    """Suggestions whose snapshot has been held past *retention_days*.

    ``IS NOT NULL`` is in the predicate, not just implied by it: without it every
    run would rewrite every already-purged row, report them as work done, and make
    the number in the journal meaningless. With it, the statement matches exactly
    the partial index revision ``0027`` created for it.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    return and_(RecipeSuggestion.stock_snapshot.is_not(None), RecipeSuggestion.created_at < cutoff)


async def count(
    connection: AsyncConnection, snapshot_days: int, idle_hours: float
) -> tuple[int, int]:
    """How many rows a run would touch, without touching any."""
    snapshots = await connection.scalar(
        select(func.count()).select_from(RecipeSuggestion).where(_stale_snapshots(snapshot_days))
    )
    buckets = await count_idle_buckets(
        connection, older_than_seconds=idle_hours * _SECONDS_PER_HOUR
    )
    return int(snapshots or 0), buckets


async def purge(
    connection: AsyncConnection, snapshot_days: int, idle_hours: float
) -> tuple[int, int]:
    """Clear them, and report what went."""
    # `null()` and not `None`. On a JSON/JSONB column SQLAlchemy renders a bare
    # `None` as the *JSON* value `null` -- a scalar that is emphatically not SQL
    # NULL. The column would then still satisfy `IS NOT NULL`, so every run would
    # re-purge every row it had ever purged and report them as work; the retained
    # snapshot would be gone, which is the part that matters, but the job would have
    # been lying about what it did ever since. `tests/test_retention_purge.py`
    # asserts `IS NULL` in SQL rather than `is None` in Python for the same reason:
    # a JSON null decodes to `None` and passes the weaker assertion.
    snapshots = await connection.execute(
        update(RecipeSuggestion)
        .where(_stale_snapshots(snapshot_days))
        .values(stock_snapshot=null())
    )
    buckets = await sweep_idle_buckets(
        connection, older_than_seconds=idle_hours * _SECONDS_PER_HOUR
    )
    return snapshots.rowcount, buckets


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-days",
        type=int,
        default=DEFAULT_SNAPSHOT_DAYS,
        help=(
            "how long recipe_suggestion.stock_snapshot -- a household's complete "
            f"inventory -- is kept (default {DEFAULT_SNAPSHOT_DAYS}, the period "
            "security-model.md 8.4 proposes). Below 1 is refused: a zero-day "
            "retention would clear the snapshot of a suggestion made this morning, "
            "which is the one the household is looking at."
        ),
    )
    parser.add_argument(
        "--bucket-idle-hours",
        type=float,
        default=DEFAULT_BUCKET_IDLE_HOURS,
        help=(
            "how long a rate-limit bucket is left alone before it is swept "
            f"(default {DEFAULT_BUCKET_IDLE_HOURS}). Below "
            f"{MIN_BUCKET_IDLE_HOURS:g} is refused, because a bucket that has not "
            "refilled to capacity is not one whose deletion is free."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count and report; change nothing. What a first run should be.",
    )
    arguments = parser.parse_args()

    if arguments.snapshot_days < 1:
        logger.error("--snapshot-days must be at least 1")
        return 2
    if arguments.bucket_idle_hours < MIN_BUCKET_IDLE_HOURS:
        logger.error("--bucket-idle-hours must be at least %g", MIN_BUCKET_IDLE_HOURS)
        return 2

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        if arguments.dry_run:
            async with engine.connect() as connection:
                snapshots, buckets = await count(
                    connection, arguments.snapshot_days, arguments.bucket_idle_hours
                )
            logger.info(
                "would clear %d stock snapshot(s) older than %d days and sweep %d "
                "rate-limit bucket(s) idle for more than %g hours",
                snapshots,
                arguments.snapshot_days,
                buckets,
                arguments.bucket_idle_hours,
            )
            return 0
        # `begin` rather than `connect`: both statements commit together or neither
        # does, so a failure half-way cannot leave the two swept to different dates.
        async with engine.begin() as connection:
            snapshots, buckets = await purge(
                connection, arguments.snapshot_days, arguments.bucket_idle_hours
            )
    finally:
        await engine.dispose()

    logger.info(
        "cleared %d stock snapshot(s) older than %d days and swept %d rate-limit "
        "bucket(s) idle for more than %g hours",
        snapshots,
        arguments.snapshot_days,
        buckets,
        arguments.bucket_idle_hours,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
