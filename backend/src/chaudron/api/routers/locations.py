"""Storage locations."""

from __future__ import annotations

from fastapi import APIRouter

from chaudron.api.deps import HouseholdDep, LocationServiceDep
from chaudron.api.schemas import LocationOut

router = APIRouter(prefix="/v1/locations", tags=["locations"])


@router.get("", response_model=list[LocationOut], summary="List storage locations")
async def list_locations(
    household_id: HouseholdDep, service: LocationServiceDep
) -> list[LocationOut]:
    locations = await service.list_locations(household_id)
    return [
        LocationOut(
            id=location.id,
            name=location.name,
            kind=location.kind,
            item_count=location.item_count,
        )
        for location in locations
    ]
