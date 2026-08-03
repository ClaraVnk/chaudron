"""The life of a household's API key: written once, used, never read back.

This is the security surface of ADR-0007 exercised against a real PostgreSQL, and
three of its assertions are the reason the feature is worth having at all:

* a key can be **stored** and then **used**, which is what makes ``byok`` a product
  rather than a column;
* a key can never be **read back** -- not by the service that wrote it, not by the
  API, not from a row copied out of the database;
* a ciphertext **replayed into another household** is refused. That test walks the
  exact path an attacker with database write access would take, and demands failure.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.llm_ports import CredentialDecryptionError, ProviderNotConfigured
from chaudron.domain.models import LlmConfigStatus, LlmProviderConfig, LlmProviderMode
from chaudron.services.providers import (
    ProviderCredentialService,
    ProviderService,
    provider_ports_builder,
)
from tests.api.test_providers import CAPABILITIES_URL, add_config
from tests.conftest import (
    MakeHousehold,
    TenantPair,
    build_test_cipher,
    build_test_settings,
    household_headers,
)

_A_KEY = "sk-ant-api03-the-first-key-a-household-set"
_ANOTHER_KEY = "sk-ant-api03-the-replacement-key-2222abcd"


def credentials(session: AsyncSession) -> ProviderCredentialService:
    return ProviderCredentialService(session, build_test_cipher())


def providers(session: AsyncSession, database_url: str) -> ProviderService:
    """The read side, wired exactly as the application wires it."""
    settings = build_test_settings(database_url)
    cipher = build_test_cipher()
    return ProviderService(session, provider_ports_builder(settings, cipher), cipher)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def test_a_stored_key_becomes_a_usable_configuration(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    config = await add_config(
        db_session, household, mode=LlmProviderMode.BYOK, api_key="sk-placeholder-key"
    )

    stored = await credentials(db_session).store_api_key(household.id, config.id, _A_KEY)

    assert stored.last4 == _A_KEY[-4:]
    body = (await api_client.get(CAPABILITIES_URL, headers=household_headers(household))).json()
    assert body["configured"] is True


async def test_the_written_key_is_not_readable_anywhere_afterwards(
    api_client: httpx.AsyncClient, db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Write-only by construction: the row holds ciphertext and four characters."""
    household = await make_household()
    config = await add_config(
        db_session, household, mode=LlmProviderMode.BYOK, api_key="sk-placeholder-key"
    )
    await credentials(db_session).store_api_key(household.id, config.id, _A_KEY)

    response = await api_client.get(CAPABILITIES_URL, headers=household_headers(household))
    assert _A_KEY not in response.text

    stored_ciphertext = await db_session.scalar(
        select(LlmProviderConfig.api_key_ciphertext).where(LlmProviderConfig.id == config.id)
    )
    assert stored_ciphertext is not None
    assert _A_KEY.encode() not in stored_ciphertext


