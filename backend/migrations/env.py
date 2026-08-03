"""Alembic environment.

The database URL comes from :class:`chaudron.config.Settings`, never from
``alembic.ini``: the migration runner and the API must be incapable of pointing
at different databases.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from chaudron.config import get_settings
from chaudron.domain.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The DSN to migrate, with ``%`` escaped for ConfigParser interpolation.

    An explicit ``sqlalchemy.url`` wins when a caller sets one programmatically
    -- the test suite does, against a throwaway container. ``alembic.ini`` does
    not define the key, so in every real deployment this falls through to the
    application's own configuration and the two cannot disagree.
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured.replace("%", "%%")
    return get_settings().database_url.get_secret_value().replace("%", "%%")


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without it, widening a numeric or swapping an enum is silently
        # invisible to autogenerate.
        compare_type=True,
        compare_server_default=True,
        # Constraint names come from the MetaData naming convention; Alembic must
        # use the same one or it emits DROPs against names it cannot reproduce.
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout, for a DBA who applies changes by hand."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_configure)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
