"""Shared fixtures for the Chaudron backend test suite.

Two rules shape everything below.

* **PostgreSQL only.** There is no SQLite mode, not even for "unit" tests. The
  schema relies on ``timestamptz``, ``numeric``, ``jsonb``, partial indexes and
  composite unique constraints; an engine that fakes those hides exactly the bugs
  worth catching (ADR-0003).
* **Podman only.** When no database URL is provided, an ephemeral container is
  started through testcontainers, pointed at the rootless Podman socket. See
  :func:`_configure_container_runtime` -- that wiring is the classic failure point
  of the testcontainers + Podman combination.

The database is resolved in this order:

1. ``CHAUDRON_TEST_DATABASE_URL`` -- explicit override, wins over everything.
2. ``CHAUDRON_DATABASE_URL`` -- what CI sets for its ``postgres:16`` service.
3. An ephemeral container started here.

If none of the three can be obtained, the database fixtures **skip** with the
reason attached. They never fall back to another engine, and they never fail the
run on a machine that simply has no container runtime.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from chaudron.domain.models import (
    Base,
    Household,
    HouseholdMember,
    MembershipRole,
    UserAccount,
)

if TYPE_CHECKING:
    import httpx

# --------------------------------------------------------------------------- #
# Container runtime
# --------------------------------------------------------------------------- #

_POSTGRES_IMAGE: Final = "postgres:16"

# Checked in order; the first one set wins.
_DATABASE_URL_ENV_VARS: Final = ("CHAUDRON_TEST_DATABASE_URL", "CHAUDRON_DATABASE_URL")

_PODMAN_SETUP_HINT: Final = (
    "Enable the rootless Podman socket with "
    "`systemctl --user enable --now podman.socket`, or set CHAUDRON_TEST_DATABASE_URL "
    "to an existing PostgreSQL 16 instance."
)


def _podman_socket() -> Path | None:
    """Locate the Podman API socket, rootless first, then rootful."""
    candidates: list[Path] = []
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "podman" / "podman.sock")
    candidates.append(Path("/run/podman/podman.sock"))
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _configure_container_runtime() -> str | None:
    """Point testcontainers at Podman. Returns a reason string when it cannot.

    testcontainers speaks the Docker HTTP API, which Podman implements, so the whole
    job is telling it where the socket is -- plus one Podman-specific correction:

    ``TESTCONTAINERS_RYUK_DISABLED``. Ryuk is the reaper container testcontainers
    starts to garbage-collect after a crashed run. It bind-mounts the container
    socket and expects to run privileged; under rootless Podman it never becomes
    ready, and every run dies on ``TimeoutError: container did not become running``
    -- reported at the *database* container, which sends you looking in the wrong
    place entirely. Containers started here are stopped by the session fixture's
    ``finally`` instead, which covers everything but a hard kill of the interpreter.

    The disabling is applied whenever the socket looks like Podman, including when
    ``DOCKER_HOST`` was exported by the developer's shell -- that case is the common
    one, and skipping the correction for it was this function's first bug.

    An explicit ``DOCKER_HOST`` is otherwise left alone: someone pointing at a remote
    or rootful daemon on purpose should not be second-guessed here.
    """
    docker_host = os.environ.get("DOCKER_HOST")
    if not docker_host:
        socket = _podman_socket()
        if socket is None:
            return f"no Podman socket found. {_PODMAN_SETUP_HINT}"
        docker_host = f"unix://{socket}"
        os.environ["DOCKER_HOST"] = docker_host
    if "podman" in docker_host:
        os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    return None


def _as_asyncpg_url(raw_url: str) -> str:
    """Normalise any PostgreSQL URL to the async driver the application uses."""
    return (
        make_url(raw_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    )


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A reachable PostgreSQL 16, from the environment or from a fresh container."""
    for env_var in _DATABASE_URL_ENV_VARS:
        configured = os.environ.get(env_var)
        if configured:
            yield _as_asyncpg_url(configured)
            return

    unavailable = _configure_container_runtime()
    if unavailable is not None:
        pytest.skip(f"database tests need PostgreSQL: {unavailable}")

    try:
        # `testcontainers.postgres` still resolves but is a deprecated shim whose wait
        # strategy probes the database from the host with a *synchronous* driver that
        # is not installed here, and reports the miss as a connection refusal against
        # Podman. The community module is the maintained path.
        from testcontainers.community.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - dev group is always installed in CI
        pytest.skip(f"testcontainers is not installed: {exc}")

    container = PostgresContainer(_POSTGRES_IMAGE, driver="asyncpg")
    try:
        container.start()
    except Exception as exc:  # broad on purpose: any failure here means "no database"
        pytest.skip(
            f"could not start {_POSTGRES_IMAGE} through Podman: {exc}. {_PODMAN_SETUP_HINT}"
        )

    try:
        yield str(container.get_connection_url())
    finally:
        container.stop()


async def _create_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            # Required by ix_product_name_trgm. The test role owns its database, so
            # this succeeds; in production the extension is created by a migration.
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def initialised_database(postgres_url: str) -> str:
    """Create the schema once per session.

    NOTE: this uses ``metadata.create_all`` because no Alembic revision exists yet.
    The day ``migrations/versions`` has content, this must run the migrations
    instead -- otherwise the suite validates a schema no environment ever applies,
    and a broken migration reaches production green.

    Deliberately a *synchronous* fixture driving its own loop: the session-scoped
    async fixture alternative forces a session-scoped event loop, and asyncpg
    connections created there break when the function-scoped tests run in another.
    """
    asyncio.run(_create_schema(postgres_url))
    return postgres_url


