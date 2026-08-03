"""The inventory endpoints, along the paths a client actually walks."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    InventoryLot,
    StockMovement,
    StockMovementKind,
    StorageKind,
)
from tests.api.conftest import MakeLocation, MakeProduct
from tests.conftest import MakeHousehold, household_headers


async def test_create_read_and_page_through_the_inventory(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    milk = await make_product(name="Lait demi-écrémé", gtin="03033490004743")

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(milk.id),
            "location_id": str(fridge.id),
            "amount": "1.5",
            "unit": "l",
            "expires_on": "2099-01-31",
            "expiry_kind": "use_by",
            "source": "barcode_scan",
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()
    # A string, not a number: the contract is explicit, and a float here loses
    # decimals on a quantity of food.
    assert item["quantity"] == {"amount": "1.500", "unit": "l"}
    assert item["product"]["gtin"] == "3033490004743", "the padding is a storage detail"
    assert item["location"]["kind"] == "fridge"
    assert item["source"] == "barcode_scan"
    assert item["created_at"].endswith("Z")

    listing = await api_client.get("/v1/inventory", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert [row["id"] for row in body["items"]] == [item["id"]]

    page = await api_client.get("/v1/inventory?limit=1&offset=1", headers=headers)
    assert page.json() == {"total": 1, "items": []}


async def test_filters_narrow_the_listing(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    pantry = await make_location(household, name="Placard", kind=StorageKind.PANTRY)
    milk = await make_product(name="Lait demi-écrémé")
    rice = await make_product(name="Riz basmati", brand="Taureau Ailé")

    soon = (date.today() + timedelta(days=2)).isoformat()  # noqa: DTZ011 - calendar date
    late = (date.today() + timedelta(days=200)).isoformat()  # noqa: DTZ011 - calendar date
    for product, location, expires in ((milk, fridge, soon), (rice, pantry, late)):
        response = await api_client.post(
            "/v1/inventory",
            headers=headers,
            json={
                "product_id": str(product.id),
                "location_id": str(location.id),
                "amount": "1",
                "unit": "kg",
                "expires_on": expires,
                "expiry_kind": "best_before",
            },
        )
        assert response.status_code == 201, response.text

    by_location = await api_client.get(f"/v1/inventory?location_id={pantry.id}", headers=headers)
    assert [row["product"]["name"] for row in by_location.json()["items"]] == ["Riz basmati"]

    by_query = await api_client.get("/v1/inventory?q=lait", headers=headers)
    assert by_query.json()["total"] == 1

    by_brand = await api_client.get("/v1/inventory?q=Taureau", headers=headers)
    assert by_brand.json()["total"] == 1

    expiring = await api_client.get("/v1/inventory?expiring_within_days=7", headers=headers)
    assert [row["product"]["name"] for row in expiring.json()["items"]] == ["Lait demi-écrémé"]


async def test_scanning_the_same_pack_twice_merges_into_one_lot(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """500 g then 1 kg of the same flour is one lot of 1.5 kg, not two lines."""
    household = await make_household()
    headers = household_headers(household)
    pantry = await make_location(household, name="Placard", kind=StorageKind.PANTRY)
    flour = await make_product(name="Farine T55", brand="Francine")

    first = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(flour.id),
            "location_id": str(pantry.id),
            "amount": "1",
            "unit": "kg",
        },
    )
    assert first.status_code == 201, first.text

    second = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(flour.id),
            "location_id": str(pantry.id),
            "amount": "500",
            "unit": "g",
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    # Re-expressed in the unit already on the lot, never contradicting the
    # canonical total.
    assert second.json()["quantity"] == {"amount": "1.500", "unit": "kg"}

    listing = await api_client.get("/v1/inventory", headers=headers)
    assert listing.json()["total"] == 1

    movements = (
        await db_session.scalars(
            select(StockMovement).where(StockMovement.household_id == household.id)
        )
    ).all()
    assert [movement.kind for movement in movements] == [
        StockMovementKind.INTAKE,
        StockMovementKind.INTAKE,
    ]


async def test_patch_changes_only_what_it_names(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household, name="Frigo")
    freezer = await make_location(household, name="Congélateur", kind=StorageKind.FREEZER)
    peas = await make_product(name="Petits pois", brand="Bonduelle")

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(peas.id),
            "location_id": str(fridge.id),
            "amount": "750",
            "unit": "g",
            "expires_on": "2099-03-01",
            "expiry_kind": "best_before",
        },
    )
    item_id = created.json()["id"]

    patched = await api_client.patch(
        f"/v1/inventory/{item_id}",
        headers=headers,
        json={"amount": "500", "location_id": str(freezer.id)},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["quantity"] == {"amount": "500.000", "unit": "g"}
    assert body["location"]["id"] == str(freezer.id)
    assert body["expires_on"] == "2099-03-01", "an unsent field is not a cleared field"

    cleared = await api_client.patch(
        f"/v1/inventory/{item_id}", headers=headers, json={"expires_on": None}
    )
    assert cleared.json()["expires_on"] is None
    assert cleared.json()["expiry_kind"] == "unknown"

    adjustments = (
        await db_session.scalars(
            select(StockMovement).where(
                StockMovement.household_id == household.id,
                StockMovement.kind == StockMovementKind.ADJUSTMENT,
            )
        )
    ).all()
    assert len(adjustments) == 1
    assert adjustments[0].delta_canonical == -250


async def test_delete_records_the_reason_in_the_ledger(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """Waste and consumption are different facts; the ledger must keep them apart."""
    household = await make_household()
    headers = household_headers(household)
    fridge = await make_location(household)
    yoghurt = await make_product(name="Yaourt nature", brand="Danone")

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={
            "product_id": str(yoghurt.id),
            "location_id": str(fridge.id),
            "amount": "4",
            "unit": "piece",
        },
    )
    item_id = uuid.UUID(created.json()["id"])

    deleted = await api_client.delete(f"/v1/inventory/{item_id}?reason=wasted", headers=headers)
    assert deleted.status_code == 204

    assert (await api_client.get("/v1/inventory", headers=headers)).json()["total"] == 0

    lot = await db_session.get(InventoryLot, item_id)
    assert lot is not None, "a removal is a state change, never a DELETE"
    assert lot.depleted_at is not None

    movement = (
        await db_session.scalars(
            select(StockMovement).where(
                StockMovement.inventory_lot_id == item_id,
                StockMovement.kind == StockMovementKind.WASTE,
            )
        )
    ).one()
    assert movement.delta_canonical == -4
    assert movement.reason == "wasted"


async def test_delete_defaults_to_consumed(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_product: MakeProduct,
) -> None:
    household = await make_household()
    headers = household_headers(household)
    bread = await make_product(name="Pain de campagne", brand=None)

    created = await api_client.post(
        "/v1/inventory",
        headers=headers,
        json={"product_id": str(bread.id), "amount": "1", "unit": "piece"},
    )
    item_id = uuid.UUID(created.json()["id"])

    assert (await api_client.delete(f"/v1/inventory/{item_id}", headers=headers)).status_code == 204
    movement = (
        await db_session.scalars(
            select(StockMovement).where(StockMovement.inventory_lot_id == item_id)
        )
    ).all()
    assert {entry.kind for entry in movement} == {
        StockMovementKind.INTAKE,
        StockMovementKind.CONSUMPTION,
    }


async def test_inline_product_creation(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Manual entry is a first-class path: no barcode, no pre-existing product."""
    household = await make_household()
    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(household),
        json={
            "product": {"name": "Carottes du marché", "default_unit": "g"},
            "amount": "800",
            "unit": "g",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["product"]["name"] == "Carottes du marché"
