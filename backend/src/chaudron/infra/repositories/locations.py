"""Storage locations and their live item counts."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import InventoryLot, StorageLocation
from chaudron.domain.ports import LocationSummary


class SqlLocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_with_counts(self, household_id: uuid.UUID) -> Sequence[LocationSummary]:
        """Locations with the number of lots currently in them.

        A correlated scalar subquery rather than a ``LEFT JOIN … GROUP BY``: it
        keeps the count on the partial index ``ix_inventory_lot_location_active``
        and leaves the outer query returning exactly one row per location, with
        no aggregate over the location columns.
        """
        item_count = (
            select(func.count())
            .select_from(InventoryLot)
            .where(
                InventoryLot.household_id == household_id,
                InventoryLot.storage_location_id == StorageLocation.id,
                InventoryLot.depleted_at.is_(None),
            )
            .scalar_subquery()
        )

        rows = await self._session.execute(
            select(
                StorageLocation.id,
                StorageLocation.name,
                StorageLocation.kind,
                item_count.label("item_count"),
            )
            .where(
                StorageLocation.household_id == household_id,
                StorageLocation.archived_at.is_(None),
            )
            .order_by(StorageLocation.sort_order, StorageLocation.name)
        )
        return [
            LocationSummary(id=row.id, name=row.name, kind=row.kind, item_count=row.item_count)
            for row in rows
        ]

    async def exists(self, household_id: uuid.UUID, location_id: uuid.UUID) -> bool:
        found = await self._session.scalar(
            select(StorageLocation.id).where(
                StorageLocation.id == location_id,
                StorageLocation.household_id == household_id,
                StorageLocation.archived_at.is_(None),
            )
        )
        return found is not None