@pytest.fixture
async def engine(initialised_database: str) -> AsyncIterator[AsyncEngine]:
    """A per-test engine. ``NullPool`` keeps no connection alive across event loops."""
    test_engine = create_async_engine(initialised_database, poolclass=NullPool)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose writes are rolled back when the test ends.

    The session joins an outer transaction opened on the connection;
    ``join_transaction_mode="create_savepoint"`` turns any ``commit()`` made by the
    code under test into a savepoint release, so committing inside a test is legal
    and still leaves nothing behind. No truncation, no re-created schema, no
    ordering coupling between tests.
    """
    async with engine.connect() as connection:
        outer = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            if outer.is_active:
                await outer.rollback()


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
#
# Callable protocols rather than bare `Callable[..., Awaitable[T]]`: keyword
# arguments stay named and checked under `mypy --strict`, so a typo in a test is a
# type error rather than a runtime `TypeError` discovered on the next CI run.


class MakeHousehold(Protocol):
    async def __call__(
        self, *, name: str | None = None, is_instance_owner: bool = False
    ) -> Household: ...


class MakeUser(Protocol):
    async def __call__(
        self, *, email: str | None = None, display_name: str | None = None
    ) -> UserAccount: ...


class MakeMember(Protocol):
    async def __call__(
        self,
        household: Household,
        user: UserAccount,
        *,
        role: MembershipRole = MembershipRole.OWNER,
    ) -> HouseholdMember: ...


def _unique_suffix() -> str:
    """Short, collision-free and time-ordered, from the same source as the PKs."""
    return uuid.uuid7().hex[-12:]


@pytest.fixture
def make_household(db_session: AsyncSession) -> MakeHousehold:
    async def _make_household(
        *, name: str | None = None, is_instance_owner: bool = False
    ) -> Household:
        household = Household(
            name=name if name is not None else f"Household {_unique_suffix()}",
            is_instance_owner=is_instance_owner,
        )
        db_session.add(household)
        await db_session.flush()
        return household

    return _make_household


@pytest.fixture
def make_user(db_session: AsyncSession) -> MakeUser:
    async def _make_user(
        *, email: str | None = None, display_name: str | None = None
    ) -> UserAccount:
        suffix = _unique_suffix()
        user = UserAccount(
            email=email if email is not None else f"user-{suffix}@example.test",
            display_name=display_name if display_name is not None else f"User {suffix}",
            # No password: hashing belongs to the auth module, which does not exist
            # yet, and a fixture must not invent a format the real code will contradict.
            password_hash=None,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _make_user


@pytest.fixture
def make_member(db_session: AsyncSession) -> MakeMember:
    async def _make_member(
        household: Household,
        user: UserAccount,
        *,
        role: MembershipRole = MembershipRole.OWNER,
    ) -> HouseholdMember:
        member = HouseholdMember(household_id=household.id, user_id=user.id, role=role)
        db_session.add(member)
        await db_session.flush()
        return member

    return _make_member


@dataclass(frozen=True, slots=True)
class TenantPair:
    """Two unrelated households, each with an owner. The seed of every isolation test.

    Systematic by construction: a test that asks for this fixture cannot "forget" to
    create the second tenant, which is how isolation tests usually end up proving
    nothing (see `docs/testing-strategy.md`, section 4).
    """

    household_a: Household
    owner_a: UserAccount
    household_b: Household
    owner_b: UserAccount


@pytest.fixture
async def tenant_pair(
    make_household: MakeHousehold, make_user: MakeUser, make_member: MakeMember
) -> TenantPair:
    household_a = await make_household(name="Household A")
    owner_a = await make_user(display_name="Owner A")
    await make_member(household_a, owner_a)

    household_b = await make_household(name="Household B")
    owner_b = await make_user(display_name="Owner B")
    await make_member(household_b, owner_b)

    return TenantPair(
        household_a=household_a, owner_a=owner_a, household_b=household_b, owner_b=owner_b
    )


# --------------------------------------------------------------------------- #
# HTTP client -- not wireable yet
# --------------------------------------------------------------------------- #


@pytest.fixture
def api_client() -> httpx.AsyncClient:
    """Placeholder for the ASGI test client.

    Skips instead of guessing. Wiring it needs three things that do not exist yet,
    and inventing any of them would produce a fixture that silently tests something
    other than the application:

    1. An application factory (``chaudron.api.app:create_app`` or equivalent) -- the
       ``api`` package is still empty.
    2. A dependency override binding the request-scoped session to :func:`db_session`,
       so requests join the test transaction and are rolled back with it.
    3. An authentication override producing the ``HouseholdScope``. It must go
       through the real resolution path: ADR-0006 requires the tenant to derive from
       the auth context, so a fixture that injects ``household_id`` directly would
       bypass the very code the isolation tests exist to exercise.
    """
    pytest.skip(
        "no HTTP client fixture yet: chaudron.api exposes no application factory, "
        "no session dependency to override, and no auth context to build a "
        "HouseholdScope from. See the docstring of tests.conftest.api_client."
    )


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

# Requesting any of these means the test needs a live PostgreSQL.
_DATABASE_FIXTURES: Final = frozenset(
    {
        "postgres_url",
        "initialised_database",
        "engine",
        "db_session",
        "make_household",
        "make_user",
        "make_member",
        "tenant_pair",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark database-backed tests ``integration`` automatically.

    A marker applied by hand is a marker forgotten by hand: `-m "not integration"`
    would then run tests that hang waiting for a database. Deriving it from the
    requested fixtures makes the marker impossible to desynchronise.
    """
    for item in items:
        if not isinstance(item, pytest.Function):
            continue
        if _DATABASE_FIXTURES.intersection(item.fixturenames):
            item.add_marker(pytest.mark.integration)
