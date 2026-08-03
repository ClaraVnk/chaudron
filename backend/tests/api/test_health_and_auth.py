"""Health probes and the provisional tenant resolution."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest import MakeHousehold, household_headers


async def test_healthz_answers_without_touching_the_database(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_the_database(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


async def test_missing_household_header_is_rejected(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/v1/locations")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 401
    assert body["type"].endswith("/household-not-resolved")


async def test_unknown_household_is_rejected(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/v1/locations", headers={"X-Household-Id": str(uuid.uuid7())})
    assert response.status_code == 401


async def test_malformed_household_header_is_rejected(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/v1/locations", headers={"X-Household-Id": "not-a-uuid"})
    assert response.status_code == 401


async def test_known_household_is_accepted(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.get("/v1/locations", headers=household_headers(household))
    assert response.status_code == 200


async def test_every_response_carries_a_request_id(api_client: httpx.AsyncClient) -> None:
    """The identifier that ties an opaque error to the log line explaining it."""
    response = await api_client.get("/healthz")
    assert response.headers["X-Request-Id"]
