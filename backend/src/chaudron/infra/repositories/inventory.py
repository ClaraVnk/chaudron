"""Inventory lots: the listing query, the merge lookup, and the stock ledger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from chaudron.domain.models import (
    FoodFamily,
    InventoryLot,
    Product,
    QuantityDimension,
    ShelfLifeGuideline,
    StockMovement,
    StockMovementKind,
    StorageLocation,
)
from chaudron.domain.ports import (
    UNSET,
    InventoryConflictError,
    InventoryFilter,
    InventoryItem,
    InventoryItemNotFoundError,
    InventoryPage,
    LocationView,
    LotDraft,
    LotFreezingState,
    LotState,
    LotUpdate,
    ProductView,
)
from chaudron.domain.shelf_life import effective_expiry_sql, join_shelf_life_guideline

#: Label the computed column carries, so ``_row_to_item`` reads it by name rather
#: than by position.
_EFFECTIVE_EXPIRY: Final = "effective_expiry"

#: Label the guideline's freezing advice carries. Selected as a bare column rather
#: than by mapping the whole ``ShelfLifeGuideline`` entity: the join is a ``LEFT``
#: one, so there is frequently no row, and a projection of one column keeps the
#: "no family, no advice" case a ``None`` instead of a missing object.
_FREEZING_NOTE: Final = "freezing_note"


def _row_to_item(row: Any) -> InventoryItem:
    lot: InventoryLot = row.InventoryLot
    product: Product = row.Product
    location: StorageLocation | None = row.StorageLocation
    return InventoryItem(
        id=lot.id,
        product=ProductView(
            id=product.id,
            name=product.name,
            brand=product.brand,
            gtin=product.gtin,
            image_url=product.image_url,
            default_unit_code=product.default_unit_code,
            category_tag=product.category_tag,
            is_public=product.household_id is None,
        ),
        location=(
            None
            if location is None
            else LocationView(id=location.id, name=location.name, kind=location.kind)
        ),
        quantity_value=lot.quantity_value,
        quantity_unit_code=lot.quantity_unit_code,
        best_before=lot.best_before,
        date_kind=lot.date_kind,
        opened_at=lot.opened_at,
        effective_expiry=getattr(row, _EFFECTIVE_EXPIRY),
        entry_source=lot.entry_source,
        created_at=lot.created_at,
        frozen_at=lot.frozen_at,
        thawed_at=lot.thawed_at,
        freezing_note=getattr(row, _FREEZING_NOTE),
    )


class SqlInventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Reads ------------------------------------------------------------- #

    def _base_query(self, household_id: uuid.UUID) -> Select[Any]:
        return join_shelf_life_guideline(
            select(
                InventoryLot,
                Product,
                StorageLocation,
                effective_expiry_sql().label(_EFFECTIVE_EXPIRY),
                # Free: ``join_shelf_life_guideline`` already brought the row in
                # for ``effective_expiry_sql``, so the advice costs one more
                # column on a join the listing was paying for anyway.
                ShelfLifeGuideline.freezing_note.label(_FREEZING_NOTE),
            )
            .join(Product, Product.id == InventoryLot.product_id)
            .outerjoin(StorageLocation, StorageLocation.id == InventoryLot.storage_location_id)
        ).where(
            InventoryLot.household_id == household_id,
            InventoryLot.depleted_at.is_(None),
        )

    async def list_items(self, household_id: uuid.UUID, criteria: InventoryFilter) -> InventoryPage:
        conditions = []
        if criteria.location_id is not None:
            conditions.append(InventoryLot.storage_location_id == criteria.location_id)
        if criteria.query:
            # Backed by ix_product_name_trgm; without pg_trgm this degrades to a
            # sequential scan rather than an error, which is the wrong kind of
            # quiet -- hence the extension being created by the first migration.
            pattern = f"%{criteria.query}%"
            conditions.append(Product.name.ilike(pattern) | Product.brand.ilike(pattern))
        if criteria.expiring_within_days is not None:
            # Against the effective date, not the printed one: a jar opened three
            # weeks ago is what this filter exists to surface, and its printed
            # date has not moved (docs/data-model.md 7.4).
            horizon = datetime.now(UTC).date() + timedelta(days=criteria.expiring_within_days)
            expiry = effective_expiry_sql()
            conditions.append(expiry.is_not(None))
            conditions.append(expiry <= horizon)

        # The count joins the guideline too: it has to answer for exactly the
        # rows the page below returns, and `conditions` may now reference it.
        total = await self._session.scalar(
            join_shelf_life_guideline(
                select(func.count())
                .select_from(InventoryLot)
                .join(Product, Product.id == InventoryLot.product_id)
            ).where(
                InventoryLot.household_id == household_id,
                InventoryLot.depleted_at.is_(None),
                *conditions,
            )
        )

        rows = await self._session.execute(
            self._base_query(household_id)
            .where(*conditions)
            # What expires first comes first; dateless lots sink to the bottom
            # rather than to the top, where a NULL would otherwise sort in
            # PostgreSQL's default ascending order.
            .order_by(
                effective_expiry_sql().asc().nullslast(),
                InventoryLot.created_at.desc(),
                InventoryLot.id,
            )
            .limit(criteria.limit)
            .offset(criteria.offset)
        )
        return InventoryPage(total=total or 0, items=[_row_to_item(row) for row in rows])

    async def get_item(self, household_id: uuid.UUID, item_id: uuid.UUID) -> InventoryItem | None:
        row = (
            await self._session.execute(
                self._base_query(household_id).where(InventoryLot.id == item_id)
            )
        ).first()
        return None if row is None else _row_to_item(row)

    async def get_state(self, household_id: uuid.UUID, item_id: uuid.UUID) -> LotState | None:
        lot = await self._session.scalar(
            select(InventoryLot)
            .where(
                InventoryLot.id == item_id,
                InventoryLot.household_id == household_id,
                InventoryLot.depleted_at.is_(None),
            )
            .with_for_update()
        )
        return None if lot is None else _to_state(lot)

    async def get_freezing_state(
        self, household_id: uuid.UUID, item_id: uuid.UUID
    ) -> LotFreezingState | None:
        """The lot, its computed date, and its family's freezing figures, locked.

        Two properties of this query are load-bearing.

        *The date is read, not recomputed.* ``effective_expiry_sql`` is the same
        expression the listing sorts on and the ``expiring_within_days`` filter
        compares against, so "this lot is already expired" cannot mean one thing
        to the freeze refusal and another to the screen the user was looking at
        when they tapped it.

        *``with_for_update(of=...)`` names the lot.* The statement joins two
        tables the writer never touches, and PostgreSQL refuses ``FOR UPDATE`` on
        the nullable side of an outer join outright. Locking the lot row alone is
        also the correct scope: ``shelf_life_guideline`` is published reference
        data, and taking a row lock on it would serialise every household that
        happens to be freezing meat at the same moment.
        """
        row = (
            await self._session.execute(
                join_shelf_life_guideline(
                    select(
                        InventoryLot,
                        Product.food_family,
                        ShelfLifeGuideline.home_frozen_days,
                        ShelfLifeGuideline.freezing_note,
                        effective_expiry_sql().label(_EFFECTIVE_EXPIRY),
                    ).join(Product, Product.id == InventoryLot.product_id)
                )
                .where(
                    InventoryLot.id == item_id,
                    InventoryLot.household_id == household_id,
                    InventoryLot.depleted_at.is_(None),
                )
                .with_for_update(of=InventoryLot)
            )
        ).first()
        if row is None:
            return None
        lot: InventoryLot = row.InventoryLot
        return LotFreezingState(
            id=lot.id,
            product_id=lot.product_id,
            storage_location_id=lot.storage_location_id,
            frozen_at=lot.frozen_at,
            thawed_at=lot.thawed_at,
            effective_expiry=getattr(row, _EFFECTIVE_EXPIRY),
            home_frozen_days=row.home_frozen_days,
            freezing_note=row.freezing_note,
            sold_frozen=row.food_family is FoodFamily.FROZEN,
        )

    async def location_holds_twin(
        self, household_id: uuid.UUID, item_id: uuid.UUID, location_id: uuid.UUID
    ) -> bool:
        """Whether moving this lot to ``location_id`` would collide with the merge key.

        ``uq_inventory_lot_merge_key`` is ``(household, product, location,
        best_before, dimension)`` over active rows, so a *move* can violate it
        exactly as an insert can -- and it is not a hypothetical: freeze one pack
        of chicken on Monday, buy another with the same printed date on Tuesday,
        and Wednesday's freeze would land on top of Monday's row.

        Asked in advance rather than caught afterwards, because the caught version
        is worse in both directions: an ``IntegrityError`` aborts the request's
        transaction, and the ``409`` it produces tells the household to retry
        something that will fail again identically.

        The lot's own merge-key columns are read in the same statement, from a
        second alias, rather than carried in from Python -- one round trip, and
        no window in which the caller's copy of ``best_before`` is stale.
        """
        source = aliased(InventoryLot)
        found = await self._session.scalar(
            select(InventoryLot.id)
            .where(
                source.id == item_id,
                source.household_id == household_id,
                InventoryLot.household_id == household_id,
                InventoryLot.id != item_id,
                InventoryLot.depleted_at.is_(None),
                InventoryLot.storage_location_id == location_id,
                InventoryLot.product_id == source.product_id,
                # ``NULLS NOT DISTINCT`` on the index, so two dateless lots do
                # collide; ``IS NOT DISTINCT FROM`` is the predicate that agrees
                # with it, and a plain ``=`` would miss exactly that case.
                InventoryLot.best_before.is_not_distinct_from(source.best_before),
                InventoryLot.quantity_dimension == source.quantity_dimension,
            )
            .limit(1)
        )
        return found is not None

    async def find_mergeable(self, household_id: uuid.UUID, draft: LotDraft) -> LotState | None:
        """The active lot this draft would collide with on ``uq_inventory_lot_merge_key``.

        Locked on read: two phones scanning the same pack at the same instant
        must serialise here, or the second one hits the unique index and fails a
        request the user has no way to interpret.
        """
        lot = await self._session.scalar(
            select(InventoryLot)
            .where(
                InventoryLot.household_id == household_id,
                InventoryLot.product_id == draft.product_id,
                InventoryLot.storage_location_id.is_not_distinct_from(draft.storage_location_id),
                InventoryLot.best_before.is_not_distinct_from(draft.best_before),
                InventoryLot.quantity_dimension == draft.quantity_dimension,
                InventoryLot.depleted_at.is_(None),
            )
            .with_for_update()
        )
        return None if lot is None else _to_state(lot)

    # -- Writes ------------------------------------------------------------ #

    async def add(self, household_id: uuid.UUID, draft: LotDraft) -> InventoryItem:
        lot = InventoryLot(
            household_id=household_id,
            product_id=draft.product_id,
            storage_location_id=draft.storage_location_id,
            quantity_value=draft.quantity_value,
            quantity_unit_code=draft.quantity_unit_code,
            quantity_dimension=draft.quantity_dimension,
            quantity_canonical=draft.quantity_canonical,
            initial_quantity_canonical=draft.quantity_canonical,
            best_before=draft.best_before,
            date_kind=draft.date_kind,
            opened_at=draft.opened_at,
            entry_source=draft.entry_source,
            source_receipt_line_id=draft.source_receipt_line_id,
        )
        self._session.add(lot)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise InventoryConflictError(
                "another write created a matching lot concurrently"
            ) from exc

        item = await self.get_item(household_id, lot.id)
        if item is None:  # pragma: no cover - the row was just flushed
            raise InventoryItemNotFoundError(lot.id)
        return item

    async def apply(
        self, household_id: uuid.UUID, item_id: uuid.UUID, update: LotUpdate
    ) -> InventoryItem:
        lot = await self._session.scalar(
            select(InventoryLot).where(
                InventoryLot.id == item_id,
                InventoryLot.household_id == household_id,
                InventoryLot.depleted_at.is_(None),
            )
        )
        if lot is None:
            raise InventoryItemNotFoundError(item_id)

        for field in (
            "quantity_value",
            "quantity_unit_code",
            "quantity_dimension",
            "quantity_canonical",
            "storage_location_id",
            "best_before",
            "date_kind",
            "opened_at",
            "frozen_at",
            "thawed_at",
        ):
            value = getattr(update, field)
            if value is not UNSET:
                setattr(lot, field, value)

        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise InventoryConflictError("the change collides with an existing lot") from exc

        item = await self.get_item(household_id, item_id)
        if item is None:  # pragma: no cover - the row was just flushed
            raise InventoryItemNotFoundError(item_id)
        return item

    async def deplete(self, household_id: uuid.UUID, item_id: uuid.UUID) -> None:
        """Mark the lot as gone without deleting it.

        ``quantity_value`` keeps its last value: ``ck_inventory_lot_quantity_positive``
        requires it to stay strictly positive, and the history reads better for
        it -- "one litre, consumed" rather than "zero of something".
        """
        lot = await self._session.scalar(
            select(InventoryLot).where(
                InventoryLot.id == item_id,
                InventoryLot.household_id == household_id,
                InventoryLot.depleted_at.is_(None),
            )
        )
        if lot is None:
            raise InventoryItemNotFoundError(item_id)
        lot.quantity_canonical = Decimal(0)
        lot.depleted_at = datetime.now(UTC)
        await self._session.flush()

    async def record_movement(
        self,
        household_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        kind: StockMovementKind,
        delta_canonical: Decimal,
        dimension: QuantityDimension,
        reason: str | None,
    ) -> None:
        self._session.add(
            StockMovement(
                household_id=household_id,
                inventory_lot_id=item_id,
                kind=kind,
                delta_canonical=delta_canonical,
                quantity_dimension=dimension,
                reason=reason,
            )
        )
        await self._session.flush()


def _to_state(lot: InventoryLot) -> LotState:
    return LotState(
        id=lot.id,
        product_id=lot.product_id,
        quantity_value=lot.quantity_value,
        quantity_unit_code=lot.quantity_unit_code,
        quantity_dimension=lot.quantity_dimension,
        quantity_canonical=lot.quantity_canonical,
        storage_location_id=lot.storage_location_id,
        best_before=lot.best_before,
        date_kind=lot.date_kind,
        opened_at=lot.opened_at,
    )
