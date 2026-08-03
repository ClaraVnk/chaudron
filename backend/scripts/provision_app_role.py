"""Create the non-owner PostgreSQL role the application connects as.

Row-level security has one failure mode worth more than all the others: a
policy that is perfectly written and never applies. PostgreSQL exempts three
kinds of connection from every policy -- a superuser, a role holding
``BYPASSRLS``, and **the owner of the table**, unless ``FORCE ROW LEVEL
SECURITY`` is set. Migration ``0004`` deliberately leaves ``FORCE`` off, because
the owner is the identity Alembic, ``scripts/seed.py`` and a DBA's psql prompt
use, and forcing policies on it would mean every maintenance statement has to
post a tenant first.

So the whole guarantee reduces to one sentence: **the application must not
connect as the owner.** This script provisions the role that satisfies it, and
refuses to pretend it succeeded when it cannot.

Run from ``backend/``, against a database already at ``head``::

    read -rs -p 'App role password: ' CHAUDRON_DB_APP_PASSWORD && export CHAUDRON_DB_APP_PASSWORD
    uv run python scripts/provision_app_role.py
    unset CHAUDRON_DB_APP_PASSWORD

``CHAUDRON_DATABASE_URL`` must name the **owner** for this run -- creating a role
and granting on someone else's tables is exactly what the application role must
not be able to do. Afterwards, point the application at the new role and keep the
owner DSN for migrations only.

Idempotent: re-running grants what is missing, resets the password when one is
supplied, and reports. ``--check`` reports without writing anything, which is
what a deployment pipeline should run after every migration.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from typing import Final

from sqlalchemy import Row, make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from chaudron.config import ConfigurationError, get_settings

logger = logging.getLogger("chaudron.provision")

#: Same shape PostgreSQL accepts unquoted, so the role name never needs quoting
#: gymnastics in a DSN, a quadlet or a psql prompt. Everything else is refused
#: rather than escaped: a role named ``"; drop"`` is a mistake, not a use case.
_ROLE_NAME: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_DEFAULT_ROLE: Final = "chaudron_app"

#: The function every policy calls. Executable by PUBLIC by default; granted
#: explicitly so a hardened database that revoked PUBLIC still works.
_TENANT_FUNCTION: Final = "chaudron_current_household()"


def _role_name() -> str:
    name = os.environ.get("CHAUDRON_DB_APP_ROLE", _DEFAULT_ROLE)
    if not _ROLE_NAME.match(name):
        raise ValueError(
            f"CHAUDRON_DB_APP_ROLE={name!r} is not a plain lowercase identifier "
            f"(letters, digits and underscore, not starting with a digit)"
        )
    return name


async def _sql(connection: AsyncConnection, template: str, /, **params: str) -> str:
    """Build one statement server-side with ``format()``, then execute the result.

    ``%I`` and ``%L`` are PostgreSQL's own identifier and literal quoting, which
    is the only quoting that is right by construction -- and the only way to get
    a password into DDL, since ``ALTER ROLE ... PASSWORD`` takes no bind
    parameter. Arguments are substituted in keyword order, which is the order
    they appear in *template*.

    The casts are not decoration: ``format()`` is variadic ``any``, so without
    them PostgreSQL refuses the statement with "could not determine data type of
    parameter $1".
    """
    arguments = "".join(f", cast(:{name} as text)" for name in params)
    built = await connection.scalar(
        text(f"SELECT format(cast(:chaudron_format as text){arguments})"),
        {"chaudron_format": template, **params},
    )
    if not isinstance(built, str):  # pragma: no cover - format() always returns text
        raise RuntimeError(f"could not build statement from {template!r}")
    return built


async def _role_exists(connection: AsyncConnection, role: str) -> bool:
    found = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    return found is not None


async def _describe_role(connection: AsyncConnection, role: str) -> Row[tuple[bool, bool, bool]]:
    return (
        await connection.execute(
            text(
                """
                SELECT rolsuper AS is_superuser,
                       rolbypassrls AS bypasses_rls,
                       rolcanlogin AS can_login
                FROM pg_roles
                WHERE rolname = :role
                """
            ),
            {"role": role},
        )
    ).one()


async def provision(connection: AsyncConnection, role: str, password: str | None) -> None:
    """Create or update the role, then grant it exactly what the API needs."""
    database = await connection.scalar(text("SELECT current_database()"))
    owner = await connection.scalar(text("SELECT current_user"))

    attributes = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN"
    if await _role_exists(connection, role):
        logger.info("role %s already exists", role)
        await connection.execute(
            text(await _sql(connection, f"ALTER ROLE %I {attributes}", role=role))
        )
    else:
        await connection.execute(
            text(await _sql(connection, f"CREATE ROLE %I {attributes}", role=role))
        )
        logger.info("created role %s", role)

    if password is not None:
        await connection.execute(
            text(await _sql(connection, "ALTER ROLE %I PASSWORD %L", role=role, pw=password))
        )
        logger.info("password set on %s", role)

    statements: list[tuple[str, dict[str, str]]] = [
        ("GRANT CONNECT ON DATABASE %I TO %I", {"db": str(database), "role": role}),
        ("GRANT USAGE ON SCHEMA public TO %I", {"role": role}),
        # No CREATE: a role that can add a table to the schema can add one
        # without a policy, and then read it from the application.
        ("REVOKE CREATE ON SCHEMA public FROM %I", {"role": role}),
        (
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I",
            {"role": role},
        ),
        ("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I", {"role": role}),
        (f"GRANT EXECUTE ON FUNCTION {_TENANT_FUNCTION} TO %I", {"role": role}),
        # So the next migration's tables are granted without re-running this.
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I",
            {"owner": str(owner), "role": role},
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO %I",
            {"owner": str(owner), "role": role},
        ),
    ]
    for template, params in statements:
        await connection.execute(text(await _sql(connection, template, **params)))
    logger.info("granted read/write on %s to %s, owning nothing", database, role)


async def report(connection: AsyncConnection, role: str) -> list[str]:
    """Everything that would make the policies not apply to *role*. Empty is good."""
    problems: list[str] = []
    if not await _role_exists(connection, role):
        return [f"role {role} does not exist: run this script without --check"]

    attributes = await _describe_role(connection, role)
    if attributes.is_superuser:
        problems.append(f"{role} is a superuser: it bypasses every policy")
    if attributes.bypasses_rls:
        problems.append(f"{role} holds BYPASSRLS: it bypasses every policy")
    if not attributes.can_login:
        problems.append(f"{role} cannot log in: the application will not connect")

    owned = (
        await connection.scalars(
            text(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                  AND c.relrowsecurity
                  AND NOT c.relforcerowsecurity
                  AND pg_catalog.pg_get_userbyid(c.relowner) = :role
                ORDER BY c.relname
                """
            ),
            {"role": role},
        )
    ).all()
    if owned:
        problems.append(
            f"{role} owns {', '.join(owned)}: an owner bypasses its own policies "
            f"unless FORCE ROW LEVEL SECURITY is set"
        )

    unprotected = (
        await connection.scalars(
            text(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                  AND NOT c.relrowsecurity
                  AND EXISTS (
                      SELECT 1 FROM pg_attribute a
                      WHERE a.attrelid = c.oid
                        AND a.attname = 'household_id'
                        AND NOT a.attisdropped
                  )
                ORDER BY c.relname
                """
            )
        )
    ).all()
    if unprotected:
        problems.append(
            f"tables carrying household_id with no row-level security: {', '.join(unprotected)}: "
            f"is the database at revision 0004?"
        )

    missing = (
        await connection.scalars(
            text(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                  AND c.relrowsecurity
                  AND NOT has_table_privilege(:role, c.oid, 'SELECT')
                ORDER BY c.relname
                """
            ),
            {"role": role},
        )
    ).all()
    if missing:
        problems.append(
            f"{role} cannot read {', '.join(missing)}: re-run this script without --check"
        )
    return problems


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report only; make no change. Safe to run in a deployment pipeline.",
    )
    arguments = parser.parse_args()

    try:
        role = _role_name()
        settings = get_settings()
    except (ConfigurationError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    password = os.environ.get("CHAUDRON_DB_APP_PASSWORD") or None
    if password is None and not arguments.check:
        logger.warning(
            "CHAUDRON_DB_APP_PASSWORD is unset: the role is provisioned without "
            "changing its password. An existing role keeps the one it has; a new "
            "one will have none and cannot connect over TCP."
        )

    url = make_url(settings.database_url.get_secret_value())
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            if not arguments.check:
                await provision(connection, role, password)
            problems = await report(connection, role)
    finally:
        await engine.dispose()

    if problems:
        for problem in problems:
            logger.error("%s", problem)
        return 1

    logger.info(
        "row-level security is enforceable for %s; point the application at "
        "%s and keep %s for migrations only",
        role,
        url.set(username=role, password=None).render_as_string(hide_password=True),
        url.username,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
