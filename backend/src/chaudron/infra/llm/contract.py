"""``CONTRACT_ADAPTERS`` -- what each adapter contributes to the conformance suite.

ADR-0005 accepts five adapters on one guarantee: *adding a provider is writing an
adapter and making the conformance suite pass*. This module is the adapter side of
that bargain. Each entry describes itself -- how to build its ports wired to a
double replaying a named scenario, what the tested (provider, model) pair claims to
be capable of, and which case of the degradation taxonomy it chose for each
capability it lacks -- and ``tests/contracts/test_llm_provider_contract.py`` does
the asserting.

Nothing here calls a real provider, and nothing here needs a credential.

**The tested configuration is chosen to exercise the matrix, not to flatter it.**
Anthropic, OpenAI and Gemini are pointed at fully capable models; Mistral AI at
``mistral-small-latest`` (no vision, short context) and Ollama at an old server
serving a text-only model. Between them, all three cases of the taxonomy --
``unavailable``, ``emulated`` and ``degraded`` -- are executed on every run. Pointing
all five at their best model would leave the entire degradation path untested, which
is the half of ADR-0005 most likely to break unnoticed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from chaudron.domain.llm_ports import (
    CapabilitySource,
    InventoryItem,
    ParsedReceipt,
    ProviderCapabilities,
    RecipeRequest,
    RecipeSuggestion,
)
from chaudron.infra.llm import doubles
from chaudron.infra.llm.anthropic_provider import (
    AnthropicClientLike,
    AnthropicTransport,
    anthropic_capabilities,
)
from chaudron.infra.llm.base import (
    ModelReceiptParser,
    ModelRecipeGenerator,
    ProviderTransport,
)
from chaudron.infra.llm.gemini_provider import (
    GeminiTransport,
    build_gemini_client,
    gemini_capabilities,
)
from chaudron.infra.llm.http import GuardedHttpClient
from chaudron.infra.llm.ollama_provider import OllamaTransport, build_guarded_client
from chaudron.infra.llm.openai_compatible import (
    MISTRAL_PROVIDER_CODE,
    OPENAI_PROVIDER_CODE,
    ChatCompletionsTransport,
    build_chat_client,
    mistral_capabilities,
    openai_capabilities,
)
from chaudron.infra.llm.settings import LlmSettings

__all__ = ["CONTRACT_ADAPTERS", "ContractProviderAdapter"]

_PORTS: Final = ("RecipeGenerator", "ReceiptParser")

#: A believable request: enough items that the short-context providers actually
#: trim, with expiry dates so the trimming has something to prioritise on.
_REQUEST: Final = RecipeRequest(
    inventory=tuple(
        InventoryItem(name=f"Item {index}", quantity="1", unit="pc", expires_in_days=index)
        for index in range(40)
    ),
    max_suggestions=3,
    notes="quick, no oven",
)

_IMAGE: Final = b"\x89PNG\r\n\x1a\n contract double, not a real photograph"
_MEDIA_TYPE: Final = "image/png"

_OLLAMA_HOST: Final = "ollama"
_OLLAMA_URL: Final = f"http://{_OLLAMA_HOST}:11434"
_OLLAMA_ADDRESSES: Final = frozenset({"10.89.0.7"})

_SETTINGS: Final = LlmSettings(
    ollama_allowed_hosts=frozenset({_OLLAMA_HOST}),
    instance_owner_household_id=uuid.UUID(int=1),
    timeout_seconds=5.0,
)


async def _fixed_resolver(host: str, port: int) -> frozenset[str]:
    """Stands in for DNS so the rebinding check runs without a resolver."""
    return _OLLAMA_ADDRESSES


#: What a 2024-era Ollama serving a text-only model answers when probed. Short
#: context, no vision, and a server too old to enforce a JSON schema -- so this one
#: adapter drives all three degradation cases at once.
_OLLAMA_CAPABILITIES: Final = ProviderCapabilities(
    provider="ollama",
    model="llama3.2",
    context_window=8192,
    supports_structured_output=False,
    supports_vision=False,
    supports_prompt_caching=False,
    source=CapabilitySource.PROBED,
    probed_at=dt.datetime(2026, 3, 3, 9, 30, tzinfo=dt.UTC),
)


# --------------------------------------------------------------------------- #
# Pre-bound ports
# --------------------------------------------------------------------------- #
#
# The harness calls the port method with no arguments: it is provider-agnostic and
# cannot know how to build a `RecipeRequest` or where to find a receipt photograph.
# For the `nominal` scenario it inspects the signature instead of calling, so that
# case returns the genuine adapter; every other scenario returns the same adapter
# with representative input already applied.


@dataclass(frozen=True, slots=True)
class _PreparedRecipeGenerator:
    inner: ModelRecipeGenerator

    async def suggest(self) -> list[RecipeSuggestion]:
        return await self.inner.suggest(_REQUEST)


@dataclass(frozen=True, slots=True)
class _PreparedReceiptParser:
    inner: ModelReceiptParser

    async def parse(self) -> ParsedReceipt:
        return await self.inner.parse(_IMAGE, _MEDIA_TYPE)


# --------------------------------------------------------------------------- #
# The registry entries
# --------------------------------------------------------------------------- #


class ContractProviderAdapter:
    """One provider's self-description for the conformance suite."""

    def __init__(self, key: str, capabilities: ProviderCapabilities) -> None:
        self.key = key
        self._capabilities = capabilities

    # -- what the harness calls ------------------------------------------- #

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def degradation_for(self, capability: str) -> str | None:
        strategy = self._capabilities.degradation_for(capability)
        return None if strategy is None else str(strategy)

    def build_port(self, port_name: str, scenario: str) -> object:
        if port_name not in _PORTS:
            raise LookupError(f"{self.key} does not offer {port_name}")
        if (
            port_name == "ReceiptParser"
            and not self._capabilities.supports_vision
            and scenario != "missing_vision"
        ):
            # "Not offered in the tested configuration" is the harness's own answer
            # for a vision-less provider, and it is the honest one: asking how this
            # configuration surfaces a rate limit on receipt import is asking about
            # a call it never makes. The one exception is the capability drill
            # itself, which exists to check that the refusal is a domain error
            # carrying a reason rather than a raw failure.
            raise LookupError(
                f"{self.key} has no vision in the tested configuration; receipt "
                "import is switched off (ADR-0005, 'explicit unavailability')"
            )
        transport = self._transport(scenario, port_name)
        if port_name == "ReceiptParser":
            parser = ModelReceiptParser(transport)
            return parser if scenario == "nominal" else _PreparedReceiptParser(parser)
        generator = ModelRecipeGenerator(transport)
        return generator if scenario == "nominal" else _PreparedRecipeGenerator(generator)

    # -- provider-specific wiring ----------------------------------------- #

    def _transport(self, scenario: str, port_name: str) -> ProviderTransport:
        raise NotImplementedError  # pragma: no cover