async def test_replacing_a_key_overwrites_it_and_asks_for_re_verification(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """Rotation is an idempotent write: no versioning, no second copy to forget."""
    household = await make_household()
    config = await add_config(
        db_session,
        household,
        mode=LlmProviderMode.BYOK,
        api_key=_A_KEY,
        status=LlmConfigStatus.INVALID_CREDENTIALS,
    )

    stored = await credentials(db_session).store_api_key(household.id, config.id, _ANOTHER_KEY)

    await db_session.refresh(config)
    assert stored.last4 == _ANOTHER_KEY[-4:]
    assert config.api_key_last4 == _ANOTHER_KEY[-4:]
    assert config.api_key_set_at is not None
    # The provider's earlier verdict was about a key that no longer exists.
    assert config.status is LlmConfigStatus.UNVERIFIED
    assert config.last_error is None


async def test_a_key_cannot_be_written_onto_another_households_configuration(
    db_session: AsyncSession, tenant_pair: TenantPair
) -> None:
    """The tenant guard on the write path, stated as its own test."""
    theirs = await add_config(
        db_session, tenant_pair.household_a, mode=LlmProviderMode.BYOK, api_key=_A_KEY
    )

    with pytest.raises(ProviderNotConfigured, match="belongs to this household"):
        await credentials(db_session).store_api_key(
            tenant_pair.household_b.id, theirs.id, _ANOTHER_KEY
        )


async def test_a_mode_that_must_not_hold_a_key_refuses_one(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """The operator's key is never copied into the database (ADR-0007)."""
    household = await make_household()
    config = await add_config(db_session, household, mode=LlmProviderMode.INSTANCE_OWNER)

    with pytest.raises(ProviderNotConfigured, match="only 'byok'"):
        await credentials(db_session).store_api_key(household.id, config.id, _A_KEY)


async def test_an_unusable_key_is_refused_without_being_quoted(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    household = await make_household()
    config = await add_config(db_session, household, mode=LlmProviderMode.BYOK, api_key=_A_KEY)

    with pytest.raises(ProviderNotConfigured) as raised:
        await credentials(db_session).store_api_key(household.id, config.id, "sk-1")

    assert "sk-1" not in str(raised.value), "a validation message must not echo the value"


async def test_surrounding_whitespace_is_stripped_before_encryption(
    db_session: AsyncSession, initialised_database: str, make_household: MakeHousehold
) -> None:
    """A pasted key arrives with a newline far more often than not.

    Stored verbatim it becomes an authentication failure that is invisible in a
    database dump -- the same trap as a podman secret written with `echo`.
    """
    household = await make_household()
    config = await add_config(
        db_session, household, mode=LlmProviderMode.BYOK, api_key="sk-placeholder-key"
    )

    await credentials(db_session).store_api_key(household.id, config.id, f"  {_A_KEY}\n")

    active = await providers(db_session, initialised_database).for_recipes(household.id)
    assert active.config.sealed_api_key is not None
    assert build_test_cipher().decrypt(active.config.sealed_api_key) == _A_KEY


# --------------------------------------------------------------------------- #
# The replay
# --------------------------------------------------------------------------- #


async def test_a_ciphertext_replayed_into_another_household_is_refused(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    initialised_database: str,
    tenant_pair: TenantPair,
) -> None:
    """Write access to the database must not be enough to spend someone else's credit.

    Household B copies A's three secret columns -- ciphertext, ``last4`` and the key
    id, so every constraint and the rotation check pass -- onto its own configuration
    row and asks Chaudron to use it. The AAD binds the ciphertext to
    ``(household_id, llm_provider_config.id)``, so the theft fails at the only place
    it can be caught: authenticated decryption.
    """
    victim = await add_config(
        db_session, tenant_pair.household_a, mode=LlmProviderMode.BYOK, api_key=_A_KEY
    )
    thief = await add_config(
        db_session, tenant_pair.household_b, mode=LlmProviderMode.BYOK, api_key=_ANOTHER_KEY
    )
    # Read out of the ORM before the raw UPDATE below, because everything is expired
    # afterwards and a lazy reload from a sync context would fail on an async session.
    victim_id, thief_household = victim.id, tenant_pair.household_b.id
    victim_household, thief_headers = (
        tenant_pair.household_a.id,
        household_headers(tenant_pair.household_b),
    )

    stolen = (
        await db_session.execute(
            select(
                LlmProviderConfig.api_key_ciphertext,
                LlmProviderConfig.api_key_last4,
                LlmProviderConfig.api_key_encryption_key_id,
            ).where(LlmProviderConfig.id == victim.id)
        )
    ).one()
    await db_session.execute(
        update(LlmProviderConfig)
        .where(LlmProviderConfig.id == thief.id)
        .values(
            api_key_ciphertext=stolen.api_key_ciphertext,
            api_key_last4=stolen.api_key_last4,
            api_key_encryption_key_id=stolen.api_key_encryption_key_id,
        )
    )
    db_session.expire_all()

    # The banner refuses it...
    body = (await api_client.get(CAPABILITIES_URL, headers=thief_headers)).json()
    assert body["configured"] is False
    assert "déchiffrée" in body["degraded_reasons"][0]

    # ...and so does the path that would actually have spent the victim's money.
    with pytest.raises(CredentialDecryptionError) as raised:
        await providers(db_session, initialised_database).for_recipes(thief_household)
    assert _A_KEY not in str(raised.value)

    # The victim is untouched: their own row still decrypts under their own identity.
    assert (
        await providers(db_session, initialised_database).for_recipes(victim_household)
    ).config_id == victim_id
