"""The allergen property of ADR-0009, asserted again over a weaker vocabulary.

``tests/domain/test_allergen_unknown_is_not_safe.py`` is the model this file
follows, deliberately question for question: "no ingredient data" and "does not
contain it" are different statements, and the code that conflates them is easy to
write, passes review and reads perfectly. Asking the same question here is not
duplication. The avoided-ingredient filter is the *second* mechanism of this
shape, and a second mechanism is exactly where the reasoning gets summarised into
something that no longer holds -- ``avoided_ingredients_named`` is right there,
it is shorter, and reading it instead of the generated column would pass every
test that does not exist.

What is **not** claimed here, and the reason the interface says "best effort":
``declared`` on these columns means Open Food Facts published an ingredient list
and every tag in it resolved. It does not mean a manufacturer asserted anything.
The tests below are about the mechanism being safe in the direction of
over-exclusion; they say nothing about the wiki being right.

The database half runs against a real PostgreSQL, because the property is carried
by a generated column and a check constraint. Asserting it against the model
objects would test the docstring.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.constraints import (
    AVOIDED_INGREDIENT_LABELS,
    PANTRY_STAPLES,
    HouseholdConstraints,
    Person,
    ProductFacts,
    WithholdReason,
    staples_allowed_for,
    union_of,
    withhold_reason,
)
from chaudron.domain.dietary import AVOIDED_INGREDIENT_TAGS, assess_ingredients
from chaudron.domain.models import (
    AgeBand,
    Allergen,
    AllergenDataState,
    AvoidedIngredient,
    Diet,
    IngredientDataState,
    Product,
    ProductSource,
)
from chaudron.domain.ports import CatalogRecord
from chaudron.infra.repositories.products import SqlProductRepository

# --------------------------------------------------------------------------- #
# Reading the catalogue
# --------------------------------------------------------------------------- #


def test_a_product_with_no_ingredient_field_is_unknown_not_empty() -> None:
    """The majority case, and the one that must not read as "contains none".

    An implementation doing ``payload.get("ingredients_tags", [])`` produces an
    empty list, which every later reader is entitled to read as "names none of
    the avoidable ingredients".
    """
    assessment = assess_ingredients({"product_name": "Barquette de fraises"})

    assert assessment.state is IngredientDataState.UNKNOWN
    assert assessment.named == ()


def test_an_empty_tag_list_is_also_unknown() -> None:
    """The other shape of "no data" the API emits, and it means the same thing."""
    assert assess_ingredients({"ingredients_tags": []}).state is IngredientDataState.UNKNOWN


def test_a_parsed_list_that_names_nothing_avoidable_is_declared() -> None:
    """The contrast case, without which the two above pass for the wrong reason."""
    assessment = assess_ingredients({"ingredients_tags": ["en:water", "en:sugar"]})

    assert assessment.state is IngredientDataState.DECLARED
    assert assessment.named == ()


def test_an_unresolved_tag_sends_the_whole_record_back_to_unknown() -> None:
    """A list one line of which Open Food Facts could not parse is not a list.

    This is real data, not a contrived one: product ``3017624010701`` publishes
    ``["de:sugar", "de:palm oil", "de:hazelnuts", ...]`` -- the parser failed on
    every line and stored the raw German text. Certifying "names no kiwi" from
    that is a statement about a list nobody read.
    """
    assessment = assess_ingredients({"ingredients_tags": ["en:sugar", "de:palm oil"]})

    assert assessment.state is IngredientDataState.UNKNOWN
    assert assessment.unreadable_tags == ("de:palm oil",)
    assert assessment.named == (), (
        "an unreadable record must not carry a resolved list beside it: the "
        "narrower answer is the one a reader picks by mistake"
    )


def test_the_raw_tags_survive_even_when_the_record_is_unreadable() -> None:
    """Because they are the evidence of *why* it is unreadable, and the backfill."""
    assessment = assess_ingredients({"ingredients_tags": ["en:sugar", "de:palm oil"]})

    assert assessment.tags == ("en:sugar", "de:palm oil")


def test_the_hierarchy_is_what_makes_a_parent_tag_enough() -> None:
    """Open Food Facts expands ancestors into ``ingredients_tags``.

    ``en:onion-powder`` arrives accompanied by ``en:onion``; matching the entry a
    household would name therefore catches every variety underneath it, and the
    mapping does not have to enumerate them.
    """
    assessment = assess_ingredients(
        {"ingredients_tags": ["en:onion-powder", "en:onion", "en:vegetable"]}
    )

    assert assessment.named == (AvoidedIngredient.ONION,)


def test_tags_are_case_folded() -> None:
    """Real records carry both spellings of the same entry."""
    assessment = assess_ingredients({"ingredients_tags": ["EN:BANANA", "en:banana"]})

    assert assessment.named == (AvoidedIngredient.BANANA,)


def test_an_absurdly_long_list_is_unreadable_rather_than_stored() -> None:
    """A ceiling that fails towards withholding, on a shared table anyone reads."""
    assessment = assess_ingredients({"ingredients_tags": [f"en:x{n}" for n in range(1000)]})

    assert assessment.state is IngredientDataState.UNKNOWN
    assert assessment.named == ()


def test_every_avoidable_ingredient_has_a_tag_and_they_are_all_distinct() -> None:
    """The mapping covers the vocabulary and collides on none of it.

    Cheap, and it is the test that fails when somebody adds a member to the enum
    and forgets that a product then has no way to name it -- which would leave a
    filter that silently matches nothing on every documented product while still
    withholding every undocumented one.
    """
    assert set(AVOIDED_INGREDIENT_TAGS) == set(AvoidedIngredient)
    assert len(set(AVOIDED_INGREDIENT_TAGS.values())) == len(AvoidedIngredient)


def test_every_avoidable_ingredient_has_a_french_label() -> None:
    """A member with no label is a checkbox rendering as ``undefined``."""
    assert set(AVOIDED_INGREDIENT_LABELS) == set(AvoidedIngredient)


def test_the_kiwi_tag_is_the_ingredient_one_and_not_the_allergen_one() -> None:
    """The mistake this feature was one copy-paste away from shipping.

    ``en:kiwi`` exists -- in the *allergens* taxonomy, where it is regulated in
    Japan, and ``domain/dietary.py`` already lists it as out of scope. The
    ingredient taxonomy has no such entry; kiwi is ``en:kiwifruit``. Taking the
    familiar-looking tag would have produced a filter that matched nothing on the
    single example anybody tests first, while still looking like it worked --
    because undocumented products would have gone on being withheld.
    """
    assert AVOIDED_INGREDIENT_TAGS[AvoidedIngredient.KIWI] == "en:kiwifruit"

    assessment = assess_ingredients({"ingredients_tags": ["en:kiwifruit", "en:sugar"]})
    assert assessment.named == (AvoidedIngredient.KIWI,)


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #


def _facts(
    *,
    avoided_risk: frozenset[AvoidedIngredient],
    name: str = "Produit",
) -> ProductFacts:
    return ProductFacts(
        product_id=uuid.uuid7(),
        name=name,
        allergen_state=AllergenDataState.DECLARED,
        allergens_risk=frozenset(),
        avoided_ingredients_risk=avoided_risk,
        pnns_markers=frozenset(),
        category_tags=(),
    )


def _constraints(*avoided: AvoidedIngredient) -> HouseholdConstraints:
    return union_of(
        [
            Person(
                id=uuid.uuid7(),
                display_name="Camille",
                age_band=AgeBand.ADULT,
                diet=Diet.OMNIVORE,
                allergens=frozenset(),
                avoided_ingredients=frozenset(avoided),
                infant_texture=None,
                free_text_restrictions=None,
            )
        ]
    )


def test_the_screen_withholds_the_product_whose_list_could_not_be_read() -> None:
    """Written the way the service writes it, with no special case for unknown.

    The undocumented product is withheld by the shape of the data, not by this
    test having remembered that the state exists.
    """
    unreadable = _facts(avoided_risk=frozenset(AvoidedIngredient))
    declared_clear = _facts(avoided_risk=frozenset())

    constraints = _constraints(AvoidedIngredient.KIWI)

    assert withhold_reason(unreadable, constraints, ()) is WithholdReason.AVOIDED_INGREDIENT
    assert withhold_reason(declared_clear, constraints, ()) is None


def test_nothing_is_withheld_from_a_household_that_avoids_nothing() -> None:
    """The other half, and the one over-exclusion would break.

    An unreadable ingredient list is not a reason to withhold anything on its
    own -- it becomes one only against somebody who asked. Without this the
    filter would empty every inventory in the product.
    """
    unreadable = _facts(avoided_risk=frozenset(AvoidedIngredient))

    assert withhold_reason(unreadable, _constraints(), ()) is None


def test_an_allergen_outranks_an_avoided_ingredient_in_the_reported_reason() -> None:
    """Both apply; the interface is told about the one that matters more."""
    both = ProductFacts(
        product_id=uuid.uuid7(),
        name="Produit",
        allergen_state=AllergenDataState.UNKNOWN,
        allergens_risk=frozenset(Allergen),
        avoided_ingredients_risk=frozenset(AvoidedIngredient),
        pnns_markers=frozenset(),
        category_tags=(),
    )
    people = union_of(
        [
            Person(
                id=uuid.uuid7(),
                display_name="Camille",
                age_band=AgeBand.ADULT,
                diet=Diet.OMNIVORE,
                allergens=frozenset({Allergen.PEANUTS}),
                avoided_ingredients=frozenset({AvoidedIngredient.KIWI}),
                infant_texture=None,
                free_text_restrictions=None,
            )
        ]
    )

    assert withhold_reason(both, people, ()) is WithholdReason.ALLERGEN


def test_the_union_of_two_eaters_avoids_what_either_of_them_avoids() -> None:
    """Cooking one meal for two people means cooking for the stricter of them."""

    def eater(*avoided: AvoidedIngredient) -> Person:
        return Person(
            id=uuid.uuid7(),
            display_name="Camille",
            age_band=AgeBand.ADULT,
            diet=Diet.OMNIVORE,
            allergens=frozenset(),
            avoided_ingredients=frozenset(avoided),
            infant_texture=None,
            free_text_restrictions=None,
        )

    united = union_of([eater(AvoidedIngredient.KIWI), eater(AvoidedIngredient.ONION)])

    assert united.avoided_ingredients == frozenset(
        {AvoidedIngredient.KIWI, AvoidedIngredient.ONION}
    )


def test_the_olive_oil_staple_is_withheld_from_somebody_avoiding_olives() -> None:
    """The one ingredient that reaches a recipe without passing the catalogue.

    Open Food Facts does not make ``en:olive-oil`` a child of ``en:olive``, so no
    amount of walking the taxonomy reaches it -- and the pantry staples bypass
    the catalogue entirely anyway. Declared per entry for exactly this reason.
    """
    allowed = staples_allowed_for(_constraints(AvoidedIngredient.OLIVE), ())

    assert "huile olive" not in {staple.label for staple in allowed}
    assert "huile tournesol" in {staple.label for staple in allowed}
    assert len(allowed) == len(PANTRY_STAPLES) - 1


# --------------------------------------------------------------------------- #
# Answering the query
# --------------------------------------------------------------------------- #
#
# From here on, PostgreSQL. `avoided_ingredients_risk` is a generated column and
# the invariants are check constraints; asserting them in Python would assert
# nothing about what a service will actually run.


async def _add(session: AsyncSession, **columns: object) -> uuid.UUID:
    product_id = uuid.uuid7()
    await session.execute(
        sa.insert(Product).values(
            id=product_id,
            household_id=None,
            name="fixture",
            source=ProductSource.OPEN_FOOD_FACTS,
            **columns,
        )
    )
    return product_id


async def test_the_obvious_exclusion_query_withholds_the_unreadable_product(
    db_session: AsyncSession,
) -> None:
    """The headline. Written the way a service would write it, with no special case.

    Three products, one query, and the only one that comes back is the one whose
    ingredient list was actually read and did not name kiwi. This is the test
    that fails if somebody later "optimises" the unknown branch out of the
    generated column, or points the query at ``avoided_ingredients_named``.
    """
    silent = await _add(db_session)
    declared_clear = await _add(db_session, ingredient_state=IngredientDataState.DECLARED)
    declared_kiwi = await _add(
        db_session,
        ingredient_state=IngredientDataState.DECLARED,
        avoided_ingredients_named=[AvoidedIngredient.KIWI],
    )

    excluded = sa.select(Product.id).where(
        Product.id.in_([silent, declared_clear, declared_kiwi]),
        ~Product.avoided_ingredients_risk.overlap([AvoidedIngredient.KIWI]),
    )
    survivors = set((await db_session.scalars(excluded)).all())

    assert survivors == {declared_clear}, (
        "a product with no readable ingredient list survived a kiwi exclusion: "
        "'unknown' is being read as 'contains no kiwi'"
    )


@pytest.mark.parametrize(
    "ingredient", list(AvoidedIngredient), ids=[i.value for i in AvoidedIngredient]
)
async def test_no_avoidable_ingredient_escapes_the_unknown_branch(
    db_session: AsyncSession, ingredient: AvoidedIngredient
) -> None:
    """Every member, one by one, and the reason this is parametrised.

    The unknown branch is only safe while it stays a *superset* of everything a
    household can ask for. A member added to the enum without the revision that
    rewrites the generated column would leave that member filterable on
    documented products and silently unfilterable on undocumented ones -- the
    exact asymmetry this whole design exists to remove, reintroduced by an edit
    that looks like data entry.
    """
    product_id = await _add(db_session)

    excluded = sa.select(Product.id).where(
        Product.id == product_id,
        ~Product.avoided_ingredients_risk.overlap([ingredient]),
    )

    assert (await db_session.scalars(excluded)).all() == [], (
        f"a product with no readable ingredient list survived a {ingredient.value} "
        f"exclusion: the generated column's unknown branch no longer covers the "
        f"whole vocabulary"
    )


async def test_an_unreadable_product_carries_every_avoidable_ingredient_as_a_risk(
    db_session: AsyncSession,
) -> None:
    """Not thirty separate rules: one generated column, maximally excluding."""
    product_id = await _add(db_session)

    risk = await db_session.scalar(
        sa.select(Product.avoided_ingredients_risk).where(Product.id == product_id)
    )

    assert risk is not None
    assert set(risk) == set(AvoidedIngredient)


async def test_the_risk_column_cannot_be_written(db_session: AsyncSession) -> None:
    """Generated, therefore not something a hurried ``UPDATE`` can quietly clear."""
    with pytest.raises(ProgrammingError):
        await db_session.execute(
            sa.insert(Product).values(
                id=uuid.uuid7(),
                household_id=None,
                name="fixture",
                source=ProductSource.OPEN_FOOD_FACTS,
                avoided_ingredients_risk=[],
            )
        )


async def test_an_unknown_product_cannot_carry_a_resolved_list(
    db_session: AsyncSession,
) -> None:
    """A row that says "unreadable" while listing what it resolved contradicts itself."""
    with pytest.raises(IntegrityError, match="ingredient_unknown_is_empty"):
        await _add(db_session, avoided_ingredients_named=[AvoidedIngredient.KIWI])


async def test_the_raw_tags_are_kept_on_an_unknown_product(
    db_session: AsyncSession,
) -> None:
    """The constraint above empties the resolved list and *not* the evidence.

    Clearing ``ingredients_tags`` alongside it would erase the only record of why
    the row is unknown, and the only thing a widened vocabulary could be
    backfilled from without re-fetching the catalogue at ten calls a minute.
    """
    product_id = await _add(db_session, ingredients_tags=["en:sugar", "de:palm oil"])

    tags = await db_session.scalar(
        sa.select(Product.ingredients_tags).where(Product.id == product_id)
    )

    assert tags == ["en:sugar", "de:palm oil"]


async def test_a_null_element_cannot_enter_an_ingredient_array(
    db_session: AsyncSession,
) -> None:
    """NULL inside the array makes every containment test answer "unknown".

    ``NOT (x && y)`` with a NULL in ``y`` is NULL, and a NULL predicate drops the
    row from a ``WHERE`` -- silently turning an exclusion filter into a
    disclosure.
    """
    with pytest.raises(IntegrityError, match="ingredient_arrays_wellformed"):
        await _add(db_session, ingredients_tags=["en:sugar", None])


async def test_a_refresh_that_loses_the_list_loses_the_claim(
    db_session: AsyncSession,
) -> None:
    """A contributor blanking a wiki page must revoke what it used to support.

    The tempting implementation only writes the ingredient columns when the
    record has something to say, which leaves yesterday's parsed list in place
    while the upstream page now says nothing.
    """
    repository = SqlProductRepository(db_session)
    gtin = "00003017620422"
    await repository.upsert_public(
        CatalogRecord(
            gtin=gtin,
            name="Pot de confiture",
            ingredient_state=IngredientDataState.DECLARED,
            avoided_ingredients_named=(AvoidedIngredient.STRAWBERRY,),
            ingredients_tags=("en:strawberry", "en:sugar"),
        )
    )

    await repository.upsert_public(CatalogRecord(gtin=gtin, name="Pot de confiture"))

    state, risk = (
        await db_session.execute(
            sa.select(Product.ingredient_state, Product.avoided_ingredients_risk).where(
                Product.gtin == gtin, Product.household_id.is_(None)
            )
        )
    ).one()
    assert state is IngredientDataState.UNKNOWN
    assert set(risk) == set(AvoidedIngredient)


async def test_a_member_cannot_hold_a_null_avoided_ingredient(
    db_session: AsyncSession,
) -> None:
    """Same NULL trap, on the other side of the containment test."""
    from chaudron.domain.models import Household, HouseholdPerson

    household = Household(name="Foyer")
    db_session.add(household)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="avoided_ingredients_wellformed"):
        await db_session.execute(
            sa.insert(HouseholdPerson).values(
                id=uuid.uuid7(),
                household_id=household.id,
                display_name="Camille",
                avoided_ingredients=[AvoidedIngredient.KIWI, None],
            )
        )
