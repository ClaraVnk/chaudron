"""computed filter for avoided ingredients, mirroring the allergen mechanism

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-10 00:00:00.000000+00:00

Until now the only way to say "no kiwi" was ``free_text_restrictions``, which
revision ``0005`` deliberately made a *preference*: it reaches the model as a
sentence and filters nothing, because nothing in the catalogue said which
products contain kiwi. Open Food Facts does publish ``ingredients_tags``; this
revision stores it, and turns the sentence into a filter.

**The whole revision is one CASE, and it is the same one as ``0005``.**
``product.avoided_ingredients_risk`` is generated, and a product whose ingredient
list could not be read carries **every** entry of the vocabulary in it -- so
``NOT (:member_avoided && avoided_ingredients_risk)`` withholds it whether or not
the author of that query thought about the unreadable case. Nothing here is a new
mechanism; copying the existing one exactly is the point, because a second,
subtly different answer to "what does no data mean" is how the first one stops
being believed.

**The vocabulary is closed, and that is what makes the CASE possible.** The
unknown branch has to be a superset of anything a household can ask to avoid.
Over free text there is no such set, which is precisely why free text stayed a
preference and why ``avoided_ingredient`` is a native enum rather than a text
column: a household cannot invent a value the branch does not cover. The price
is that adding an ingredient is a revision -- it has to rewrite the generated
column -- and that price is the guarantee.

**``ingredients_tags`` is stored raw and is not what the filter reads.** Two
things need it. A tag nobody could resolve is the reason ``ingredient_state``
fell back to ``unknown``, and that is only diagnosable if it was kept. And when
the vocabulary grows, every product already in the catalogue can be reclassified
from this column; the alternative is re-fetching them through an API that permits
ten calls a minute, which for a real catalogue is measured in days.

**What this revision does not claim.** ``declared`` here means "Open Food Facts
published an ingredient list and every tag in it resolved" -- not "a manufacturer
asserted the absence of this food". The allergen columns rest on EU 1169/2011;
these rest on a wiki's parser. The two therefore stay in separate columns, under
separate names, all the way to the screen, and the interface is required to say
which is which. Merging them into one "exclusions" list would hand the weaker one
the stronger one's credibility, and there would be no way back.

Rollback
--------

``downgrade`` is real and total: index, constraints, columns, then the two enum
types, in that order. ``op.drop_column`` leaves a native enum behind, and an
``upgrade`` after an incomplete ``downgrade`` fails on "type already exists" --
the trap revisions ``0001`` and ``0005`` both document. Nothing is lost that
cannot be recovered: ``ingredients_tags`` is re-derivable from ``off_payload``,
which this revision does not touch, and a member's avoided list is one form to
fill in again. It is a *feature* being removed, not a fact being destroyed, which
is why this downgrade does not need to refuse anything the way ``0027``'s does.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from chaudron.domain.models import ALL_AVOIDED_INGREDIENTS_SQL, AvoidedIngredient

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: New native enum types, in creation order. Names match what the model passes to
#: ``pg_enum``; ``ingredient_data_state`` duplicates the two members of
#: ``allergen_data_state`` on purpose rather than reusing the type, so that a
#: filter cannot read one state and act on the other's column.
_ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("avoided_ingredient", tuple(member.value for member in AvoidedIngredient)),
    ("ingredient_data_state", ("unknown", "declared")),
)

_STATE_COMMENT: str = (
    "'unknown' means the upstream record published no ingredient list, or "
    "published one this application could not fully resolve. It never means "
    "'contains none of them'."
)

_TAGS_COMMENT: str = (
    "Raw upstream ingredient taxonomy tags. Evidence and backfill source; NOT "
    "what a filter reads -- see avoided_ingredients_risk."
)

_RISK_COMMENT: str = (
    "GENERATED. Avoidable ingredients this product cannot be cleared of, "
    "including every one of them when the ingredient list could not be read. "
    "The only column a filter should read; avoided_ingredients_named alone "
    "would treat ''unknown'' as safe."
)

_PERSON_COMMENT: str = (
    "Best-effort exclusions, applied as a filter but NOT a regulated "
    "declaration -- see `allergens` for that. Never logged, never sent to a "
    "model, loaded only through an explicit undefer()."
)


def _enum(name: str, *, create_type: bool = False) -> postgresql.ENUM:
    """Reference an enum type that already exists, without trying to create it."""
    values = next(members for type_name, members in _ENUM_TYPES if type_name == name)
    return postgresql.ENUM(*values, name=name, create_type=create_type)


def upgrade() -> None:
    for name, values in _ENUM_TYPES:
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=False)

    _extend_product()
    _extend_household_person()


def downgrade() -> None:
    # The component, not the rendered name: `drop_constraint` puts the template's
    # `ck_household_person_` prefix back on, exactly as `create_check_constraint`
    # did (revisions 0013 and 0016 carry the same note; 0010 is the clean-up of
    # the eighteen constraints that were deployed without it).
    op.drop_constraint("avoided_ingredients_wellformed", "household_person", type_="check")
    op.drop_column("household_person", "avoided_ingredients")

    op.drop_index("ix_product_avoided_ingredients_risk", table_name="product")
    op.drop_constraint("ingredient_arrays_wellformed", "product", type_="check")
    op.drop_constraint("ingredient_unknown_is_empty", "product", type_="check")
    for column in ("avoided_ingredients_risk", "avoided_ingredients_named", "ingredients_tags"):
        op.drop_column("product", column)
    op.drop_column("product", "ingredient_state")

    # Types last, and explicitly: dropping a column does not drop the type it
    # used, and an upgrade run after a downgrade that skipped this fails on
    # "type avoided_ingredient already exists".
    for name, _ in reversed(_ENUM_TYPES):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=False)


# --------------------------------------------------------------------------- #
# product
# --------------------------------------------------------------------------- #


def _extend_product() -> None:
    op.add_column(
        "product",
        sa.Column(
            "ingredient_state",
            _enum("ingredient_data_state"),
            server_default=sa.text("'unknown'"),
            nullable=False,
            comment=_STATE_COMMENT,
        ),
    )
    # Every existing row therefore backfills to `unknown`, which is the truth
    # about them: none was ingested with an ingredient list, so none of them can
    # support a claim about one. Backfilling to `declared` with an empty list
    # would silently clear the entire catalogue.
    op.add_column(
        "product",
        sa.Column(
            "ingredients_tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment=_TAGS_COMMENT,
        ),
    )
    op.add_column(
        "product",
        sa.Column(
            "avoided_ingredients_named",
            postgresql.ARRAY(_enum("avoided_ingredient")),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    # Raw SQL rather than a `Computed` column, for the reason revision 0005
    # gives: the expression casts an array literal to an enum array, which
    # alembic renders with quoting PostgreSQL then rejects. It is also, again,
    # the one line of this revision worth reading twice.
    op.execute(
        "ALTER TABLE product ADD COLUMN avoided_ingredients_risk avoided_ingredient[] "
        "GENERATED ALWAYS AS ("
        f"CASE WHEN ingredient_state = 'unknown' THEN {ALL_AVOIDED_INGREDIENTS_SQL}"
        " ELSE avoided_ingredients_named END"
        ") STORED"
    )
    op.execute(f"COMMENT ON COLUMN product.avoided_ingredients_risk IS '{_RISK_COMMENT}'")

    # The component, not the rendered name: the metadata template already adds
    # the `ck_product_` prefix, and passing the whole thing is what produced the
    # eighteen doubled names revision 0010 had to rename.
    op.create_check_constraint(
        "ingredient_unknown_is_empty",
        "product",
        "ingredient_state <> 'unknown' OR cardinality(avoided_ingredients_named) = 0",
    )
    # `ingredients_tags` is deliberately *not* emptied by the constraint above:
    # it is the evidence of the unreadability, and clearing it would erase the
    # only record of why the row is `unknown`. A NULL element in it is another
    # matter -- it makes every containment test answer "unknown", which is the
    # answer an exclusion filter must never give.
    op.create_check_constraint(
        "ingredient_arrays_wellformed",
        "product",
        "array_position(avoided_ingredients_named, NULL) IS NULL"
        " AND array_position(ingredients_tags, NULL) IS NULL",
    )
    op.create_index(
        "ix_product_avoided_ingredients_risk",
        "product",
        ["avoided_ingredients_risk"],
        unique=False,
        postgresql_using="gin",
    )


# --------------------------------------------------------------------------- #
# household_person
# --------------------------------------------------------------------------- #


def _extend_household_person() -> None:
    op.add_column(
        "household_person",
        sa.Column(
            "avoided_ingredients",
            postgresql.ARRAY(_enum("avoided_ingredient")),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment=_PERSON_COMMENT,
        ),
    )
    # The cardinality ceiling is rendered from the enum rather than typed as a
    # literal: a later revision adding a member would otherwise leave a bound
    # nobody can reach, and the ceiling would quietly become the vocabulary.
    op.create_check_constraint(
        "avoided_ingredients_wellformed",
        "household_person",
        "array_position(avoided_ingredients, NULL) IS NULL"
        f" AND cardinality(avoided_ingredients) <= {len(AvoidedIngredient)}",
    )
