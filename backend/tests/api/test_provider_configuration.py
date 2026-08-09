"""Switching bring-your-own-model on, over HTTP, and never getting the key back.

``tests/api/test_providers.py`` covers the banner and ``test_provider_credentials.py``
covers the credential service directly. This file covers the routes that were
missing entirely until now: the ones that **create** a configuration, alter it,
retire it, and grant or withdraw the agreement that lets it be used. Penetration
test finding O-02 -- "no route creates a provider configuration at all" -- is what
they close, and three properties are what makes them worth having rather than
merely present:

* a key can be **submitted** over HTTP and then **used**, which is what makes
  ``byok`` a product rather than a column;
* a key can never be **read back** -- not from the creation response, not from the
  list, not from a rotation, not from an error. Every response body produced in this
  file is searched for it, by the same standard ``tests/llm/test_no_key_leaks.py``
  holds the adapters to;
* a configuration that would **transmit without an agreement cannot be stored**,
  and "would transmit" is decided by the endpoint rather than by the mode enum --
  which is the design note the penetration test left for this work.

The Ollama probe is a double throughout. The real one is exercised in
``tests/llm/test_ollama_probe.py`` against a fake socket; what is under test here is
what the *routes* do with its answer, and a test that needed a running Ollama would
be a test nobody runs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.routers.providers import get_provider_config_service
from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderCapabilities,
    ProviderUnavailable,
)
from chaudron.domain.models import (
    Household,
    LlmProviderConfig,
    MembershipRole,
)
from chaudron.infra.llm.http import Resolver
from chaudron.services.providers import ProviderConfigService
from tests.api.test_providers import CAPABILITIES_URL
from tests.conftest import (
    MakeHousehold,
    MakeMember,
    MakeUser,
    TenantPair,
    build_test_cipher,
    household_headers,
)

CONFIGS_URL = "/v1/providers"
CATALOGUE_URL = "/v1/providers/catalogue"

#: A household's own key, in the shape a provider issues one. Long enough that
#: `infra/redaction.py` treats it as a secret, and distinctive enough that a
#: substring search over a response body is a real assertion.
A_KEY = "sk-ant-api03-the-key-this-household-pays-for"
ANOTHER_KEY = "sk-ant-api03-the-replacement-key-9999wxyz"

#: What a probed Ollama reports back. Fixed rather than `now()` so a test can assert
#: the date reached the row rather than assert that some date did.
PROBED_AT = dt.datetime(2026, 5, 4, 11, 15, tzinfo=dt.UTC)


def probed(*, vision: bool = True, context: int = 131_072) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider="ollama",
        model="llama3.2-vision",
        context_window=context,
        supports_structured_output=True,
        supports_vision=vision,
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=PROBED_AT,
    )


class RecordingProbe:
    """An Ollama that answers from a script, and remembers what it was asked."""

    def __init__(self, answer: ProviderCapabilities | Exception | None = None) -> None:
        self.answer = answer if answer is not None else probed()
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, base_url: str, model: str) -> ProviderCapabilities:
        self.calls.append((base_url, model))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def resolving(*addresses: str) -> Resolver:
    async def resolve(host: str, port: int) -> frozenset[str]:
        return frozenset(addresses)

    return resolve


def wire(
    app: FastAPI,
    session: AsyncSession,
    *,
    probe: RecordingProbe | None = None,
    instance_owner: uuid.UUID | None = None,
    resolver: Resolver | None = None,
) -> RecordingProbe:
    """Replace the write service with one whose probe and resolver are doubles.

    Only the two things that would otherwise leave the process are substituted. The
    session, the cipher, the guards and every route are the real ones -- so what is
    exercised is the application, not a rehearsal of it.
    """
    recording = probe or RecordingProbe()
    app.dependency_overrides[get_provider_config_service] = lambda: ProviderConfigService(
        session,
        build_test_cipher(),
        probe=recording,
        instance_owner_household_id=instance_owner,
        resolver=resolver or resolving("10.89.0.14"),
    )
    return recording


def byok_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "label": "Ma clé Anthropic",
        "mode": "byok",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "consent_granted": True,
        "api_key": A_KEY,
    }
    body.update(overrides)
    return body


def ollama_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "label": "Le NAS",
        "mode": "ollama",
        "provider": "ollama",
        "model": "llama3.2-vision",
        "consent_granted": False,
        "base_url": "http://127.0.0.1:11434",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Creating
# --------------------------------------------------------------------------- #


async def test_a_household_can_switch_on_its_own_key_over_http(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The whole point of this change, in one test: no SQL, and the feature works.

    Before these routes existed, an operator had to write the ``INSERT`` by hand --
    and, since revision ``0016``, had to date a consent in the household's name to do
    it, which is not a thing an operator may do on somebody else's behalf.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)

    created = await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["mode"] == "byok"
    assert body["provider"] == "anthropic"
    assert body["api_key_last4"] == A_KEY[-4:]
    assert body["consent_required"] is True
    assert body["is_consented"] is True
    assert body["is_permitted"] is True
    assert body["capabilities"] == {"vision": True, "structured_output": True}

    # ...and the banner the whole interface reads now says the feature is on.
    banner = (await api_client.get(CAPABILITIES_URL, headers=headers)).json()
    assert banner["configured"] is True, banner
    assert banner["model"] == "claude-opus-5"


async def test_the_submitted_key_never_comes_back_by_any_route(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Asked for over HTTP, in every shape the API offers, and never handed over.

    This is the assertion ADR-0007 rests on, made against the routes rather than
    against the service that backs them: a response model with no field for a key
    cannot leak one, and this is what proves the models in the router are those.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)

    created = await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)
    config_id = created.json()["id"]

    listed = await api_client.get(CONFIGS_URL, headers=headers)
    banner = await api_client.get(CAPABILITIES_URL, headers=headers)
    rotated = await api_client.patch(
        f"{CONFIGS_URL}/{config_id}", json={"api_key": ANOTHER_KEY}, headers=headers
    )
    consented = await api_client.post(
        f"{CONFIGS_URL}/{config_id}/consent", json={"granted": True}, headers=headers
    )
    archived = await api_client.delete(f"{CONFIGS_URL}/{config_id}", headers=headers)

    for response in (created, listed, banner, rotated, consented, archived):
        assert A_KEY not in response.text, response.request.url
        assert ANOTHER_KEY not in response.text, response.request.url
        assert "sk-ant-api03" not in response.text, response.request.url
    # Four characters, and they are the *new* ones: a rotation that kept showing the
    # old tail would tell a household its key had not been replaced.
    assert rotated.json()["api_key_last4"] == ANOTHER_KEY[-4:]

    # And what landed in the column is ciphertext, not the key with a coat on.
    stored = await db_session.scalar(
        select(LlmProviderConfig.api_key_ciphertext).where(
            LlmProviderConfig.id == uuid.UUID(config_id)
        )
    )
    assert stored is not None
    assert ANOTHER_KEY.encode() not in stored


async def test_a_key_is_not_echoed_by_the_refusal_that_rejects_it(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A validation error is exactly the kind of string that ends up in a client log.

    ``api/errors.py`` strips pydantic's ``input`` echo from a 422, and the field is a
    ``SecretStr`` so a model dumped anywhere prints a mask. Both locks, asserted
    together, because either one alone would have been enough to believe in.
    """
    household = await make_household()
    wire(api_app, db_session)

    refused = await api_client.post(
        CONFIGS_URL,
        json=byok_body(model="a-model-nobody-ships"),
        headers=household_headers(household),
    )

    assert refused.status_code == 422, refused.text
    assert A_KEY not in refused.text
    assert "sk-ant" not in refused.text
    assert "claude-opus-5" in refused.text, "the refusal must name what may be chosen instead"


