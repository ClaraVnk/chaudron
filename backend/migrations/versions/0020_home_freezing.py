"""record that a household froze a lot itself, and what that does to its date

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-08 10:00:00.000000+00:00

The case this exists for: you buy chicken breast on Tuesday with a use-by of
Thursday, and you freeze it on Tuesday evening. Until now the application had no
way to be told, so it kept counting down to Thursday and would have put a
two-day alert on a piece of meat that is good for three months.

**Freezing is a state of the lot, not a change of product.** `product.food_family
= 'frozen'` already exists and means something else entirely -- *sold* frozen, a
bag of peas or a tub of ice cream, carrying a printed date that applies as it
stands. Reclassifying a home-frozen chicken breast into that family would hand it
an industrial product's date and lose the fact that it was ever fresh. So the two
columns land on `inventory_lot`, beside `opened_at`, whose mirror they are: one
brings the date forward, the other replaces it.

**Why two columns and not one flag.** Thawing is not the absence of freezing. It
starts the shortest clock in this application -- three days, refrigerated, ANSES
-- and it forbids going back into the freezer. A lot that was frozen and then
thawed carries *both* dates, and the expiry rule reads `thawed_at` first: reading
the other order would answer "three months" about meat that came out on Tuesday.
That ordering is the safety property of this revision.

`shelf_life_guideline` gains the durations to go with it. They are per family and
mostly NULL, which is deliberate and is the same posture the table already takes
elsewhere: a figure where a source supports one, nothing where it does not.
FoodKeeper gives freezer times split by cut, so meat gets the short end (minced,
three months) and fish the short end of *its* split (fatty fish, two months);
hard cheese gets six. The other six families get no figure and a note that says
which of the two reasons applies -- no honest number (a vegetable's answer
depends on whether it was blanched) or do not do this at all (a shell egg cracks,
a sealed tin bursts). NULL never means "keeps indefinitely", and the rule in
`domain/shelf_life.py` turns it into *no date proposed* rather than a fallback.

Reversible in full. `downgrade` drops the four columns; the rows in
`shelf_life_guideline` are re-seeded from the constant either way, so the table
matches whichever revision is deployed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from chaudron.domain.shelf_life import SHELF_LIFE_GUIDELINES

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None

_FROZEN_AT_COMMENT = (
    "When this household froze this lot itself. Distinct from "
    "product.food_family = 'frozen', which means sold frozen. Voids best_before "
    "and starts a new clock; see domain/shelf_life.py."
)
_THAWED_AT_COMMENT = (
    "When this lot was taken out of the freezer. Starts the ANSES three-day "
    "clock and forbids refreezing. Read before frozen_at by the expiry rule: a "
    "thawed lot carries both dates."
)
_FROZEN_DAYS_COMMENT = (
    "Days a home-frozen lot of this family keeps. NULL means no date is "
    "proposed -- either no honest figure exists or freezing is inadvisable; "
    "never 'indefinitely'. See freezing_note."
)
_FREEZING_NOTE_COMMENT = (
    "Why home_frozen_days is what it is, or why there is none. Shown beside a "
    "proposed date, in the same register as note."
)


def _reseed_guidelines(*, with_freezing: bool) -> None:
    """Rewrite every guideline row from the constant in `domain/shelf_life.py`.

    A delete-and-insert rather than an update per family, because the constant is
    the source of truth and a partial update would leave a row this revision
    never heard of. Safe: the table is published reference data with no foreign
    key pointing into it -- `inventory_lot` joins it by family, not by id.
    """
    columns = [
        sa.column("family", sa.Enum(name="food_family")),
        sa.column("label", sa.String()),
        sa.column("unopened_days", sa.SmallInteger()),
        sa.column("opened_days", sa.SmallInteger()),
        sa.column("date_kind", sa.Enum(name="expiry_date_kind")),
        sa.column("source_url", sa.Text()),
        sa.column("note", sa.Text()),
    ]
    if with_freezing:
        columns += [
            sa.column("home_frozen_days", sa.SmallInteger()),
            sa.column("freezing_note", sa.Text()),
        ]
    guideline = sa.table("shelf_life_guideline", *columns)

    op.execute(sa.delete(guideline))

    rows: list[dict[str, object]] = []
    for spec in SHELF_LIFE_GUIDELINES:
        row: dict[str, object] = {
            "family": spec.family.value,
            "label": spec.label,
            "unopened_days": spec.unopened_days,
            "opened_days": spec.opened_days,
            "date_kind": spec.date_kind.value,
            "source_url": spec.source_url,
            "note": spec.note,
        }
        if with_freezing:
            row["home_frozen_days"] = spec.home_frozen_days
            row["freezing_note"] = spec.freezing_note
        rows.append(row)

    op.bulk_insert(guideline, rows)


def upgrade() -> None:
    op.add_column(
        "inventory_lot",
        sa.Column("frozen_at", sa.Date(), nullable=True, comment=_FROZEN_AT_COMMENT),
    )
    op.add_column(
        "inventory_lot",
        sa.Column("thawed_at", sa.Date(), nullable=True, comment=_THAWED_AT_COMMENT),
    )
    op.add_column(
        "shelf_life_guideline",
        sa.Column(
            "home_frozen_days", sa.SmallInteger(), nullable=True, comment=_FROZEN_DAYS_COMMENT
        ),
    )
    op.add_column(
        "shelf_life_guideline",
        sa.Column("freezing_note", sa.Text(), nullable=True, comment=_FREEZING_NOTE_COMMENT),
    )

    # A lot cannot come out of a freezer it never went into -- except for a
    # product that was *sold* frozen, which the household did not freeze and can
    # still thaw. So this is not "thawed implies frozen"; it is the narrower
    # ordering rule, which holds in both cases.
    op.create_check_constraint(
        "thaw_follows_freeze",
        "inventory_lot",
        "frozen_at IS NULL OR thawed_at IS NULL OR thawed_at >= frozen_at",
    )
    # The same shape as the durations already on this table: a figure is a
    # positive number of days or it is absent. Zero would mean "expires the day
    # it is frozen", which is not a statement any source here makes.
    op.create_check_constraint(
        "home_frozen_days_positive",
        "shelf_life_guideline",
        "home_frozen_days IS NULL OR home_frozen_days > 0",
    )

    _reseed_guidelines(with_freezing=True)

    # The freezer view, and the expiry rule's own index. Every listing that sorts
    # by the effective date now reads these two columns on every row, and a
    # household's freezer is the part of its stock that grows without ever being
    # cleared out.
    op.create_index(
        "ix_inventory_lot_frozen",
        "inventory_lot",
        ["household_id", "frozen_at"],
        postgresql_where=sa.text("frozen_at IS NOT NULL AND thawed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_lot_frozen", table_name="inventory_lot")
    op.drop_constraint("ck_shelf_life_guideline_home_frozen_days_positive", "shelf_life_guideline")
    op.drop_constraint("ck_inventory_lot_thaw_follows_freeze", "inventory_lot")
    op.drop_column("shelf_life_guideline", "freezing_note")
    op.drop_column("shelf_life_guideline", "home_frozen_days")
    op.drop_column("inventory_lot", "thawed_at")
    op.drop_column("inventory_lot", "frozen_at")
    _reseed_guidelines(with_freezing=False)
