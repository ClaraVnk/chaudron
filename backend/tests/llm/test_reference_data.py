"""The seeded ``llm_provider`` rows must describe models this instance can actually use.

The reference table is data and the adapters are code, and nothing but a test keeps
the two in step. When they drifted apart the symptom was silent and expensive: the
default pre-filled for a household was a model no adapter knew, so every configuration
created on that default was rejected as "unknown model" -- with the reference table
insisting, in the database, that it was the recommended choice.

These assertions run against the real migrated database, so they cover the seed *as
applied* rather than a constant in a migration module that may or may not have run.
"""

from __future__ import annotations

from typing import Final

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.llm_ports import ProviderCapabilities
from chaudron.domain.models import LlmProvider
from chaudron.infra.llm.anthropic_provider import anthropic_capabilities
from chaudron.infra.llm.factory import BYOK_PROVIDERS
from chaudron.infra.llm.gemini_provider import gemini_capabilities
from chaudron.infra.llm.ollama_provider import PROVIDER_CODE as OLLAMA_CODE
from chaudron.infra.llm.openai_compatible import mistral_capabilities, openai_capabilities

#: The adapter that answers for each provider code. Ollama is absent on purpose: its
#: capabilities depend on the pulled model and are established by a probe, so it is
#: the one provider that cannot -- and must not -- declare a default model.
_CAPABILITIES: Final = {
    "anthropic": anthropic_capabilities,
    "openai": openai_capabilities,
    "gemini": gemini_capabilities,
    "mistral": mistral_capabilities,
}


async def _providers(session: AsyncSession) -> list[LlmProvider]:
    rows = (await session.scalars(select(LlmProvider).order_by(LlmProvider.sort_order))).all()
    assert rows, "revision 0002 seeds the provider table; an empty one means it did not run"
    return list(rows)


async def test_every_declared_default_model_is_one_an_adapter_knows(
    db_session: AsyncSession,
) -> None:
    """The invariant the ``gpt-5`` incident cost us.

    A ``default_model`` that no capability table contains is a default that cannot be
    used: the adapter refuses an unknown model rather than assuming it capable
    (ADR-0005), so the configuration is rejected at creation.
    """
    for provider in await _providers(db_session):
        if provider.default_model is None:
            continue
        capabilities_for = _CAPABILITIES.get(provider.code)
        assert capabilities_for is not None, (
            f"provider {provider.code!r} declares a default model but no adapter in "
            "chaudron.infra.llm can say what it supports"
        )
        # Raises ProviderNotConfigured on an unknown model -- which is the failure the
        # household used to get, several screens later, instead of here.
        capabilities_for(provider.default_model)


async def test_the_declared_default_capabilities_match_the_adapter(
    db_session: AsyncSession,
) -> None:
    """A row that names a real model but lies about it is the subtler version.

    These columns pre-fill a household's configuration, and a wrong ``supports_vision``
    offers receipt import to a model that cannot see -- which produces a fabricated
    shopping list rather than an error.
    """
    for provider in await _providers(db_session):
        if provider.default_model is None:
            continue
        actual: ProviderCapabilities = _CAPABILITIES[provider.code](provider.default_model)
        declared = (
            provider.default_supports_vision,
            provider.default_supports_structured_output,
            provider.default_max_context_tokens,
        )
        assert declared == (
            actual.supports_vision,
            actual.supports_structured_output,
            actual.context_window,
        ), f"llm_provider.{provider.code} contradicts the adapter about {actual.model!r}"


async def test_ollama_is_the_only_provider_without_a_default_model(
    db_session: AsyncSession,
) -> None:
    """Its model name is free text chosen by the user; no table could cover it."""
    without_default = [p.code for p in await _providers(db_session) if p.default_model is None]
    assert without_default == [OLLAMA_CODE]


async def test_the_providers_that_require_a_key_are_exactly_the_byok_four(
    db_session: AsyncSession,
) -> None:
    """ADR-0007's list of first-rank providers, asserted against the seed."""
    requiring = {p.code for p in await _providers(db_session) if p.requires_api_key}
    assert requiring == set(BYOK_PROVIDERS)


@pytest.mark.parametrize("code", sorted(_CAPABILITIES))
def test_every_adapter_this_instance_ships_is_seeded(code: str) -> None:
    """The mirror image: an adapter nobody can select is dead weight."""
    assert code in BYOK_PROVIDERS