async def test_a_configuration_that_would_transmit_needs_an_agreement_to_exist(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Opt-in, and refused before anything is written (art. 6(1)(a))."""
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)

    refused = await api_client.post(
        CONFIGS_URL, json=byok_body(consent_granted=False), headers=headers
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("provider-consent-required")
    assert (await api_client.get(CONFIGS_URL, headers=headers)).json() == []


async def test_the_agreement_has_no_default_and_must_be_stated(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A body that omits the field is a 422, not a configuration that quietly agreed.

    A default of ``true`` would be a pre-ticked box; a default of ``false`` would let
    a client omit the one field the feature turns on and meet a refusal it could not
    explain. Required is what forces the question to be asked.
    """
    household = await make_household()
    wire(api_app, db_session)
    body = byok_body()
    del body["consent_granted"]

    refused = await api_client.post(CONFIGS_URL, json=body, headers=household_headers(household))

    assert refused.status_code == 422, refused.text


async def test_a_hosted_ollama_cannot_be_stored_under_the_local_exemption(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The penetration test's design note, at the write end.

    ``mode = 'ollama'`` used to be the whole of the exemption, so an operator who
    allowlisted a hosted Ollama produced a configuration that shipped a child's age
    band and a member's health note to a third party with the gate switched off.
    The endpoint decides now, and a public one is treated exactly like Anthropic.
    """
    household = await make_household()
    wire(api_app, db_session, resolver=resolving("203.0.113.9", "35.1.2.3"))
    headers = household_headers(household)

    refused = await api_client.post(
        CONFIGS_URL,
        json=ollama_body(base_url="https://ollama.example.com", consent_granted=False),
        headers=headers,
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("provider-consent-required")

    # The same endpoint, with the household actually agreeing, is allowed -- the
    # rule is "say so", not "you may not".
    accepted = await api_client.post(
        CONFIGS_URL,
        json=ollama_body(base_url="https://ollama.example.com", consent_granted=True),
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["consent_required"] is True


async def test_a_local_ollama_needs_no_agreement_and_is_probed(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """ADR-0007's premise: a household that sends nothing anywhere gets the feature.

    And the configuration is **probed** before it is stored: ADR-0007 is explicit
    that a configuration whose abilities nobody established must not exist, because
    the interface would then promise a feature on a guess.
    """
    household = await make_household()
    probe = wire(api_app, db_session)
    headers = household_headers(household)

    created = await api_client.post(CONFIGS_URL, json=ollama_body(), headers=headers)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["consent_required"] is False
    assert body["is_permitted"] is True
    assert body["consented_at"] is None
    assert body["capabilities"] == {"vision": True, "structured_output": True}
    assert body["max_context_tokens"] == 131_072
    assert body["status"] == "verified"
    assert probe.calls == [("http://127.0.0.1:11434", "llama3.2-vision")]

    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is True


async def test_an_unreachable_ollama_fails_the_save_rather_than_being_stored(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Guessing is worse than refusing: a stored row here would promise a feature."""
    household = await make_household()
    wire(
        api_app,
        db_session,
        probe=RecordingProbe(ProviderUnavailable("Ollama could not be reached")),
    )
    headers = household_headers(household)

    refused = await api_client.post(CONFIGS_URL, json=ollama_body(), headers=headers)

    assert refused.status_code == 422, refused.text
    assert refused.json()["type"].endswith("provider-probe-failed")
    assert (await api_client.get(CONFIGS_URL, headers=headers)).json() == []


async def test_the_operators_key_is_reserved_for_the_household_that_owns_the_instance(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The locked door of ADR-0007, closed at creation rather than at the invoice.

    The factory refuses this at call time too. Refusing here keeps a household from
    storing a configuration that can only ever produce a 409 -- and keeps the default
    closed, because the failure mode of getting it wrong is a stranger's bill.
    """
    household = await make_household()
    wire(api_app, db_session, instance_owner=uuid.uuid7())

    refused = await api_client.post(
        CONFIGS_URL,
        json={
            "label": "Sur le compte de l'hébergeur",
            "mode": "instance_owner",
            "provider": "openai",
            "model": "gpt-4o",
            "consent_granted": True,
        },
        headers=household_headers(household),
    )

    assert refused.status_code == 422, refused.text
    assert "instance_owner" in refused.json()["detail"]


async def test_reusing_the_name_of_the_existing_configuration_is_refused_by_name(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Compared without regard to case, like the partial unique index behind it.

    Both refusals are true of this request since revision ``0025`` -- the name is
    taken *and* the household already has its one configuration -- and the label is
    checked first, so this is the one the household reads. That ordering is what
    keeps ``uq_llm_provider_config_label`` a rule something still exercises rather
    than an index nothing can reach.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)

    clash = await api_client.post(
        CONFIGS_URL,
        json=byok_body(label="MA CLÉ ANTHROPIC", model="claude-haiku-4-5"),
        headers=headers,
    )

    assert clash.status_code == 409, clash.text
    assert clash.json()["type"].endswith("provider-label-taken")


# --------------------------------------------------------------------------- #
# One household, one configuration (revision 0025)
# --------------------------------------------------------------------------- #


async def test_a_second_configuration_is_refused_rather_than_replacing_the_first(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """409, and the existing configuration is untouched.

    Silently replacing it -- or archiving it on the household's behalf -- would
    retire a working API key because somebody pressed "create". The refusal names
    the two things the household can do instead.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    first = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()

    refused = await api_client.post(
        CONFIGS_URL,
        json=byok_body(label="Une autre clé", model="claude-haiku-4-5"),
        headers=headers,
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith("provider-config-already-exists")
    detail = refused.json()["detail"]
    assert "archive" in detail and "edit it in place" in detail, detail
    # Untouched: same row, same model, still the one the banner resolves.
    still_there = (await api_client.get(CONFIGS_URL, headers=headers)).json()
    assert [config["id"] for config in still_there] == [first["id"]]
    assert still_there[0]["model"] == first["model"]


async def test_archiving_the_one_configuration_lets_the_next_be_created(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Changing provider is archive-then-create, and it works end to end.

    The unique index is partial on ``archived_at IS NULL`` precisely so this
    sequence stays possible; without that, a household could switch provider once
    and never again.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    first = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()

    assert (
        await api_client.delete(f"{CONFIGS_URL}/{first['id']}", headers=headers)
    ).status_code == 200

    replacement = await api_client.post(
        CONFIGS_URL,
        json=byok_body(label="Ma nouvelle clé", model="claude-haiku-4-5"),
        headers=headers,
    )

    assert replacement.status_code == 201, replacement.text
    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["model"] == (
        "claude-haiku-4-5"
    )


# --------------------------------------------------------------------------- #
# Consent, granted and withdrawn
# --------------------------------------------------------------------------- #


async def test_withdrawing_the_agreement_stops_the_provider_at_the_next_request(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Art. 7(3), end to end: as easy as consent, and effective immediately.

    Nothing is deleted. The household keeps the record of what it authorised and
    when it stopped -- which is what makes "who did you send this to?" answerable
    after the fact.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()["id"]

    withdrawn = await api_client.delete(f"{CONFIGS_URL}/{config_id}/consent", headers=headers)

    assert withdrawn.status_code == 200, withdrawn.text
    body = withdrawn.json()
    assert body["is_consented"] is False
    assert body["is_permitted"] is False
    assert body["consented_at"] is not None, "the record of the agreement survives it"
    assert body["consent_revoked_at"] is not None

    banner = (await api_client.get(CAPABILITIES_URL, headers=headers)).json()
    assert banner["configured"] is False
    assert "retiré son accord" in banner["degraded_reasons"][0]


async def test_granting_again_after_a_withdrawal_puts_the_provider_back(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A new agreement, taking today's date, and clearing the withdrawal with it."""
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()["id"]
    await api_client.delete(f"{CONFIGS_URL}/{config_id}/consent", headers=headers)

    granted = await api_client.post(
        f"{CONFIGS_URL}/{config_id}/consent", json={"granted": True}, headers=headers
    )

    assert granted.status_code == 200, granted.text
    assert granted.json()["consent_revoked_at"] is None
    assert granted.json()["is_permitted"] is True
    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is True


async def test_granting_twice_does_not_move_the_date_of_the_agreement(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The one fact art. 7(1) asks a controller to be able to demonstrate."""
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    created = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()

    again = await api_client.post(
        f"{CONFIGS_URL}/{created['id']}/consent", json={"granted": True}, headers=headers
    )

    assert again.json()["consented_at"] == created["consented_at"]


async def test_the_grant_route_refuses_to_be_used_as_a_withdrawal(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """``granted: false`` is a mistake, not a quiet second way to withdraw.

    Two operations with two audit trails; collapsing them would make a withdrawal
    indistinguishable from a client that sent the wrong boolean.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()["id"]

    refused = await api_client.post(
        f"{CONFIGS_URL}/{config_id}/consent", json={"granted": False}, headers=headers
    )

    assert refused.status_code == 422, refused.text
    assert "DELETE" in refused.json()["detail"]
    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is True


async def test_there_is_nothing_to_withdraw_on_a_configuration_that_never_agreed(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A local Ollama has no agreement, so withdrawing one is a 409 rather than a lie."""
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=ollama_body(), headers=headers)).json()[
        "id"
    ]

    refused = await api_client.delete(f"{CONFIGS_URL}/{config_id}/consent", headers=headers)

    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith("provider-consent-not-recorded")


# --------------------------------------------------------------------------- #
# Altering, re-probing and retiring
# --------------------------------------------------------------------------- #


async def test_replacing_a_key_resets_the_verdict_about_the_old_one(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Rotation is an idempotent write: no versioning, no second copy to forget."""
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()["id"]

    rotated = await api_client.patch(
        f"{CONFIGS_URL}/{config_id}", json={"api_key": ANOTHER_KEY}, headers=headers
    )

    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["api_key_last4"] == ANOTHER_KEY[-4:]
    assert rotated.json()["status"] == "unverified"
    # The agreement is untouched by an edit: it is about the provider, not the key.
    assert rotated.json()["is_consented"] is True


async def test_moving_an_ollama_to_another_model_re_probes_it(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Capabilities that survive a model change are a claim about something else."""
    household = await make_household()
    probe = wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=ollama_body(), headers=headers)).json()[
        "id"
    ]
    probe.answer = probed(vision=False, context=8192)

    updated = await api_client.patch(
        f"{CONFIGS_URL}/{config_id}", json={"model": "llama3.2"}, headers=headers
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["capabilities"]["vision"] is False
    assert updated.json()["max_context_tokens"] == 8192
    assert probe.calls[-1] == ("http://127.0.0.1:11434", "llama3.2")


async def test_a_household_can_ask_its_ollama_again_what_its_model_can_do(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Pulling a vision model onto a registered endpoint has to be tellable.

    Until the capability columns are re-read the receipt photograph stays greyed
    out for a model that can now see, and nothing else in the API would say so.
    """
    household = await make_household()
    probe = wire(api_app, db_session, probe=RecordingProbe(probed(vision=False)))
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=ollama_body(), headers=headers)).json()[
        "id"
    ]
    probe.answer = probed(vision=True)

    refreshed = await api_client.post(f"{CONFIGS_URL}/{config_id}/probe", headers=headers)

    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["capabilities"]["vision"] is True


async def test_a_hosted_provider_has_no_endpoint_to_interrogate(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Refused rather than answered with a no-op: "refreshed" after doing nothing lies."""
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()["id"]

    refused = await api_client.post(f"{CONFIGS_URL}/{config_id}/probe", headers=headers)

    assert refused.status_code == 422, refused.text


async def test_archiving_retires_the_configuration_without_deleting_the_record(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Gone from the routes, still in the table.

    "What did we send, to whom, and when did we agree?" is an article 15 question,
    and a ``DELETE`` here would make it unanswerable. ``DELETE /v1/households`` is
    where erasure lives.
    """
    household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)
    config_id = (await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)).json()["id"]

    archived = await api_client.delete(f"{CONFIGS_URL}/{config_id}", headers=headers)

    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert (await api_client.get(CONFIGS_URL, headers=headers)).json() == []
    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is False
    assert (
        await db_session.scalar(
            select(LlmProviderConfig.id).where(LlmProviderConfig.id == uuid.UUID(config_id))
        )
        is not None
    )


# --------------------------------------------------------------------------- #
# Who may do any of this
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [MembershipRole.MEMBER, MembershipRole.VIEWER])
async def test_only_an_owner_may_configure_a_provider(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    role: MembershipRole,
) -> None:
    """It accepts a credential and consents on the whole household's behalf.

    The same argument ``routers/export_targets.py`` is owner-only for, arriving from
    the same side: a ``viewer`` could otherwise paste their own key, agree on
    everybody's behalf, and set the household's health data in motion.
    """
    household = await make_household(role=role)
    wire(api_app, db_session)
    headers = household_headers(household)

    refused = await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)

    assert refused.status_code == 403, refused.text


async def test_every_member_may_read_the_configuration_screen(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Four characters of a key are not a credential, and hiding the list would
    leave the people who use the feature unable to see why it is switched off."""
    household = await make_household(role=MembershipRole.VIEWER)
    wire(api_app, db_session)

    listed = await api_client.get(CONFIGS_URL, headers=household_headers(household))

    assert listed.status_code == 200, listed.text
    assert listed.json() == []


async def test_the_owner_of_one_household_cannot_touch_anothers_configuration(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    tenant_pair: TenantPair,
    make_user: MakeUser,
    make_member: MakeMember,
) -> None:
    """A guessed identifier must not be enough to rotate somebody else's key.

    ``404`` and not ``403``: telling a caller that the identifier exists but is not
    theirs would turn the route into a configuration oracle.
    """
    wire(api_app, db_session)
    theirs = (
        await api_client.post(
            CONFIGS_URL, json=byok_body(), headers=household_headers(tenant_pair.household_a)
        )
    ).json()["id"]

    refused = await api_client.patch(
        f"{CONFIGS_URL}/{theirs}",
        json={"label": "mine now"},
        headers=household_headers(tenant_pair.household_b),
    )

    assert refused.status_code == 404, refused.text


async def test_the_routes_need_a_session(anonymous_client: httpx.AsyncClient) -> None:
    assert (await anonymous_client.get(CONFIGS_URL)).status_code == 401
    assert (await anonymous_client.post(CONFIGS_URL, json=byok_body())).status_code == 401


# --------------------------------------------------------------------------- #
# The catalogue the screen is built from
# --------------------------------------------------------------------------- #


async def test_the_catalogue_offers_only_what_this_build_can_describe(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """A screen offering a model the capability lookup refuses is a screen that traps.

    The models come from the adapters themselves rather than from a list restated in
    the client, so the two cannot drift: whatever this endpoint offers is exactly
    what ``POST`` will accept.
    """
    household = await make_household()
    wire(api_app, db_session)

    catalogue = await api_client.get(CATALOGUE_URL, headers=household_headers(household))

    assert catalogue.status_code == 200, catalogue.text
    by_code = {entry["code"]: entry for entry in catalogue.json()}
    assert {"anthropic", "openai", "gemini", "mistral", "ollama"} <= set(by_code)
    assert "claude-opus-5" in by_code["anthropic"]["models"]
    assert by_code["anthropic"]["requires_api_key"] is True
    # Ollama's abilities depend on what the household pulled, so no list can be
    # offered and the form has to take free text there.
    assert by_code["ollama"]["models"] == []
    assert by_code["ollama"]["requires_base_url"] is True


async def test_the_catalogue_never_carries_the_instance_owner_key(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """Reference data about providers, not about this instance's credentials.

    ``instance_owner`` mode spends a key the operator holds in the environment, and
    this endpoint is the one place that describes the providers it could be spent
    on. It must describe *whether* a key is needed and never carry one.
    """
    household = await make_household()
    wire(api_app, db_session)

    catalogue = await api_client.get(CATALOGUE_URL, headers=household_headers(household))

    body = catalogue.json()
    assert all(set(entry) == set(body[0]) for entry in body)
    assert "api_key" not in set(body[0]), sorted(body[0])
    assert "sk-" not in catalogue.text
    assert "AIza" not in catalogue.text


# --------------------------------------------------------------------------- #
# The gate, on rows these routes produced
# --------------------------------------------------------------------------- #


async def test_a_hosted_ollama_whose_agreement_is_withdrawn_stops_sending(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The end of the O-02 thread: the exemption is the endpoint's, not the enum's.

    A hosted Ollama behaves like every other third party -- it needs the agreement,
    and it loses the feature when the agreement is withdrawn. Under the old gate
    this configuration would have kept sending, because its ``mode`` said ``ollama``.

    The default resolver is used deliberately here: ``ProviderService`` is the real
    one built by ``api/deps.py``, and ``.invalid`` resolves nowhere by RFC 6761, so
    the endpoint cannot be shown to be local -- which is the fail-closed branch.
    """
    household = await make_household()
    wire(api_app, db_session, resolver=resolving("203.0.113.9", "35.1.2.3"))
    headers = household_headers(household)
    created = await api_client.post(
        CONFIGS_URL,
        json=ollama_body(base_url="http://ollama.example.invalid:11434", consent_granted=True),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is True

    await api_client.delete(f"{CONFIGS_URL}/{created.json()['id']}/consent", headers=headers)

    banner = (await api_client.get(CAPABILITIES_URL, headers=headers)).json()
    assert banner["configured"] is False, banner
    assert "retiré son accord" in banner["degraded_reasons"][0]


async def test_a_household_ends_up_where_the_feature_actually_works(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
) -> None:
    """The end-to-end shape of the change, asserted as one story.

    Nothing configured, a screen with a catalogue on it, a key entered, and a
    provider the recipe path would accept -- with the key unreadable at every step.
    """
    household: Household = await make_household()
    wire(api_app, db_session)
    headers = household_headers(household)

    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is False
    assert (await api_client.get(CONFIGS_URL, headers=headers)).json() == []
    assert (await api_client.get(CATALOGUE_URL, headers=headers)).json() != []

    created = await api_client.post(CONFIGS_URL, json=byok_body(), headers=headers)
    assert created.status_code == 201, created.text

    listed = await api_client.get(CONFIGS_URL, headers=headers)
    assert [entry["label"] for entry in listed.json()] == ["Ma clé Anthropic"]
    assert A_KEY not in listed.text
    assert (await api_client.get(CAPABILITIES_URL, headers=headers)).json()["configured"] is True
