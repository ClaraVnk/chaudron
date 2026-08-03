"""Dependency wiring: the transaction, the tenant, and the services.

Everything a handler needs arrives through this module, and nothing a handler
needs is built inside a handler. That is what lets the test suite swap the
session for one that rolls back, without the routes knowing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.errors import unauthorized
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
    """
    if not x_household_id:
        raise unauthorized(f"The {HOUSEHOLD_HEADER} header is required.")
    try:
        household_id = uuid.UUID(x_household_id)
    except ValueError:
        raise unauthorized(f"The {HOUSEHOLD_HEADER} header is not a valid UUID.") from None

    if not await SqlHouseholdRepository(session).exists(household_id):
        # Same answer as a malformed header on purpose: distinguishing "unknown"
        # from "invalid" would turn this endpoint into a household oracle.
        raise unauthorized(f"The {HOUSEHOLD_HEADER} header does not designate a known household.")

    household_id_var.set(str(household_id))
    return household_id


HouseholdDep = Annotated[uuid.UUID, Depends(get_household_id)]


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
