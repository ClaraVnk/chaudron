"""Dependency wiring: the transaction, the tenant, and the services.

Everything a handler needs arrives through this module, and nothing a handler
needs is built inside a handler. That is what lets the test suite swap the
session for one that rolls back, without the routes knowing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack
from typing import Annotated, Final

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.errors import rate_limited, unauthorized
from chaudron.api.throttling import AtCapacityError, Throttles
from chaudron.config import Settings
from chaudron.domain.ports import ProductCatalog
from chaudron.infra.crypto import CredentialCipher
from chaudron.infra.db import Database
from chaudron.infra.logging import household_id_var
from chaudron.infra.repositories import (
    SqlHouseholdRepository,
    SqlInventoryRepository,
    SqlLocationRepository,
    SqlProductRepository,
    SqlUnitRegistry,
)
from chaudron.services.inventory import InventoryService
from chaudron.services.locations import LocationService
from chaudron.services.products import ProductService
from chaudron.services.providers import (
    ProviderCredentialService,
    ProviderPortsBuilder,
    ProviderService,
    provider_ports_builder,
)
from chaudron.services.recipes import RecipeService

HOUSEHOLD_HEADER = "X-Household-Id"

#: The canonical 36-character lowercase form, and nothing else. ``uuid.UUID``
#: also accepts ``urn:uuid:…``, ``{…}`` and the unhyphenated digest, so three
#: spellings of one household reach the application today (audit AUD-026). That
#: is harmless while the value is only ever used as a :class:`uuid.UUID` -- and
#: stops being harmless the moment anything keys on the *string*, which the rate
#: limiter added alongside this now does. Normalising after the fact would work
#: just as well; refusing is shorter and leaves nothing to forget.
_CANONICAL_UUID: Final = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """One session per request, inside one transaction (see ``infra/db.py``)."""
    async with database.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_household_id(
    session: SessionDep,
    x_household_id: Annotated[str | None, Header(alias=HOUSEHOLD_HEADER)] = None,
) -> uuid.UUID:
    """Resolve the current household from a request header.

    PROVISIONAL, and documented as such in ``docs/api-contract-v1.md``. There are
    no user accounts in this slice, so the client names the household it wants
    and the server checks only that it exists. **Anyone who can reach the API can
    read any household by guessing a UUID.** It exists so the vertical slice is
    testable end to end before authentication is built.

    The shape of the contract does not change when a real session arrives: the
    household stays resolved server-side, and the header simply stops being
    consulted. Every call site already depends on *this function*, not on a
    header, so the replacement is one body.

    Absent, malformed and unknown all produce the *same* ``401`` body, byte for
    byte. The comment here used to claim that and the code contradicted it, which
    made the endpoint an oracle: a caller could confirm a household identifier
    with no observable side effect (audit AUD-013).
    """
    if not x_household_id or not _CANONICAL_UUID.match(x_household_id):
        raise unauthorized()
    household_id = uuid.UUID(x_household_id)

    if not await SqlHouseholdRepository(session).exists(household_id):
        raise unauthorized()

    household_id_var.set(str(household_id))
    return household_id


HouseholdDep = Annotated[uuid.UUID, Depends(get_household_id)]


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #


def get_throttles(request: Request) -> Throttles:
    throttles: Throttles = request.app.state.throttles
    return throttles


ThrottlesDep = Annotated[Throttles, Depends(get_throttles)]

# Both guards below are ``async def`` on purpose. A synchronous dependency is run
# by FastAPI in a worker *thread*, and the limiters' read-modify-write on a plain
# dictionary is only atomic because a single event loop never interleaves it.


async def enforce_product_lookup_limit(household_id: HouseholdDep, throttles: ThrottlesDep) -> None:
    """Keep one household from draining the instance's Open Food Facts budget.

    Counts *requests*, not upstream calls: a lookup served from the cache costs
    nothing to Open Food Facts, but counting only misses would let a household
    hammer the endpoint for free and still saturate the process. The limit is set
    below the instance-wide outbound budget so that a household exhausting its own
    allowance leaves some of the shared one for everybody else (ADR-0008).
    """
    try:
        throttles.product_lookups.acquire(str(household_id))
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "This household has made too many barcode lookups. The product catalogue "
                "budget is shared by the whole instance; retry shortly, or enter the "
                "product manually."
            ),
            retry_after=exc.retry_after,
        ) from None


async def enforce_recipe_limits(
    household_id: HouseholdDep, throttles: ThrottlesDep
) -> AsyncIterator[None]:
    """Bound both the rate and the concurrency of the endpoint that costs money.

    The rate cap is the wallet: every call is a billed inference. The concurrency
    slot is availability, and it is held for the whole request -- released by the
    ``finally`` of this generator once the response has been produced -- which is
    what makes "two tabs do not double the bill" true rather than aspirational.
    """
    key = str(household_id)
    try:
        throttles.recipe_suggestions.acquire(key)
    except AtCapacityError as exc:
        raise rate_limited(
            detail=(
                "This household has asked for too many recipe suggestions. Each one is a "
                "model call that costs tokens or local compute; retry later."
            ),
            retry_after=exc.retry_after,
        ) from None

    # An ExitStack rather than a `with` around the `yield`: FastAPI throws an
    # endpoint's exception back in at the yield point, and a bare `except
    # AtCapacityError` there would mistranslate an unrelated failure into a 429.
    with ExitStack() as stack:
        try:
            stack.enter_context(throttles.recipe_inferences.slot(key))
        except AtCapacityError as exc:
            raise rate_limited(
                detail=(
                    "Too many recipe suggestions are already being generated. Wait for the "
                    "one in flight to finish before asking for another."
                ),
                retry_after=exc.retry_after,
            ) from None
        yield


def get_catalog(request: Request) -> ProductCatalog:
    catalog: ProductCatalog = request.app.state.catalog
    return catalog


def get_location_service(session: SessionDep) -> LocationService:
    return LocationService(SqlLocationRepository(session))


def get_inventory_service(session: SessionDep) -> InventoryService:
    return InventoryService(
        SqlInventoryRepository(session),
        SqlProductRepository(session),
        SqlLocationRepository(session),
        SqlUnitRegistry(session),
    )


def get_product_service(
    session: SessionDep,
    catalog: Annotated[ProductCatalog, Depends(get_catalog)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ProductService:
    return ProductService(
        SqlProductRepository(session),
        SqlUnitRegistry(session),
        catalog,
        cache_ttl_seconds=settings.off_cache_ttl_seconds,
    )


def get_credential_cipher(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> CredentialCipher:
    """The one object holding the master key, built from the validated settings.

    Cheap to construct (a key schedule), so there is no cache to invalidate and no
    key material parked on ``app.state`` for the lifetime of the process.
    """
    return CredentialCipher.from_settings(settings)


CipherDep = Annotated[CredentialCipher, Depends(get_credential_cipher)]


def get_provider_ports_builder(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cipher: CipherDep,
) -> ProviderPortsBuilder:
    """The seam the tests replace to exercise the real adapters over a fake socket.

    A builder rather than a factory: which instance-owned key applies depends on the
    provider the household chose, and that is only known once its configuration is
    read.
    """
    return provider_ports_builder(settings, cipher)


def get_provider_service(
    session: SessionDep,
    build_ports: Annotated[ProviderPortsBuilder, Depends(get_provider_ports_builder)],
    cipher: CipherDep,
) -> ProviderService:
    return ProviderService(session, build_ports, cipher)


def get_provider_credential_service(
    session: SessionDep, cipher: CipherDep
) -> ProviderCredentialService:
    return ProviderCredentialService(session, cipher)


def get_recipe_service(
    session: SessionDep,
    providers: Annotated[ProviderService, Depends(get_provider_service)],
) -> RecipeService:
    return RecipeService(
        session,
        providers,
        SqlInventoryRepository(session),
        SqlLocationRepository(session),
    )


LocationServiceDep = Annotated[LocationService, Depends(get_location_service)]
InventoryServiceDep = Annotated[InventoryService, Depends(get_inventory_service)]
ProductServiceDep = Annotated[ProductService, Depends(get_product_service)]
ProviderServiceDep = Annotated[ProviderService, Depends(get_provider_service)]
ProviderCredentialServiceDep = Annotated[
    ProviderCredentialService, Depends(get_provider_credential_service)
]
RecipeServiceDep = Annotated[RecipeService, Depends(get_recipe_service)]
