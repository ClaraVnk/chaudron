"""The caps on the two endpoints that spend money or a shared quota (AUD-007, AUD-008).

Two layers are tested, because they fail differently. The limiters themselves are
exercised against a fake clock -- real time in a rate-limit test is how a suite
becomes flaky at 3am -- and the wiring is exercised over HTTP, because a limiter
nobody attached to a route protects nothing.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import get_catalog
from chaudron.api.throttling import (
    AtCapacityError,
    ConcurrencyLimiter,
    RateLimiter,
    Throttles,
)
from tests.api.conftest import MakeLocation, MakeProduct, RecordingCatalog
from tests.api.test_products import MILK_GTIN, MILK_RECORD, MILK_STORED
from tests.api.test_providers import add_config
from tests.api.test_recipes import SUGGEST_URL, stock_cream, use_provider_double
from tests.conftest import MakeHousehold, household_headers


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- #
# RateLimiter
# --------------------------------------------------------------------------- #


def test_a_key_spends_its_budget_and_is_then_refused() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60.0, clock=clock)

    for _ in range(3):
        limiter.acquire("household")

    with pytest.raises(AtCapacityError) as refused:
        limiter.acquire("household")
    assert refused.value.retry_after >= 1


def test_one_key_exhausting_its_budget_does_not_touch_another() -> None:
    """The property the whole design rests on: a noisy household is contained."""
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60.0, clock=clock)

    limiter.acquire("noisy")
    limiter.acquire("noisy")
    with pytest.raises(AtCapacityError):
        limiter.acquire("noisy")

    limiter.acquire("quiet")


def test_the_budget_refills_over_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60.0, clock=clock)
    limiter.acquire("household")
    limiter.acquire("household")

    with pytest.raises(AtCapacityError) as refused:
        limiter.acquire("household")

    # Exactly the delay the limiter advertised, and not a millisecond less.
    clock.advance(refused.value.retry_after)
    limiter.acquire("household")


def test_a_full_window_of_silence_does_not_grant_extra_credit() -> None:
    """A token bucket is capped: idling does not bank requests for later."""
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60.0, clock=clock)
    clock.advance(6000.0)

    limiter.acquire("household")
    limiter.acquire("household")
    with pytest.raises(AtCapacityError):
        limiter.acquire("household")


def test_idle_keys_are_forgotten_so_the_table_cannot_grow_forever() -> None:
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60.0, clock=clock)
    for index in range(50):
        limiter.acquire(f"household-{index}")

    clock.advance(120.0)
    limiter.acquire("survivor")

    assert len(limiter._buckets) == 1, "swept keys were fully refilled; keeping them is a leak"


# --------------------------------------------------------------------------- #
# ConcurrencyLimiter
# --------------------------------------------------------------------------- #


def test_a_key_cannot_hold_more_slots_than_its_share() -> None:
    limiter = ConcurrencyLimiter(per_key=2, total=10)
    with limiter.slot("household"), limiter.slot("household"), pytest.raises(AtCapacityError):
        limiter.slot("household").__enter__()


def test_the_process_wide_cap_applies_across_keys() -> None:
    limiter = ConcurrencyLimiter(per_key=2, total=2)
    with limiter.slot("a"), limiter.slot("b"), pytest.raises(AtCapacityError):
        limiter.slot("c").__enter__()


def test_a_slot_is_released_even_when_the_request_fails() -> None:
    """Otherwise one 500 permanently narrows the endpoint for that household."""
    limiter = ConcurrencyLimiter(per_key=1, total=1)

    with pytest.raises(RuntimeError), limiter.slot("household"):
        raise RuntimeError("the endpoint blew up")

    with limiter.slot("household"):
        pass


def test_a_cap_below_the_per_key_one_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="cannot be below"):
        ConcurrencyLimiter(per_key=4, total=2)


# --------------------------------------------------------------------------- #
# Wiring: /v1/products/lookup
# --------------------------------------------------------------------------- #


def install(app: FastAPI, throttles: Throttles) -> None:
    app.state.throttles = throttles


def tight_throttles(
    *,
    lookups: int = 100,
    suggestions: int = 100,
    per_household: int = 4,
    total: int = 8,
) -> Throttles:
    return Throttles(
        recipe_suggestions=RateLimiter(limit=suggestions, window_seconds=3600.0),
        recipe_inferences=ConcurrencyLimiter(per_key=per_household, total=total),
        product_lookups=RateLimiter(limit=lookups, window_seconds=60.0),
    )


@pytest.fixture
def catalog(api_app: FastAPI) -> RecordingCatalog:
    double = RecordingCatalog({MILK_STORED: MILK_RECORD})
    api_app.dependency_overrides[get_catalog] = lambda: double
    return double


async def test_a_household_cannot_drain_the_shared_catalogue_budget(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    catalog: RecordingCatalog,
    make_household: MakeHousehold,
) -> None:
    """AUD-007: ten requests a minute used to take barcodes down for everyone.

    The Open Food Facts budget is instance-wide (ADR-0008), so the inbound limit
    has to bite before the outbound one does, and it has to bite per household.
    """
    install(api_app, tight_throttles(lookups=2))
    household = await make_household()
    other = await make_household()
    url = f"/v1/products/lookup?gtin={MILK_GTIN}"

    for _ in range(2):
        allowed = await api_client.get(url, headers=household_headers(household))
        assert allowed.status_code == 200, allowed.text

    refused = await api_client.get(url, headers=household_headers(household))
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/problem+json")
    assert refused.json()["type"].endswith("/rate-limited")
    assert int(refused.headers["Retry-After"]) >= 1

    served = await api_client.get(url, headers=household_headers(other))
    assert served.status_code == 200, "one household's flood must not throttle another"


# --------------------------------------------------------------------------- #
# Wiring: /v1/recipes/suggest
# --------------------------------------------------------------------------- #


async def test_recipe_suggestions_are_capped_per_household(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """AUD-008: the endpoint that spends real money had no ceiling at all."""
    install(api_app, tight_throttles(suggestions=1))
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")

    first = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )
    assert first.status_code == 200, first.text

    second = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )
    assert second.status_code == 429
    assert second.json()["type"].endswith("/rate-limited")
    assert int(second.headers["Retry-After"]) >= 1


async def test_a_second_tab_does_not_double_the_bill(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_location: MakeLocation,
    make_product: MakeProduct,
) -> None:
    """The concurrency slot, proved by holding it while a real request arrives.

    Occupying the limiter directly rather than racing two HTTP calls: the race
    would pass or fail on how fast the provider double answers, which is not the
    property under test.
    """
    throttles = tight_throttles(per_household=1, total=1)
    install(api_app, throttles)
    household = await make_household()
    await add_config(db_session, household)
    await stock_cream(api_client, household, make_location, make_product)
    use_provider_double(api_app, household, "nominal")

    with throttles.recipe_inferences.slot(str(household.id)):
        refused = await api_client.post(
            SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
        )
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1

    admitted = await api_client.post(
        SUGGEST_URL, headers=household_headers(household), json={"max_suggestions": 1}
    )
    assert admitted.status_code == 200, "the slot must be free again once the request ends"


async def test_an_unresolved_household_is_refused_before_any_budget_is_spent(
    api_app: FastAPI, api_client: httpx.AsyncClient
) -> None:
    """The limiter keys on a resolved household, so it can never be keyed on ``None``."""
    install(api_app, tight_throttles(suggestions=1, lookups=1))

    for _ in range(3):
        response = await api_client.post(SUGGEST_URL, json={"max_suggestions": 1})
        assert response.status_code == 401
