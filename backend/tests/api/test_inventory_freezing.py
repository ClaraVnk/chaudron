"""Freezing a lot at home, and taking it out again.

The case the whole feature exists for is one sentence long: *j'ai acheté du blanc
de poulet mais je le congèle*. Three things follow from it and each one has a test
below that would fail without the code that answers it -- the lot moves into the
freezer, its date stops being Thursday, and a recipe engine that thought it could
be cooked tonight is told otherwise.

The two refusals are the reason this file is longer than that. Freezing does not
rescue food that is already gone, and thawed food never goes back in. Neither is a
validation nicety: the first would replace a use-by crossed last Thursday with
ninety days, and the second is the ANSES rule with the shortest fuse in the
application. Both are asserted here against the real HTTP surface and the real
database, because both are one refactor away from becoming "and then the
application said the chicken was fine".
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import FoodFamily, Product, ProductSource, StorageKind
from tests.api.conftest import MakeLocation
from tests.conftest import MakeHousehold, household_headers

#: The figure seeded for ``fresh_meat`` -- the low end of the USDA FoodKeeper
#: split, which is minced meat. Named rather than repeated so a change to the
#: guideline table fails one assertion with a readable diff instead of six.
MEAT_FROZEN_DAYS = 90

#: What ANSES allows a thawed food, refrigerated.
THAWED_DAYS = 3


def today() -> date:
    return date.today()  # noqa: DTZ011 - a calendar date, as the column is


async def make_family_product(
    session: AsyncSession, *, name: str, family: FoodFamily | None
) -> Product:
    product = Product(household_id=None, name=name, source=ProductSource.MANUAL, food_family=family)
    session.add(product)
    await session.flush()
    return product


async def add_lot(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    product: Product,
    *,
    expires_on: date | None = None,
    opened_at: date | None = None,
    location_id: str | None = None,
    expiry_kind: str = "use_by",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product_id": str(product.id),
        "amount": "1",
        "unit": "kg",
        "expires_on": None if expires_on is None else expires_on.isoformat(),
        "opened_at": None if opened_at is None else opened_at.isoformat(),
    }
    if expires_on is not None:
        payload["expiry_kind"] = expiry_kind
    if location_id is not None:
        payload["location_id"] = location_id
    response = await client.post("/v1/inventory", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def freeze(
    client: httpx.AsyncClient, headers: dict[str, str], item_id: str
) -> httpx.Response:
    return await client.post(f"/v1/inventory/{item_id}/freeze", headers=headers)


async def thaw(client: httpx.AsyncClient, headers: dict[str, str], item_id: str) -> httpx.Response:
    return await client.post(f"/v1/inventory/{item_id}/thaw", headers=headers)


# --------------------------------------------------------------------------- #
# The date
# --------------------------------------------------------------------------- #


async def test_freezing_replaces_a_two_day_use_by_with_the_family_s_freezer_time(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The whole feature, in one assertion pair.

    Chicken bought today with a use-by of Thursday. Nothing about the packaging
    changes, and the printed date is still reported as printed -- that is what
    somebody read off the pack, and overwriting it would make the application look
    like it had invented a label. What changes is the date every filter, every
    alarm and every ranking in the application actually reads.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Blanc de poulet", family=FoodFamily.FRESH_MEAT
    )
    printed = today() + timedelta(days=2)
    lot = await add_lot(api_client, headers, chicken, expires_on=printed)
    assert lot["effective_expires_on"] == printed.isoformat()

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_on"] == printed.isoformat(), "the pack still says Thursday"
    assert body["frozen_at"] == today().isoformat()
    assert body["thawed_at"] is None
    assert body["effective_expires_on"] == (today() + timedelta(days=MEAT_FROZEN_DAYS)).isoformat()
    assert body["proposes_expiry_date"] is True


async def test_thawing_drops_ninety_days_to_three(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The other half, and the ordering that makes it work.

    A thawed lot carries *both* dates. Reading the frozen branch first would
    answer "three months" about meat that came out of the freezer this morning,
    which is the single failure ``domain/shelf_life.py`` orders its branches to
    prevent. This is that ordering, exercised end to end.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Blanc de poulet", family=FoodFamily.FRESH_MEAT
    )
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    frozen = await freeze(api_client, headers, lot["id"])
    assert frozen.status_code == 200, frozen.text

    response = await thaw(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frozen_at"] == today().isoformat(), "it stays on the record that it was frozen"
    assert body["thawed_at"] == today().isoformat()
    assert body["effective_expires_on"] == (today() + timedelta(days=THAWED_DAYS)).isoformat()


async def test_a_thawed_lot_is_urgent_stock_again(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Nothing special had to be written for this, and that is the point.

    ``expiring_within_days`` compares against the effective date, so a lot that
    came out of the freezer this morning enters the "eat this now" filter on its
    own -- while it was frozen it was three months away and correctly invisible.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Steak haché", family=FoodFamily.FRESH_MEAT
    )
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    await freeze(api_client, headers, lot["id"])

    while_frozen = await api_client.get("/v1/inventory?expiring_within_days=7", headers=headers)
    assert while_frozen.json()["total"] == 0, (
        "a frozen lot is not what the household must eat today"
    )

    await thaw(api_client, headers, lot["id"])

    once_thawed = await api_client.get("/v1/inventory?expiring_within_days=7", headers=headers)
    assert [row["id"] for row in once_thawed.json()["items"]] == [lot["id"]]


# --------------------------------------------------------------------------- #
# The two refusals
# --------------------------------------------------------------------------- #


async def test_a_lot_past_its_date_cannot_be_frozen(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Freezing halts spoilage; it does not reverse it.

    Accepting this would be the most dangerous arithmetic in the feature: the
    printed date is *voided* by freezing, so a use-by crossed last Thursday would
    come back as ninety days from today.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Blanc de poulet", family=FoodFamily.FRESH_MEAT
    )
    lot = await add_lot(api_client, headers, chicken, expires_on=today() - timedelta(days=1))

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 409, response.text
    problem = response.json()
    assert problem["type"].endswith("lot-already-expired")
    assert "does not reverse it" in problem["detail"]

    unchanged = await api_client.get(f"/v1/inventory?q={chicken.name}", headers=headers)
    assert unchanged.json()["items"][0]["frozen_at"] is None, "the refusal wrote nothing"


async def test_a_lot_expired_only_by_the_after_opening_rule_cannot_be_frozen_either(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The refusal reads the *effective* date, not the printed one.

    A pot opened three weeks ago whose packaging still says next month is exactly
    the case the after-opening rule exists for. A freeze check that compared
    ``best_before`` would wave it through and hand it three months, which is the
    same bug as the test above wearing a different hat.
    """
    household = await make_household()
    headers = household_headers(household)
    cream = await make_family_product(db_session, name="Crème", family=FoodFamily.FRESH_DAIRY)
    lot = await add_lot(
        api_client,
        headers,
        cream,
        expires_on=today() + timedelta(days=30),
        opened_at=today() - timedelta(days=21),
    )

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("lot-already-expired")


