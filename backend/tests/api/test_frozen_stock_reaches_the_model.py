"""The freezer, all the way from ``inventory_lot`` to what crossed the boundary.

``tests/llm/test_frozen_stock_in_the_prompt.py`` pins the document a frozen item
produces; this file proves that a lot the household actually froze *becomes* one.
The two halves are worth keeping apart because the join between them is where a
feature quietly stops working: the flag can be perfectly rendered by a prompt
builder nobody ever hands a frozen item to.

Asserted on the persisted ``stock_snapshot`` rather than on the answer, for the
reason ``test_dietary_suggestions.py`` gives: the answer comes from a double that
would happily ignore any instruction, and the snapshot is what actually crossed
the boundary.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import FoodFamily, Household, Product, ProductSource, RecipeSuggestion
from tests.api.conftest import MakeLocation
from tests.api.test_providers import add_config
from tests.api.test_recipes import use_provider_double
from tests.conftest import MakeHousehold, household_headers

SUGGEST_URL = "/v1/recipes/suggest"

#: What the provider double's recipe calls for. The stock below is built out of
#: these so that the suggestion survives the post-generation check.
DOUBLE_INGREDIENTS = ("Courgettes", "Crème")


def today() -> date:
    return date.today()  # noqa: DTZ011 - a calendar date, as the column is


async def add_product(session: AsyncSession, name: str, family: FoodFamily) -> Product:
    product = Product(
        household_id=None, name=name, source=ProductSource.OPEN_FOOD_FACTS, food_family=family
    )
    session.add(product)
    await session.flush()
    return product


async def stock(
    api_client: httpx.AsyncClient, household: Household, product: Product, *, days: int = 2
) -> str:
    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product_id": str(product.id),
            "amount": "200",
            "unit": "g",
            "expires_on": (today() + timedelta(days=days)).isoformat(),
            "expiry_kind": "use_by",
        },
    )
    assert created.status_code == 201, created.text
    item_id: str = created.json()["id"]
    return item_id


async def snapshot_items(session: AsyncSession, household: Household) -> list[dict[str, Any]]:
    row = (
        await session.scalars(
            select(RecipeSuggestion).where(RecipeSuggestion.household_id == household.id)
        )
    ).first()
    assert row is not None
    items: list[dict[str, Any]] = row.stock_snapshot["items"]
    return items


async def test_a_frozen_lot_reaches_the_model_marked_and_undated_by_its_printed_date(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """Two facts travel, and neither is inferable from the other.

    ``frozen`` is what tells the model to plan a thaw. ``expires_in_days`` is what
    stops it treating the chicken as tomorrow's emergency -- and it is ninety days
    rather than two only because the freeze voided the printed date. A prompt
    carrying one without the other produces either a panicked recipe or one that
    starts by searing a block of ice.
    """
    household = await make_household()
    await add_config(db_session, household)
    await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, await add_product(db_session, name, FoodFamily.PRODUCE))
    chicken = await add_product(db_session, "Blanc de poulet", FoodFamily.FRESH_MEAT)
    lot = await stock(api_client, household, chicken)
    frozen = await api_client.post(
        f"/v1/inventory/{lot}/freeze", headers=household_headers(household)
    )
    assert frozen.status_code == 200, frozen.text
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )

    assert response.status_code == 200, response.text
    sent = {item["name"]: item for item in await snapshot_items(db_session, household)}
    assert set(sent) == {*DOUBLE_INGREDIENTS, "Blanc de poulet"}, (
        "frozen stock is real stock; hiding it would be a different lie from calling it fresh"
    )
    assert sent["Blanc de poulet"]["frozen"] is True
    assert sent["Blanc de poulet"]["expires_in_days"] == 90
    assert sent["Courgettes"]["frozen"] is False


async def test_a_thawed_lot_is_ordinary_stock_again_and_is_not_called_frozen(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
) -> None:
    """A lot carrying both dates is out of the freezer, and urgent.

    The flag follows ``ix_inventory_lot_frozen``'s own predicate -- frozen *and
    not since thawed* -- so a lot that came out this morning is described as what
    it is: three days of stock that needs eating, not something to plan a thaw
    for.
    """
    household = await make_household()
    await add_config(db_session, household)
    await make_location(household)
    for name in DOUBLE_INGREDIENTS:
        await stock(api_client, household, await add_product(db_session, name, FoodFamily.PRODUCE))
    chicken = await add_product(db_session, "Blanc de poulet", FoodFamily.FRESH_MEAT)
    lot = await stock(api_client, household, chicken)
    headers = household_headers(household)
    frozen = await api_client.post(f"/v1/inventory/{lot}/freeze", headers=headers)
    assert frozen.status_code == 200, frozen.text
    thawed = await api_client.post(f"/v1/inventory/{lot}/thaw", headers=headers)
    assert thawed.status_code == 200, thawed.text
    use_provider_double(api_app, household, "nominal")

    response = await api_client.post(SUGGEST_URL, headers=headers, json={"max_suggestions": 1})

    assert response.status_code == 200, response.text
    sent = {item["name"]: item for item in await snapshot_items(db_session, household)}
    assert sent["Blanc de poulet"]["frozen"] is False
    assert sent["Blanc de poulet"]["expires_in_days"] == 3