class _AnthropicContract(ContractProviderAdapter):
    def _transport(self, scenario: str, port_name: str) -> ProviderTransport:
        client: AnthropicClientLike = doubles.FakeAnthropicClient(scenario, port_name=port_name)
        return AnthropicTransport(client, self._capabilities, api_key=doubles.LEAKY_KEY)


class _ChatCompletionsContract(ContractProviderAdapter):
    def _transport(self, scenario: str, port_name: str) -> ProviderTransport:
        client = build_chat_client(
            self.key,
            doubles.LEAKY_KEY,
            _SETTINGS,
            transport=doubles.chat_completions_transport(scenario, port_name=port_name),
        )
        return ChatCompletionsTransport(client, self._capabilities, api_key=doubles.LEAKY_KEY)


class _GeminiContract(ContractProviderAdapter):
    def _transport(self, scenario: str, port_name: str) -> ProviderTransport:
        client: GuardedHttpClient = build_gemini_client(
            doubles.LEAKY_KEY,
            _SETTINGS,
            transport=doubles.gemini_transport(scenario, port_name=port_name),
        )
        return GeminiTransport(client, self._capabilities, api_key=doubles.LEAKY_KEY)


class _OllamaContract(ContractProviderAdapter):
    def _transport(self, scenario: str, port_name: str) -> ProviderTransport:
        client = build_guarded_client(
            _OLLAMA_URL,
            _SETTINGS,
            transport=doubles.ollama_transport(scenario, port_name=port_name),
            resolver=_fixed_resolver,
            pinned_addresses=_OLLAMA_ADDRESSES,
        )
        return OllamaTransport(client, self._capabilities)


#: The registry the conformance suite reads. Every provider ADR-0005 commits to has
#: an entry; dropping one is an ADR revision, not a deleted line.
CONTRACT_ADAPTERS: Final[Mapping[str, ContractProviderAdapter]] = {
    "anthropic": _AnthropicContract("anthropic", anthropic_capabilities("claude-opus-5")),
    "openai": _ChatCompletionsContract(OPENAI_PROVIDER_CODE, openai_capabilities("gpt-4o")),
    "gemini": _GeminiContract("gemini", gemini_capabilities("gemini-1.5-pro")),
    "mistral": _ChatCompletionsContract(
        MISTRAL_PROVIDER_CODE, mistral_capabilities("mistral-small-latest")
    ),
    "ollama": _OllamaContract("ollama", _OLLAMA_CAPABILITIES),
}
