"""Storage locations use cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from chaudron.domain.ports import LocationRepository, LocationSummary


class LocationService:
    def __init__(self, locations: LocationRepository) -> None:
        self._locations = locations

    async def list_locations(self, household_id: uuid.UUID) -> Sequence[LocationSummary]:
        return await self._locations.list_with_counts(household_id)
