"""Populate a demonstration household with credible French pantry data.

Run from ``backend/`` against a migrated database::

    uv run alembic upgrade head
    uv run python scripts/seed.py

Every identifier is fixed, so the script is idempotent and so a screenshot, a
bookmark or a frontend fixture keeps working across re-seeds. Re-running updates
the demonstration rows in place and touches nothing else in the database.

The data is deliberately realistic and in French: these rows end up in
screenshots and in the frontend's development loop, and ``foo``/``bar`` in an
inventory screen makes the product impossible to judge. Expiry dates are
computed relative to the run date -- a fixed date would show a demonstration
entirely made of expired food within a fortnight.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from chaudron.config import ConfigurationError, get_settings
from chaudron.domain.models import (
    ExpiryDateKind,
    Household,
    InventoryLot,
    Product,
    ProductSource,
    QuantityDimension,
    StockEntrySource,
    StockMovement,
    StockMovementKind,
    StorageKind,
    StorageLocation,
    Unit,
)

logger = logging.getLogger("chaudron.seed")

# A fixed UUIDv7-shaped identifier. Not random: the frontend hardcodes it as the
# X-Household-Id of its development loop, and a value that changed per run would
# make every developer re-read it from the console every morning.
DEMO_HOUSEHOLD_ID = uuid.UUID("01991000-0000-7000-8000-000000000001")

_LOCATION_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000100")
_PRODUCT_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000200")
_LOT_NAMESPACE = uuid.UUID("01991000-0000-7000-8000-000000000300")


def _stable_id(namespace: uuid.UUID, name: str) -> uuid.UUID:
    """A deterministic identifier for a named demonstration row."""
    return uuid.uuid5(namespace, name)


@dataclass(frozen=True, slots=True)
class SeedLocation:
    key: str
    name: str
    kind: StorageKind
    sort_order: int


@dataclass(frozen=True, slots=True)
class SeedProduct:
    key: str
    name: str
    brand: str | None
    gtin: str | None
    default_unit: str
    category_tag: str | None = None


@dataclass(frozen=True, slots=True)
class SeedLot:
    product_key: str
    location_key: str
    amount: str
    unit: str
    #: Days from today. ``None`` means no expiry date at all, which is the normal
    #: case for flour, rice and anything decanted into a jar.
    expires_in_days: int | None
    expiry_kind: ExpiryDateKind
    source: StockEntrySource = StockEntrySource.MANUAL
    opened_days_ago: int | None = None


LOCATIONS: tuple[SeedLocation, ...] = (
    SeedLocation("fridge", "Frigo", StorageKind.FRIDGE, 10),
    SeedLocation("freezer", "Congélateur", StorageKind.FREEZER, 20),
    SeedLocation("pantry", "Placard", StorageKind.PANTRY, 30),
)

PRODUCTS: tuple[SeedProduct, ...] = (
    SeedProduct("lait", "Lait demi-écrémé UHT", "Lactel", "3033490004743", "l", "en:milks"),
    SeedProduct("beurre", "Beurre doux 250 g", "Président", "3155250353518", "g", "en:butters"),
    SeedProduct("yaourt", "Yaourt nature brassé", "Danone", "3033491401503", "piece", "en:yogurts"),
    SeedProduct("comte", "Comté affiné 12 mois", None, None, "g", "en:cheeses"),
    SeedProduct("oeufs", "Œufs plein air calibre moyen", "Loué", "3268840001008", "piece"),
    SeedProduct("poulet", "Filets de poulet fermier", None, None, "g", "en:chicken-breasts"),
    SeedProduct("saumon", "Pavés de saumon surgelés", "Findus", "3045140105502", "piece"),
    SeedProduct("petits-pois", "Petits pois extra-fins surgelés", "Bonduelle", None, "g"),
    SeedProduct("courgettes", "Courgettes du marché", None, None, "g", "en:courgettes"),
    SeedProduct("tomates", "Tomates grappe", None, None, "g", "en:tomatoes"),
    SeedProduct("pommes", "Pommes Gala", None, None, "piece", "en:apples"),
    SeedProduct("pates", "Penne rigate 500 g", "Barilla", "8076809513692", "g", "en:pastas"),
    SeedProduct("riz", "Riz basmati", "Taureau Ailé", "3016570100016", "g", "en:rices"),
    SeedProduct("farine", "Farine de blé T55", "Francine", "3242272371207", "g", "en:flours"),
    SeedProduct("huile", "Huile d'olive vierge extra", "Puget", "3021690043051", "ml"),
    SeedProduct("tomates-pelees", "Tomates pelées entières", "Mutti", "8005110120404", "g"),
    SeedProduct("lentilles", "Lentilles vertes du Puy", None, None, "g", "en:lentils"),
)

# A believable mid-week fridge: a couple of things about to go off, a lot of
# things with room to spare, and a few staples with no date at all.
LOTS: tuple[SeedLot, ...] = (
    SeedLot("lait", "fridge", "1", "l", 2, ExpiryDateKind.USE_BY, StockEntrySource.BARCODE_SCAN),
    SeedLot("lait", "fridge", "1", "l", 9, ExpiryDateKind.USE_BY, StockEntrySource.BARCODE_SCAN),
    SeedLot("beurre", "fridge", "250", "g", 21, ExpiryDateKind.BEST_BEFORE),
    SeedLot(
        "yaourt",
        "fridge",
        "4",
        "piece",
        1,
        ExpiryDateKind.USE_BY,
        StockEntrySource.BARCODE_SCAN,
    ),
    SeedLot("comte", "fridge", "220", "g", 12, ExpiryDateKind.BEST_BEFORE, opened_days_ago=3),
    SeedLot("oeufs", "fridge", "6", "piece", 11, ExpiryDateKind.BEST_BEFORE),
    SeedLot("poulet", "fridge", "450", "g", 2, ExpiryDateKind.USE_BY),
    SeedLot("courgettes", "fridge", "600", "g", 4, ExpiryDateKind.BEST_BEFORE),
    SeedLot("tomates", "fridge", "500", "g", 3, ExpiryDateKind.BEST_BEFORE),
    SeedLot("saumon", "freezer", "4", "piece", 180, ExpiryDateKind.BEST_BEFORE),
    SeedLot("petits-pois", "freezer", "750", "g", 240, ExpiryDateKind.BEST_BEFORE),
    SeedLot("pommes", "pantry", "6", "piece", None, ExpiryDateKind.UNKNOWN),
    SeedLot(
        "pates",
        "pantry",
        "500",
        "g",
        400,
        ExpiryDateKind.BEST_BEFORE,
        StockEntrySource.BARCODE_SCAN,
    ),
    SeedLot("riz", "pantry", "1", "kg", 500, ExpiryDateKind.BEST_BEFORE),
    SeedLot("farine", "pantry", "1", "kg", 120, ExpiryDateKind.BEST_BEFORE),
    SeedLot("huile", "pantry", "750", "ml", 300, ExpiryDateKind.BEST_BEFORE),
    SeedLot(
        "tomates-pelees",
        "pantry",
        "800",
        "g",
        600,
        ExpiryDateKind.BEST_BEFORE,
        StockEntrySource.RECEIPT_IMPORT,
    ),
    SeedLot("lentilles", "pantry", "500", "g", None, ExpiryDateKind.UNKNOWN),
)


async def _load_units(session: AsyncSession) -> dict[str, Unit]:
    units = {unit.code: unit for unit in await session.scalars(select(Unit))}
    if not units:
        raise RuntimeError(
            "the unit reference table is empty: run `uv run alembic upgrade head` first"
        )
    return units


async def _seed_household(session: AsyncSession) -> Household:
    household = await session.get(Household, DEMO_HOUSEHOLD_ID)
    if household is None:
        household = Household(id=DEMO_HOUSEHOLD_ID)
        session.add(household)
    household.name = "Foyer de démonstration"
    household.timezone = "Europe/Zurich"
    household.default_currency = "CHF"
    household.archived_at = None
    await session.flush()
    return household


async def _seed_locations(session: AsyncSession) -> dict[str, StorageLocation]:
    locations: dict[str, StorageLocation] = {}
    for spec in LOCATIONS:
        location_id = _stable_id(_LOCATION_NAMESPACE, spec.key)
        location = await session.get(StorageLocation, location_id)
        if location is None:
            location = StorageLocation(id=location_id, household_id=DEMO_HOUSEHOLD_ID)
            session.add(location)
        location.name = spec.name
        location.kind = spec.kind
        location.sort_order = spec.sort_order
        location.archived_at = None
        locations[spec.key] = location
    await session.flush()
    return locations


async def _seed_products(session: AsyncSession) -> dict[str, Product]:
    products: dict[str, Product] = {}
    for spec in PRODUCTS:
        product_id = _stable_id(_PRODUCT_NAMESPACE, spec.key)
        product = await session.get(Product, product_id)
        if product is None:
            product = Product(id=product_id)
            session.add(product)
        # Private to the demonstration household, even for products that carry a
        # real barcode: seeding them into the public catalogue would put
        # never-verified rows in the shared cache that real scans then read back.
        product.household_id = DEMO_HOUSEHOLD_ID
        product.name = spec.name
        product.brand = spec.brand
        product.gtin = None if spec.gtin is None else spec.gtin.rjust(14, "0")
        product.category_tag = spec.category_tag
        product.default_unit_code = spec.default_unit
        product.source = ProductSource.MANUAL
        product.archived_at = None
        products[spec.key] = product
    await session.flush()
    return products


async def _seed_lots(
    session: AsyncSession,
    units: dict[str, Unit],
    products: dict[str, Product],
    locations: dict[str, StorageLocation],
) -> int:
    today = date.today()  # noqa: DTZ011 - a calendar date, matching the column type
    count = 0
    for index, spec in enumerate(LOTS):
        unit = units[spec.unit]
        amount = Decimal(spec.amount)
        canonical = (amount * unit.factor_to_canonical).quantize(Decimal("0.001"))
        lot_id = _stable_id(_LOT_NAMESPACE, f"{spec.product_key}:{spec.location_key}:{index}")

        lot = await session.get(InventoryLot, lot_id)
        is_new = lot is None
        if lot is None:
            lot = InventoryLot(id=lot_id, household_id=DEMO_HOUSEHOLD_ID)
            session.add(lot)

        lot.product_id = products[spec.product_key].id
        lot.storage_location_id = locations[spec.location_key].id
        lot.quantity_value = amount
        lot.quantity_unit_code = unit.code
        lot.quantity_dimension = QuantityDimension(unit.dimension)
        lot.quantity_canonical = canonical
        lot.initial_quantity_canonical = canonical
        lot.best_before = (
            None if spec.expires_in_days is None else today + timedelta(days=spec.expires_in_days)
        )
        lot.date_kind = spec.expiry_kind
        lot.opened_at = (
            None if spec.opened_days_ago is None else today - timedelta(days=spec.opened_days_ago)
        )
        lot.entry_source = spec.source
        lot.depleted_at = None

        if is_new:
            # The ledger is the historical truth; a lot that appeared without a
            # movement would make the reconciliation job report drift on a
            # freshly seeded database.
            session.add(
                StockMovement(
                    id=_stable_id(_LOT_NAMESPACE, f"intake:{lot_id}"),
                    household_id=DEMO_HOUSEHOLD_ID,
                    inventory_lot_id=lot_id,
                    kind=StockMovementKind.INTAKE,
                    delta_canonical=canonical,
                    quantity_dimension=QuantityDimension(unit.dimension),
                    occurred_at=datetime.now(UTC),
                    reason="seed",
                )
            )
        count += 1
    await session.flush()
    return count


async def seed(session: AsyncSession) -> None:
    await _seed_household(session)
    units = await _load_units(session)
    locations = await _seed_locations(session)
    products = await _seed_products(session)
    lots = await _seed_lots(session, units, products, locations)
    logger.info(
        "seed_complete",
        extra={
            "household_id": str(DEMO_HOUSEHOLD_ID),
            "locations": len(locations),
            "products": len(products),
            "lots": lots,
        },
    )


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        return 2

    engine = create_async_engine(settings.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await seed(session)
    finally:
        await engine.dispose()

    logger.info("X-Household-Id: %s", DEMO_HOUSEHOLD_ID)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
