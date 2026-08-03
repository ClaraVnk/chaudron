"""Household lookups."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Household


class SqlHouseholdRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, household_id: uuid.UUID) -> bool:
        """True for a live household. An archived one is treated as absent.

        Archiving is how a household leaves the instance; letting its identifier
        keep opening the door would make the operation cosmetic.
        """
        found = await self._session.scalar(
            select(Household.id).where(
                Household.id == household_id,
                Household.archived_at.is_(None),
            )
        )
        return found is not None
