"""Barcode resolution: the cache, the negative cache, and the failure modes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import get_catalog
from chaudron.domain.models import Product
from chaudron.domain.ports import CatalogRecord, ProductCatalogUnavailableError
from tests.api.conftest import RecordingCatalog
from tests.conftest import MakeHousehold, household_headers

MILK_GTIN = "3033490004743"
MILK_STORED = MILK_GTIN.rjust(14, "0")

MILK_RECORD = CatalogRecord(
    gtin=MILK_STORED,
    name="Lait demi-écrémé UHT",
    brand="Lactel",
    image_url="https://images.openfoodfacts.org/lait.jpg",
    category_tag="en:milks",
    payload={"product_name_fr": "Lait demi-écrémé UHT"},
)


@pytest.fixture
def catalog(api_app: FastAPI) -> RecordingCatalog:
    """Install a scripted catalogue in place of the Open Food Facts client."""
    double = RecordingCatalog({})
    api_app.dependency_overrides[get_catalog] = lambda: double
    return double


async def test_a_hit_is_cached_and_not_fetched_twice(
    api_client: httpx.AsyncClient,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    catalog.answers[MILK_STORED] = MILK_RECORD

    first = await api_client.get(
        f"/v1/products/lookup?gtin={MILK_GTIN}", headers=household_headers(household)
    )
    assert first.status_code == 200, first.text
    assert first.json()["name"] == "Lait demi-écrémé UHT"
    assert first.json()["gtin"] == MILK_GTIN

    second = await api_client.get(
        f"/v1/products/lookup?gtin={MILK_GTIN}", headers=household_headers(household)
    )
    assert second.status_code == 200
    assert catalog.calls == [MILK_STORED], "the cache is a condition of use, not an optimisation"


async def test_an_absence_is_cached_too(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    """A code Open Food Facts does not know must not be asked for on every scan."""
    household = await make_household()
    catalog.answers["03760020507350"] = None

    for _ in range(2):
        response = await api_client.get(
            "/v1/products/lookup?gtin=3760020507350", headers=household_headers(household)
        )
        assert response.status_code == 404
        assert response.json()["gtin"] == "3760020507350"
        assert response.json()["type"].endswith("/product-not-found")

    assert catalog.calls == ["03760020507350"]

    tombstone = (
        await db_session.scalars(
            select(Product).where(Product.household_id.is_(None), Product.gtin == "03760020507350")
        )
    ).one()
    assert tombstone.archived_at is not None, "a negative cache entry is not a product"


async def test_a_retailer_internal_code_is_refused_without_a_network_call(
    api_client: httpx.AsyncClient,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    response = await api_client.get(
        "/v1/products/lookup?gtin=2012345006789", headers=household_headers(household)
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/retailer-internal-barcode")
    assert catalog.calls == [], "a variable-weight code will never be in a public reference"


async def test_an_unavailable_catalogue_answers_503_with_retry_after(
    api_client: httpx.AsyncClient,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    catalog.answers[MILK_STORED] = ProductCatalogUnavailableError("down", retry_after=42)

    response = await api_client.get(
        f"/v1/products/lookup?gtin={MILK_GTIN}", headers=household_headers(household)
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "42"


async def test_a_stale_entry_is_served_when_the_catalogue_is_down(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    """Being banned by Open Food Facts must not break groceries already known."""
    household = await make_household()
    headers = household_headers(household)
    catalog.answers[MILK_STORED] = MILK_RECORD
    assert (
        await api_client.get(f"/v1/products/lookup?gtin={MILK_GTIN}", headers=headers)
    ).status_code == 200

    cached = (
        await db_session.scalars(
            select(Product).where(Product.household_id.is_(None), Product.gtin == MILK_STORED)
        )
    ).one()
    cached.off_synced_at = datetime.now(UTC) - timedelta(days=365)
    await db_session.flush()

    catalog.answers[MILK_STORED] = ProductCatalogUnavailableError("banned", retry_after=60)
    response = await api_client.get(f"/v1/products/lookup?gtin={MILK_GTIN}", headers=headers)
    assert response.status_code == 200
    assert response.json()["brand"] == "Lactel"
    assert catalog.calls == [MILK_STORED, MILK_STORED], "the refresh was attempted and failed"


async def test_manual_product_creation_is_private_to_the_household(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    household = await make_household()
    response = await api_client.post(
        "/v1/products",
        headers=household_headers(household),
        json={
            "name": "Confiture de mirabelles",
            "brand": None,
            "gtin": None,
            "default_unit": "g",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["default_unit"] == "g"

    product = await db_session.get(Product, body["id"])
    assert product is not None
    assert product.household_id == household.id


async def test_an_unknown_default_unit_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.post(
        "/v1/products",
        headers=household_headers(household),
        json={"name": "Vrac", "default_unit": "poignée"},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/unknown-unit")
