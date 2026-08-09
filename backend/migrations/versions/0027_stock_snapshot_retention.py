"""make recipe_suggestion.stock_snapshot droppable, so a retention job can drop it

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-09 00:00:00.000000+00:00

``docs/security-model.md`` section 8.2 is unambiguous about this one column:
``recipe_suggestion.stock_snapshot`` is *"a home's complete inventory, frozen and
retained"*, it is sent to a model provider, and it must "be given **the shortest
retention period in the system**". Section 8.4 then proposes thirty days and
labels its whole table *"proposals to be arbitrated, not decisions"*.

The proposal has never been implementable, because the column is ``NOT NULL``.
There was no way to stop holding the snapshot short of deleting the whole
suggestion -- and the suggestion is not the problem. Its title, its steps, its
rating and its cost are what makes the feature reviewable, and none of them is an
inventory of somebody's cupboards.

**NULL means "no longer held", not "there was none".** That distinction is the
reason this is a nullable column rather than an empty object. Writing ``'{}'`` on
expiry would keep the ``NOT NULL`` and record a *fact*: that the household had
nothing in stock when the suggestion was made. Revision ``0014`` refused to invent
a registrant and ``0016`` refused to invent a consent date, both because a
fabricated fact is worse than a missing one; an empty cupboard is the same trap,
and it would be read back by the one screen the column exists for -- "why did it
suggest that when I had no eggs?" -- as an answer rather than as a silence. The
column comment says so in the database, where anybody reading a NULL will be.

**The partial index is the job, not an optimisation.** ``ix_rate_limit_bucket_
updated_at`` was created for the same reason in revision ``0018``: a sweep that
deletes by age across every tenant needs an index on age alone, and this one is
partial on ``stock_snapshot IS NOT NULL`` so it holds only the rows still waiting
to be purged. It therefore *shrinks as the job works*, and a run that finds
nothing to do reads nothing.

Rollback
--------

``downgrade`` restores ``NOT NULL``, and it **refuses rather than fabricates** if
the retention job has already run: a purged row has no snapshot, and the only ways
to satisfy the constraint are to invent one or to delete the suggestion. Both are
worse than staying on this revision, so the downgrade reports the count and names
the two honest remedies -- delete the purged suggestions, or do not go back.
That is the "documented rollback" half of a migration that cannot be blindly
reversible, not an oversight.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE: str = "recipe_suggestion"
_COLUMN: str = "stock_snapshot"
_INDEX: str = "ix_recipe_suggestion_snapshot_retention"

_COMMENT: str = (
    "What was sent to the model: a complete inventory of the household, and the "
    "most sensitive column in this database (security-model.md 8.2). NULL means "
    "the retention job has dropped it, NOT that the household had nothing in "
    "stock -- an empty object here would have been a fabricated fact. See "
    "scripts/purge_retained_data.py and revision 0027."
)


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=postgresql.JSONB(),
        nullable=True,
        comment=_COMMENT,
        existing_comment=None,
    )
    # Partial: only the rows a purge could still act on. The index shrinks as the
    # job works, which is the opposite of what a plain index on created_at would do.
    op.create_index(
        _INDEX,
        _TABLE,
        ["created_at"],
        unique=False,
        postgresql_where=sa.text(f"{_COLUMN} IS NOT NULL"),
    )


def downgrade() -> None:
    purged = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NULL")  # noqa: S608
    )
    if purged:
        raise RuntimeError(
            f"{purged} recipe suggestion(s) have had their stock_snapshot purged by "
            f"the retention job, and this downgrade would have to restore NOT NULL "
            f"over them. There is no snapshot left to restore and inventing one -- "
            f"an empty object, say -- would record that those households had nothing "
            f"in stock, which is a fabricated fact rather than a missing one. The two "
            f"honest options are: DELETE the {purged} purged suggestion(s) and re-run "
            f"this downgrade, or stay on revision 0027."
        )
    op.drop_index(_INDEX, table_name=_TABLE)
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=postgresql.JSONB(),
        nullable=False,
        comment=None,
        existing_comment=_COMMENT,
    )
