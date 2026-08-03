"""The ``unit`` reference table, seeded by an Alembic migration."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import Unit
from chaudron.domain.ports import UnitInfo


class SqlUnitRegistry:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # The table is tiny, immutable within a request, and read once per
        # quantity written; a request that adds ten items should not issue ten
        # identical queries.
        self._cache: dict[str, UnitInfo | None] = {}

    async def get(self, code: str) -> UnitInfo | None:
        if code in self._cache:
            return self._cache[code]
        row = await self._session.scalar(select(Unit).where(Unit.code == code))
        info = (
            None
            if row is None
            else UnitInfo(
                code=row.code,
                dimension=row.dimension,
                factor_to_canonical=row.factor_to_canonical,
                symbol=row.symbol,
            )
        )
        self._cache[code] = info
        return info
