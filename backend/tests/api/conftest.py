"""Fixtures for the HTTP tests: rows the API cannot create, and a catalogue double."""

from __future__ import annotations

import uuid
from typing import Protocol

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Household, Product, ProductSource, StorageKind, StorageLocation
from chaudron.domain.ports import CatalogRecord


class MakeLocation(Protocol):
    async def __call__(
        self,
        household: Household,
        *,
        name: str = ...,
        kind: StorageKind = ...,
    ) -> StorageLocation: ...


class MakeProduct(Protocol):
    async def __call__(
        self,
        *,
        name: str = ...,
        household: Household | None = ...,
        brand: str | None = ...,
        gtin: str | None = ...,
    ) -> Product: ...


@pytest.fixture
def make_location(db_session: AsyncSession) -> MakeLocation:
    async def _make_location(
        household: Household,
        *,
        name: str = "Frigo",
        kind: StorageKind = StorageKind.FRIDGE,
    ) -> StorageLocation:
        location = StorageLocation(household_id=household.id, name=name, kind=kind)
        db_session.add(location)
        await db_session.flush()
        return location

    return _make_location


@pytest.fixture
def make_product(db_session: AsyncSession) -> MakeProduct:
    async def _make_product(
        *,
        name: str = "Lait demi-écrémé",
        household: Household | None = None,
        brand: str | None = "Lactel",
        gtin: str | None = None,
    ) -> Product:
        product = Product(
            household_id=None if household is None else household.id,
            name=name,
            brand=brand,
            gtin=gtin,
            source=ProductSource.MANUAL,
        )
        db_session.add(product)
        await db_session.flush()
        return product

    return _make_product


class RecordingCatalog:
    """A :class:`ProductCatalog` double that answers from a script.

    The real client is exercised separately; here the point is the *service*
    behaviour around it -- caching, negative caching, and serving a stale entry
    when the catalogue is down -- which needs a controllable upstream and no
    network.
    """

    def __init__(self, answers: dict[str, CatalogRecord | Exception | None]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def lookup(self, gtin: str) -> CatalogRecord | None:
        self.calls.append(gtin)
        answer = self.answers.get(gtin)
        if isinstance(answer, Exception):
            raise answer
        return answer


def new_uuid() -> uuid.UUID:
    return uuid.uuid7()
