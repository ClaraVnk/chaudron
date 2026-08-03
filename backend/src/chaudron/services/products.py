"""Barcode resolution and manual product creation.

The resolution order is the one ADR-0008 makes mandatory rather than merely
sensible: cache first, external catalogue second, and every outcome -- including
"this code does not exist" -- written back to the cache.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from chaudron.domain.ports import (
    BarcodeNotFoundError,
    CatalogRecord,
    ProductCatalog,
    ProductCatalogUnavailableError,
    ProductDraft,
    ProductRepository,
    ProductView,
    RetailerInternalBarcodeError,
    UnitRegistry,
    UnknownUnitError,
    is_retailer_internal,
    normalize_gtin,
)

logger = logging.getLogger(__name__)


class ProductService:
    def __init__(
        self,
        products: ProductRepository,
        units: UnitRegistry,
        catalog: ProductCatalog,
        *,
        cache_ttl_seconds: int,
    ) -> None:
        self._products = products
        self._units = units
        self._catalog = catalog
        self._cache_ttl_seconds = cache_ttl_seconds

    async def lookup_by_barcode(self, gtin: str) -> ProductView:
        """Resolve a barcode to a catalogue entry.

        Raises :class:`RetailerInternalBarcodeError` for a variable-weight in-store
        code, :class:`BarcodeNotFoundError` when the catalogue says it does not exist,
        and :class:`ProductCatalogUnavailableError` when it said nothing at all.
        """
        normalized = normalize_gtin(gtin)
        if is_retailer_internal(normalized):
            raise RetailerInternalBarcodeError(gtin)

        cached = await self._products.find_cached(normalized)
        cached_view = cached[0] if cached is not None else None
        if cached is not None and self._is_fresh(cached[1]):
            if cached_view is None:
                raise BarcodeNotFoundError(gtin)
            return cached_view

        try:
            record = await self._catalog.lookup(normalized)
        except ProductCatalogUnavailableError:
            # A stale entry is worth more than an error: the cache exists partly
            # so that a household keeps resolving its usual groceries while Open
            # Food Facts is down or has banned us (ADR-0008).
            if cached_view is not None:
                logger.warning("catalog_unavailable_serving_stale", extra={"gtin": normalized})
                return cached_view
            raise

        if record is None:
            await self._products.remember_absent(normalized)
            raise BarcodeNotFoundError(gtin)

        return await self._products.upsert_public(await self._sanitise(record))

    async def create_manual(self, household_id: uuid.UUID, draft: ProductDraft) -> ProductView:
        """Create a product private to the household.

        Manual entry is a first-class path, not a fallback: a large share of a
        real cupboard has no usable barcode at all (ADR-0008).
        """
        unit_code = draft.default_unit_code
        if unit_code is not None and await self._units.get(unit_code) is None:
            raise UnknownUnitError(unit_code)

        gtin = None if draft.gtin is None else normalize_gtin(draft.gtin)
        return await self._products.create_private(
            household_id,
            ProductDraft(
                name=draft.name.strip(),
                brand=draft.brand.strip() if draft.brand else None,
                gtin=gtin,
                default_unit_code=draft.default_unit_code,
            ),
        )

    def _is_fresh(self, synced_at: datetime | None) -> bool:
        if synced_at is None:
            return False
        age = (datetime.now(UTC) - synced_at).total_seconds()
        return age < self._cache_ttl_seconds

    async def _sanitise(self, record: CatalogRecord) -> CatalogRecord:
        """Drop upstream values our reference tables cannot accept.

        ``net_content_unit_code`` is a foreign key onto ``unit``; Open Food Facts
        publishes free text there. Persisting it unchecked turns a scan of an
        unusual package into a 500 with a foreign-key violation in the log.
        """
        unit_code = record.net_content_unit_code
        if unit_code is not None and await self._units.get(unit_code) is None:
            return CatalogRecord(
                gtin=record.gtin,
                name=record.name,
                brand=record.brand,
                image_url=record.image_url,
                category_tag=record.category_tag,
                net_content_value=None,
                net_content_unit_code=None,
                payload=record.payload,
            )
        if unit_code is None and record.net_content_value is not None:
            return CatalogRecord(
                gtin=record.gtin,
                name=record.name,
                brand=record.brand,
                image_url=record.image_url,
                category_tag=record.category_tag,
                net_content_value=None,
                net_content_unit_code=None,
                payload=record.payload,
            )
        return record