async def test_a_thawed_lot_is_never_refrozen(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The ANSES rule, and the one most dangerous to get wrong.

    The lot below is not expired -- it came out of the freezer this morning and
    has three days left -- so nothing else in the chain would have stopped it.
    Only the ``thawed_at`` test does, and it is deliberately read before the
    "already frozen" one so the refusal names the rule that matters.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Blanc de poulet", family=FoodFamily.FRESH_MEAT
    )
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    await freeze(api_client, headers, lot["id"])
    thawed = await thaw(api_client, headers, lot["id"])
    assert thawed.json()["effective_expires_on"] > today().isoformat(), (
        "not expired, so nothing but the thawed_at test could have refused it"
    )

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 409, response.text
    problem = response.json()
    assert problem["type"].endswith("lot-already-thawed")
    assert "must not go back into the freezer" in problem["detail"]


async def test_freezing_twice_is_refused_rather_than_extending_the_date(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A duplicate tap must not buy another three months."""
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(db_session, name="Rôti", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    await freeze(api_client, headers, lot["id"])

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("lot-already-frozen")


async def test_thawing_something_that_was_never_frozen_is_refused(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Writing ``thawed_at`` on a tin of chickpeas would give it three days."""
    household = await make_household()
    headers = household_headers(household)
    tin = await make_family_product(db_session, name="Pois chiches", family=FoodFamily.CANNED)
    lot = await add_lot(api_client, headers, tin, expires_on=today() + timedelta(days=400))

    response = await thaw(api_client, headers, lot["id"])

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("lot-not-frozen")


async def test_a_product_sold_frozen_can_be_thawed_without_ever_being_frozen_here(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The one legitimate ``thawed_at`` beside a null ``frozen_at``.

    A bag of peas was frozen by an industrial process, not by this household, and
    ``ck_inventory_lot_thaw_follows_freeze`` allows exactly this shape (migration
    ``0020``). Refusing it would have made the rule "never refreeze" unreachable
    for the half of a freezer that was bought frozen.
    """
    household = await make_household()
    headers = household_headers(household)
    peas = await make_family_product(db_session, name="Petits pois", family=FoodFamily.FROZEN)
    lot = await add_lot(
        api_client,
        headers,
        peas,
        expires_on=today() + timedelta(days=200),
        expiry_kind="best_before",
    )

    response = await thaw(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frozen_at"] is None
    assert body["thawed_at"] == today().isoformat()
    assert body["effective_expires_on"] == (today() + timedelta(days=THAWED_DAYS)).isoformat()

    refrozen = await freeze(api_client, headers, lot["id"])
    assert refrozen.status_code == 409, "and it may not go back in either"
    assert refrozen.json()["type"].endswith("lot-already-thawed")


# --------------------------------------------------------------------------- #
# Families with no figure, and the two the table advises against
# --------------------------------------------------------------------------- #


async def test_a_family_with_no_figure_freezes_and_proposes_no_date(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """No date is a real answer, and it is not "keeps indefinitely".

    Vegetables are the honest case: the outcome depends on whether they were
    blanched, which this application cannot know. So the lot freezes, the printed
    date is voided as it is for any frozen lot, and what comes back instead of a
    number is the note that says why there is none.
    """
    household = await make_household()
    headers = household_headers(household)
    beans = await make_family_product(db_session, name="Haricots verts", family=FoodFamily.PRODUCE)
    lot = await add_lot(
        api_client,
        headers,
        beans,
        expires_on=today() + timedelta(days=5),
        expiry_kind="best_before",
    )

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["frozen_at"] == today().isoformat()
    assert body["effective_expires_on"] is None
    assert body["proposes_expiry_date"] is False
    assert "blanchiment" in (body["freezing_note"] or "")


async def test_an_inadvisable_family_is_warned_about_rather_than_refused(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Eggs in shell burst, and the application says so instead of saying no.

    The argument is in ``FreezingOutcome``: the household is reporting a fact
    about its own freezer, and a refusal does not keep the eggs out of it -- it
    only keeps the application from knowing they are in it, which is the exact
    blindness this feature exists to end. The three refusals above all protect a
    *computed date*; this case threatens none, because there is no figure to
    compute from.

    What must never happen is silence, so the note is asserted rather than merely
    the status code.
    """
    household = await make_household()
    headers = household_headers(household)
    eggs = await make_family_product(db_session, name="Œufs fermiers", family=FoodFamily.EGGS)
    lot = await add_lot(
        api_client,
        headers,
        eggs,
        expires_on=today() + timedelta(days=10),
        expiry_kind="best_before",
    )

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposes_expiry_date"] is False
    assert "Ne congelez pas des œufs en coquille" in body["freezing_note"]


async def test_the_advice_is_readable_before_the_freezer_door_closes(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A warning that only arrives with the answer is a warning that arrives late."""
    household = await make_household()
    headers = household_headers(household)
    await make_family_product(db_session, name="Conserve de thon", family=FoodFamily.CANNED)
    tin = await make_family_product(db_session, name="Boîte de maïs", family=FoodFamily.CANNED)
    await add_lot(api_client, headers, tin, expires_on=today() + timedelta(days=400))

    listing = await api_client.get("/v1/inventory?q=maïs", headers=headers)

    assert listing.status_code == 200, listing.text
    item = listing.json()["items"][0]
    assert "la sertissure cède" in item["freezing_note"]
    assert item["frozen_at"] is None


async def test_a_product_with_no_family_carries_no_advice_and_no_date(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """An unresolved product gets nothing rather than a cautious default."""
    household = await make_household()
    headers = household_headers(household)
    mystery = await make_family_product(db_session, name="Produit inconnu", family=None)
    lot = await add_lot(api_client, headers, mystery, expires_on=today() + timedelta(days=10))

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["freezing_note"] is None
    assert body["effective_expires_on"] is None
    assert body["proposes_expiry_date"] is False


# --------------------------------------------------------------------------- #
# Where the lot ends up
# --------------------------------------------------------------------------- #


async def test_freezing_files_the_lot_in_the_household_s_freezer(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Point (a) of the case: it goes into the freezer stock."""
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo", kind=StorageKind.FRIDGE)
    freezer = await make_location(household, name="Congélateur", kind=StorageKind.FREEZER)
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(
        api_client,
        headers,
        chicken,
        expires_on=today() + timedelta(days=2),
        location_id=str(fridge.id),
    )

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["location_change"] == "moved"
    assert body["moved_to"]["id"] == str(freezer.id)
    assert body["location"]["id"] == str(freezer.id)

    in_freezer = await api_client.get(f"/v1/inventory?location_id={freezer.id}", headers=headers)
    assert [row["id"] for row in in_freezer.json()["items"]] == [lot["id"]]


async def test_thawing_files_it_back_in_the_fridge(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The three days ANSES allows a thawed food are three days *refrigerated*.

    Filing it there is not symmetry for its own sake: it is what makes the date
    the application then shows true of where the food actually is.
    """
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo", kind=StorageKind.FRIDGE)
    await make_location(household, name="Congélateur", kind=StorageKind.FREEZER)
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    await freeze(api_client, headers, lot["id"])

    response = await thaw(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    assert response.json()["location"]["id"] == str(fridge.id)


async def test_with_no_freezer_the_lot_freezes_where_it_stands(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The state change is the point; the filing is a courtesy.

    A household that has not told the application it owns a freezer still owns
    one, and refusing the freeze -- or inventing a location nobody created --
    would cost the date to gain a tidy list.
    """
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo", kind=StorageKind.FRIDGE)
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(
        api_client,
        headers,
        chicken,
        expires_on=today() + timedelta(days=2),
        location_id=str(fridge.id),
    )

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["location_change"] == "unresolved"
    assert body["moved_to"] is None
    assert body["location"]["id"] == str(fridge.id), "left exactly where it was"
    assert body["effective_expires_on"] == (today() + timedelta(days=MEAT_FROZEN_DAYS)).isoformat()


async def test_two_freezers_are_not_guessed_between(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """The application was told a door was opened, not which one."""
    household = await make_household()
    headers = household_headers(household)
    await make_location(household, name="Congélateur cuisine", kind=StorageKind.FREEZER)
    await make_location(household, name="Congélateur cave", kind=StorageKind.FREEZER)
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))

    response = await freeze(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    assert response.json()["location_change"] == "unresolved"
    assert response.json()["location"] is None


async def test_the_move_gives_way_to_the_merge_key_rather_than_failing(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Freeze one pack on Monday, buy another with the same date, freeze it too.

    ``uq_inventory_lot_merge_key`` covers ``(household, product, location,
    best_before, dimension)``, so the second move would land on top of the first
    row. Caught in advance: the freeze is what the household asked for and it
    happens, the lot stays where it is, and the answer says which of the two it
    got. The alternative -- an ``IntegrityError`` turned into "retry the request"
    -- is a dead end, because the retry fails identically.
    """
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo", kind=StorageKind.FRIDGE)
    freezer = await make_location(household, name="Congélateur", kind=StorageKind.FREEZER)
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    printed = today() + timedelta(days=2)

    monday = await add_lot(
        api_client, headers, chicken, expires_on=printed, location_id=str(freezer.id)
    )
    assert (await freeze(api_client, headers, monday["id"])).status_code == 200
    tuesday = await add_lot(
        api_client, headers, chicken, expires_on=printed, location_id=str(fridge.id)
    )

    response = await freeze(api_client, headers, tuesday["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["location_change"] == "occupied"
    assert body["location"]["id"] == str(fridge.id)
    assert body["frozen_at"] == today().isoformat(), "the freeze itself still happened"


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["freeze", "thaw"])
async def test_an_unknown_lot_is_a_404(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    action: str,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    missing = "00000000-0000-7000-8000-000000000000"

    response = await api_client.post(f"/v1/inventory/{missing}/{action}", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["type"].endswith("inventory-item-not-found")


async def test_another_household_s_lot_is_invisible_here_too(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Two new routes are two new ways to reach a neighbour's stock.

    The tenancy suite walks the schema rather than the routes, so a handler that
    resolved the lot without its household would pass it. This is the per-route
    half, and it is cheap.
    """
    mine = await make_household()
    theirs = await make_household()
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(
        api_client, household_headers(theirs), chicken, expires_on=today() + timedelta(days=2)
    )

    response = await freeze(api_client, household_headers(mine), lot["id"])

    assert response.status_code == 404, response.text


async def test_the_dates_are_the_server_s_and_not_the_client_s(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """``PATCH`` cannot set them, and the freeze route takes no body.

    ``frozen_at`` is the *start* of a duration, so a client able to choose it is a
    client able to choose its own expiry date. The refusals have nowhere to run on
    a ``PATCH``, which is why the two columns are reachable only through the two
    operations that enforce them.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(db_session, name="Poulet", family=FoodFamily.FRESH_MEAT)
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))

    patched = await api_client.patch(
        f"/v1/inventory/{lot['id']}",
        headers=headers,
        json={"frozen_at": (today() - timedelta(days=400)).isoformat()},
    )

    assert patched.status_code == 422, "the patch body is strict, and knows no such field"


async def test_thawing_a_lot_left_too_long_says_so_rather_than_looking_rescued(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The one place where two correct numbers add up to a misleading screen.

    Chicken frozen four months ago reads as expired: `frozen_at + 90` is a month
    behind. Thawing it sets the date to `today + 3`, so the interface goes from
    *expired* to *three days left* and looks as though opening the freezer had
    rescued the food.

    Both figures are right, and clamping the arithmetic would be the wrong fix:
    a `min()` here holds the lot permanently expired and refuses a safe food on
    quality grounds, which is how an application teaches a household to throw
    away things it should have eaten. The ninety days are a **quality** figure --
    the guideline table says so for the frozen family, at -18 °C food stays safe
    past its date -- while the three days are a **safety** limit that starts now
    whatever the meat has been through.

    So what is asserted is the sentence, not a different number.
    """
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Blanc de poulet", family=FoodFamily.FRESH_MEAT
    )
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    assert (await freeze(api_client, headers, lot["id"])).status_code == 200

    # Four months in the freezer, written where the rule reads it. Simulated
    # rather than waited for, and in the database rather than through a clock
    # patch, because the expiry is computed in SQL.
    await db_session.execute(
        text("UPDATE inventory_lot SET frozen_at = :on WHERE id = :id"),
        {"on": today() - timedelta(days=120), "id": uuid.UUID(lot["id"])},
    )
    await db_session.flush()

    response = await thaw(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective_expires_on"] == (today() + timedelta(days=THAWED_DAYS)).isoformat()
    warning = body["quality_warning"]
    assert warning is not None, "an expired-then-thawed lot must not look rescued"
    # Safe first, worse second. A warning that leads with quality is one a hungry
    # reader turns into "do not eat this".
    assert "sûr" in warning
    assert "qualité" in warning


async def test_an_ordinary_thaw_carries_no_quality_warning(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Without this the test above would pass on a constant string."""
    household = await make_household()
    headers = household_headers(household)
    chicken = await make_family_product(
        db_session, name="Blanc de poulet", family=FoodFamily.FRESH_MEAT
    )
    lot = await add_lot(api_client, headers, chicken, expires_on=today() + timedelta(days=2))
    assert (await freeze(api_client, headers, lot["id"])).status_code == 200

    response = await thaw(api_client, headers, lot["id"])

    assert response.status_code == 200, response.text
    assert response.json()["quality_warning"] is None
