"""Async engine, session factory, and the one place a transaction begins.

**One HTTP request is one transaction.** :meth:`Database.session` opens it,
commits on a clean return and rolls back on any exception. Nothing else in the
application calls ``begin()``, and no handler commits on its own.

That is not a stylistic preference. It is the prerequisite listed in
``docs/security-review-baseline.md`` (SEC-001) for row-level security: the
tenant is posted with ``SET LOCAL app.household_id``, which PostgreSQL discards
at ``COMMIT``. A request spread over two transactions would either lose the
setting halfway through or leak it into a recycled connection -- and the whole
mechanism is worthless if either can happen. Paying the discipline now costs a
context manager; retrofitting it later costs an audit of every handler.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from chaudron.config import Settings


class Database:
    """Owns the engine and hands out request-scoped sessions."""

    def __init__(self, settings: Settings) -> None:
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # A connection dropped by a restarted database otherwise surfaces as
            # a failed user request instead of a transparent reconnect.
            pool_pre_ping=True,
            echo=False,
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=True,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """One session, one transaction, committed or rolled back as a whole."""
        async with self._sessionmaker() as session, session.begin():
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()
