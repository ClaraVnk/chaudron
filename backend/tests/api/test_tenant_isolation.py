"""Runtime isolation between households.

``tests/tenancy`` proves the *schema* carries a tenant on every business table.
This file proves the *handlers* filter on it -- the two are independent failures,
and the second is the one a user can trigger with a guessed identifier.
"""

from __future__ import annotations

import httpx

from chaudron.domain.models import StorageKind
from tests.api.conftest import MakeLocation, MakeProduct
from tests.conftest import TenantPair, household_headers


async def test_one_household_never_sees_another_inventory(
    api_client: httpx.AsyncClient,
    tenant_pair: TenantPair,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    product = await make_product(name="Lait demi-écrémé")
    location_a = await make_location(tenant_pair.household_a, name="Frigo A")

    created = await api_client.post(
        "/v1/inventory",
        headers=household_headers(tenant_pair.household_a),
        json={
            "product_id": str(product.id),
            "location_id": str(location_a.id),
            "amount": "1",
            "unit": "l",
        },
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]

    seen_by_b = await api_client.get(
        "/v1/inventory", headers=household_headers(tenant_pair.household_b)
    )
    assert seen_by_b.json() == {"total": 0, "items": []}

    assert (
        await api_client.patch(
            f"/v1/inventory/{item_id}",
            headers=household_headers(tenant_pair.household_b),
            json={"amount": "99"},
        )
    ).status_code == 404

    assert (
        await api_client.delete(
            f"/v1/inventory/{item_id}", headers=household_headers(tenant_pair.household_b)
        )
    ).status_code == 404

    still_there = await api_client.get(
        "/v1/inventory", headers=household_headers(tenant_pair.household_a)
    )
    assert still_there.json()["total"] == 1


async def test_locations_are_scoped_to_their_household(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_location: MakeLocation
) -> None:
    await make_location(tenant_pair.household_a, name="Frigo A")
    await make_location(tenant_pair.household_b, name="Cave B", kind=StorageKind.CELLAR)

    listing_a = await api_client.get(
        "/v1/locations", headers=household_headers(tenant_pair.household_a)
    )
    assert [row["name"] for row in listing_a.json()] == ["Frigo A"]

    listing_b = await api_client.get(
        "/v1/locations", headers=household_headers(tenant_pair.household_b)
    )
    assert [row["name"] for row in listing_b.json()] == ["Cave B"]


async def test_stock_cannot_be_stored_in_another_household_location(
    api_client: httpx.AsyncClient,
    tenant_pair: TenantPair,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """The identifier is valid and belongs to somebody else. That is a 404."""
    product = await make_product()
    location_b = await make_location(tenant_pair.household_b, name="Frigo B")

    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(tenant_pair.household_a),
        json={
            "product_id": str(product.id),
            "location_id": str(location_b.id),
            "amount": "1",
            "unit": "l",
        },
    )
    assert response.status_code == 404
    assert response.json()["type"].endswith("/location-not-found")


async def test_a_private_product_is_invisible_to_another_household(
    api_client: httpx.AsyncClient, tenant_pair: TenantPair, make_product: MakeProduct
) -> None:
    """Purchase habits are personal data; a guessed product id must not reach them."""
    private = await make_product(name="Lait sans lactose", household=tenant_pair.household_b)

    response = await api_client.post(
        "/v1/inventory",
        headers=household_headers(tenant_pair.household_a),
        json={"product_id": str(private.id), "amount": "1", "unit": "l"},
    )
    assert response.status_code == 404
    assert response.json()["type"].endswith("/product-not-found")


async def test_locations_report_only_their_own_item_count(
    api_client: httpx.AsyncClient,
    tenant_pair: TenantPair,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    product = await make_product()
    location_a = await make_location(tenant_pair.household_a, name="Frigo A")
    await make_location(tenant_pair.household_b, name="Frigo B")

    await api_client.post(
        "/v1/inventory",
        headers=household_headers(tenant_pair.household_a),
        json={
            "product_id": str(product.id),
            "location_id": str(location_a.id),
            "amount": "2",
            "unit": "piece",
        },
    )

    listing_b = await api_client.get(
        "/v1/locations", headers=household_headers(tenant_pair.household_b)
    )
    assert [row["item_count"] for row in listing_b.json()] == [0]
