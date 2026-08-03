"""Adapter conformance suite for the LLM provider ports (ADR-0005).

ADR-0005 accepts five adapters on the strength of one guarantee: *"adding a
provider is writing an adapter and making this suite pass"*. This module is that
suite. Without it the decision to support five providers would be imprudent, and
that is stated in the ADR itself.

Nothing here calls a real provider. Every case runs against a double supplied by
the adapter itself (see :class:`ContractAdapter`), so the suite costs nothing, is
deterministic, and needs no credentials. Verification against the live providers
exists, is marked ``live_provider``, and never runs in pull-request CI --
``docs/testing-strategy.md`` section 5.4 says when it does run.

**Current state: everything skips.** No adapter exists yet, and neither do the
domain ports. The suite is written so that it activates on its own, provider by
provider, the moment ``pantry.infra.llm.contract:CONTRACT_ADAPTERS`` gains an
entry -- no edit to this file is needed to start testing a new adapter.

What an adapter must expose to be tested is documented in `tests/README.md`.
"""

from __future__ import annotations

import datetime as dt
import importlib
import inspect
from collections.abc import Mapping
from types import ModuleType
from typing import Final, Protocol, cast, runtime_checkable

import pytest

pytestmark = pytest.mark.contract

# --------------------------------------------------------------------------- #
# The contract, as data
# --------------------------------------------------------------------------- #

#: The five adapters ADR-0005 commits to. Kept here, not derived from the registry:
#: a registry missing an entry must fail a test, not silently shrink the matrix.
PROVIDER_KEYS: Final[tuple[str, ...]] = ("anthropic", "openai", "gemini", "mistral", "ollama")

_REGISTRY_MODULE: Final = "pantry.infra.llm.contract"
_REGISTRY_ATTR: Final = "CONTRACT_ADAPTERS"
_PORTS_MODULE: Final = "pantry.domain.ports"
_ERRORS_MODULE: Final = "pantry.domain.errors"

#: Port name -> the single coroutine every implementation must provide.
_PORTS: Final[Mapping[str, str]] = {
    "RecipeSuggester": "suggest_recipes",
    "ReceiptExtractor": "extract_lines",
}

#: Failure scenario -> the domain exception it must surface as.
#:
#: The scenario names are the harness's vocabulary: an adapter builds a double that
#: reproduces that provider's own way of failing that way (an ``anthropic`` rate
#: limit and an ``ollama`` connection refusal look nothing alike on the wire, which
#: is the entire point of translating them here rather than in the services layer).
_FAILURE_TRANSLATIONS: Final[Mapping[str, str]] = {
    "connection_refused": "ProviderUnavailable",
    "timeout": "ProviderUnavailable",
    "server_error": "ProviderUnavailable",
    "rate_limited": "ProviderQuotaExceeded",
    "quota_exhausted": "ProviderQuotaExceeded",
    "malformed_payload": "ProviderResponseInvalid",
    "schema_violation": "ProviderResponseInvalid",
}

#: Capabilities the interface exposes. Designed for the most capable provider, so
#: every adapter must have an answer -- possibly "absent" -- for each of them.
_CAPABILITIES: Final[tuple[str, ...]] = (
    "structured_output",
    "vision",
    "prompt_caching",
    "long_context",
)

#: The three degradation cases of ADR-0005. Exactly one applies per missing
#: capability, and it is a documented decision rather than whatever the code does.
_DEGRADATION_STRATEGIES: Final[frozenset[str]] = frozenset({"emulated", "degraded", "unavailable"})


@runtime_checkable
class ContractAdapter(Protocol):
    """What each entry of ``CONTRACT_ADAPTERS`` must look like.

    Structural, so infrastructure never imports the test package. An adapter
    contributes a *description of itself*: how to build it wired to a double for a
    named scenario, what it claims to be capable of, and which degradation case it
    chose for each capability it lacks.
    """

    key: str

    def build_port(self, port_name: str, scenario: str) -> object:
        """Return the port implementation, wired to a double replaying ``scenario``.

        Raise :class:`LookupError` when the port is not offered at all -- that is a
        legitimate answer for a vision-less configuration.
        """

    def capabilities(self) -> object:
        """Return the ``ProviderCapabilities`` value for the tested (provider, model)."""

    def degradation_for(self, capability: str) -> str | None:
        """Return ``None`` when the capability is present, else a strategy name."""


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _import_optional(module_name: str) -> ModuleType | None:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None


def _load_registry() -> Mapping[str, object] | None:
    module = _import_optional(_REGISTRY_MODULE)
    if module is None:
        return None
    registry = getattr(module, _REGISTRY_ATTR, None)
    if not isinstance(registry, Mapping):
        return None
    return cast(Mapping[str, object], registry)


# Resolved at import time: collection must show one case per provider whether or not
# the implementation exists, so that the skip count is a visible, honest backlog.
_REGISTRY: Final = _load_registry()

_NO_REGISTRY_REASON: Final = (
    f"no LLM adapter registered: {_REGISTRY_MODULE}.{_REGISTRY_ATTR} does not exist yet. "
    "This suite activates by itself once an adapter is added to it; see tests/README.md."
)


def _require_domain_attr(module_name: str, attr: str) -> object:
    module = _import_optional(module_name)
    if module is None:
        pytest.skip(f"domain module {module_name} does not exist yet (ADR-0005 port layer)")
    resolved = getattr(module, attr, None)
    if resolved is None:
        pytest.skip(f"{module_name} exists but does not define {attr} yet")
    return resolved


@pytest.fixture(params=PROVIDER_KEYS)
def provider_key(request: pytest.FixtureRequest) -> str:
    return cast(str, request.param)


@pytest.fixture
def adapter(provider_key: str) -> ContractAdapter:
    if _REGISTRY is None:
        pytest.skip(_NO_REGISTRY_REASON)
    registered = _REGISTRY.get(provider_key)
    if registered is None:
        pytest.skip(f"no adapter registered for provider {provider_key!r}")
    # Registered but malformed is a failure, not a skip: it means an adapter opted
    # into the suite and then did not honour the shape it opted into.
    assert isinstance(registered, ContractAdapter), (
        f"{provider_key!r} is registered in {_REGISTRY_MODULE}.{_REGISTRY_ATTR} but does "
        f"not satisfy the ContractAdapter protocol (needs: key, build_port, "
        f"capabilities, degradation_for)"
    )
    return registered


# --------------------------------------------------------------------------- #
# 1. Port signatures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port_name", sorted(_PORTS))
def test_adapter_implements_the_domain_port(adapter: ContractAdapter, port_name: str) -> None:
    """The adapter is substitutable for the port the services layer depends on.

    Structural conformance is not enough on its own: the method must be a coroutine
    (the whole stack is async, ADR-0003) and must not have grown parameters the
    domain does not know how to supply.
    """
    port_type = _require_domain_attr(_PORTS_MODULE, port_name)
    try:
        implementation = adapter.build_port(port_name, "nominal")
    except LookupError:
        pytest.skip(f"{adapter.key} does not offer {port_name} in the tested configuration")

    method_name = _PORTS[port_name]
    port_class = cast(type, port_type)
    try:
        conforms = isinstance(implementation, port_class)
    except TypeError:
        # A Protocol that is not runtime-checkable: fall back to a structural check
        # rather than forcing the domain to decorate its ports for the tests' sake.
        conforms = hasattr(implementation, method_name)
    assert conforms, f"{adapter.key}: {type(implementation).__name__} does not satisfy {port_name}"

    method = getattr(implementation, method_name)
    assert inspect.iscoroutinefunction(method), (
        f"{adapter.key}.{method_name} must be a coroutine function"
    )

    expected = inspect.signature(getattr(port_class, method_name))
    actual = inspect.signature(method)
    assert list(actual.parameters) == [p for p in expected.parameters if p != "self"], (
        f"{adapter.key}.{method_name} signature drifted from {port_name}: {actual} vs {expected}"
    )


# --------------------------------------------------------------------------- #
# 2. Error translation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("scenario", "expected_error"), sorted(_FAILURE_TRANSLATIONS.items()))
@pytest.mark.parametrize("port_name", sorted(_PORTS))
async def test_provider_failures_become_domain_errors(
    adapter: ContractAdapter, port_name: str, scenario: str, expected_error: str
) -> None:
    """No SDK exception crosses the infrastructure boundary.

    The assertion on the exception's module is the load-bearing one. Checking only
    the type would pass for an SDK exception that happens to subclass the domain
    one, and the failure mode this prevents -- ``except AnthropicError`` appearing in
    a route -- is exactly what ADR-0005 set out to avoid.
    """
    error_type = cast(type[Exception], _require_domain_attr(_ERRORS_MODULE, expected_error))
    try:
        implementation = adapter.build_port(port_name, scenario)
    except LookupError:
        pytest.skip(f"{adapter.key} does not offer {port_name} in the tested configuration")

    method = getattr(implementation, _PORTS[port_name])
    with pytest.raises(error_type) as raised:
        await method()

    raised_module = type(raised.value).__module__
    assert raised_module.startswith("pantry."), (
        f"{adapter.key}: scenario {scenario!r} leaked {type(raised.value).__name__} from "
        f"{raised_module}; provider exceptions must be translated in the adapter"
    )
    assert str(raised.value), (
        f"{adapter.key}: {expected_error} raised for {scenario!r} carries no message. "
        "Support diagnosis needs the provider, the model and the failure mode "
        "(ADR-0005, 'Support utilisateur plus difficile')."
    )


# --------------------------------------------------------------------------- #
# 3. Capability declaration
# --------------------------------------------------------------------------- #


def test_capability_declaration_is_well_formed(adapter: ContractAdapter) -> None:
    """Capabilities are a value with a provenance, not a bag of booleans.

    ADR-0005 makes ``static`` vs ``probed`` part of the type: the UI must be able to
    tell "this is a fact about the model" from "this is what your Ollama answered on
    the 3rd of March", and only the second one can go stale.
    """
    capabilities = adapter.capabilities()

    for capability in _CAPABILITIES:
        assert hasattr(capabilities, f"supports_{capability}"), (
            f"{adapter.key}: capabilities declare nothing about {capability!r}"
        )
        assert isinstance(getattr(capabilities, f"supports_{capability}"), bool)

    source = getattr(capabilities, "source", None)
    assert source in {"static", "probed"}, (
        f"{adapter.key}: capability source must be 'static' or 'probed', got {source!r}"
    )

    probed_at = getattr(capabilities, "probed_at", None)
    if source == "probed":
        assert isinstance(probed_at, dt.datetime), (
            f"{adapter.key}: probed capabilities must be timestamped so the UI can "
            "offer a refresh when they age"
        )
        assert probed_at.tzinfo is not None, f"{adapter.key}: probed_at must be timezone-aware"
    else:
        assert probed_at is None, (
            f"{adapter.key}: static capabilities cannot have a probe date; the two "
            "provenances must stay distinguishable"
        )


def test_declared_provider_key_matches_registration(
    adapter: ContractAdapter, provider_key: str
) -> None:
    assert adapter.key == provider_key, (
        f"registered under {provider_key!r} but declares key {adapter.key!r}"
    )


# --------------------------------------------------------------------------- #
# 4. Degradation taxonomy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("capability", _CAPABILITIES)
def test_missing_capability_declares_a_taxonomy_case(
    adapter: ContractAdapter, capability: str
) -> None:
    """Every absent capability maps to exactly one of the three ADR-0005 cases.

    The point is not that a strategy exists but that it was *chosen*: a missing
    entry means the behaviour is whatever the code happens to do, which is the
    accident the taxonomy exists to prevent.
    """
    capabilities = adapter.capabilities()
    if getattr(capabilities, f"supports_{capability}", False):
        pytest.skip(f"{adapter.key} supports {capability}; nothing to degrade")

    strategy = adapter.degradation_for(capability)
    assert strategy in _DEGRADATION_STRATEGIES, (
        f"{adapter.key}: no degradation case declared for missing {capability!r}. "
        f"Expected one of {sorted(_DEGRADATION_STRATEGIES)} (ADR-0005)."
    )


@pytest.mark.parametrize("capability", _CAPABILITIES)
async def test_degradation_behaviour_matches_its_declared_case(
    adapter: ContractAdapter, capability: str
) -> None:
    """A declared case is a promise about behaviour, and this is where it is held.

    * ``unavailable`` -- the feature refuses, with a domain error carrying a reason.
      Never a raw provider error, never an answer invented without the input.
    * ``emulated`` -- the feature still returns a valid domain object; the loss is in
      the failure rate, not in the type.
    * ``degraded`` -- the feature answers *and* says what it left out, because the
      persistent degraded-mode indicator has nothing to display otherwise.
    """
    capabilities = adapter.capabilities()
    if getattr(capabilities, f"supports_{capability}", False):
        pytest.skip(f"{adapter.key} supports {capability}; nothing to degrade")

    strategy = adapter.degradation_for(capability)
    if strategy not in _DEGRADATION_STRATEGIES:
        pytest.skip("covered by test_missing_capability_declares_a_taxonomy_case")

    port_name = "ReceiptExtractor" if capability == "vision" else "RecipeSuggester"
    try:
        implementation = adapter.build_port(port_name, f"missing_{capability}")
    except LookupError:
        pytest.skip(f"{adapter.key} does not offer {port_name} in the tested configuration")

    method = getattr(implementation, _PORTS[port_name])

    if strategy == "unavailable":
        # Deliberately not pinned to a named exception: ADR-0005 names three error
        # types for provider *failures*, and a refused capability is not one of them.
        # What matters, and what is asserted, is that it comes from the domain.
        with pytest.raises(Exception) as raised:
            await method()
        assert type(raised.value).__module__.startswith("pantry."), (
            f"{adapter.key}: an unavailable capability must fail with a domain error"
        )
        assert str(raised.value), (
            f"{adapter.key}: the user must be told why {capability!r} is unavailable "
            "and what to do about it"
        )
        return

    result = await method()
    assert result is not None, f"{adapter.key}: {strategy!r} must still produce a result"

    if strategy == "degraded":
        notice = getattr(result, "degradation_notice", None)
        assert notice, (
            f"{adapter.key}: a degraded answer must state what it left out "
            f"(missing {capability!r}); the persistent indicator renders this text"
        )


# --------------------------------------------------------------------------- #
# 5. Registry completeness
# --------------------------------------------------------------------------- #


def test_every_adr_provider_is_registered() -> None:
    """The matrix cannot shrink silently.

    ADR-0005 lists five adapters; dropping one is a decision to record in an ADR
    revision, not a line quietly removed from a dict.
    """
    if _REGISTRY is None:
        pytest.skip(_NO_REGISTRY_REASON)
    missing = [key for key in PROVIDER_KEYS if key not in _REGISTRY]
    assert not missing, (
        f"ADR-0005 commits to {list(PROVIDER_KEYS)}; missing from "
        f"{_REGISTRY_MODULE}.{_REGISTRY_ATTR}: {missing}"
    )


# --------------------------------------------------------------------------- #
# 6. Live providers -- opt-in, never in pull-request CI
# --------------------------------------------------------------------------- #


@pytest.mark.live_provider
async def test_live_provider_still_honours_the_contract(adapter: ContractAdapter) -> None:
    """The doubles above assume the provider's wire behaviour has not changed.

    That assumption decays: SDKs ship breaking releases, models get deprecated, error
    payloads are reshaped. This test is the only thing that notices, and it costs
    real money and real credentials -- hence opt-in, on a schedule, never on a pull
    request. See `docs/testing-strategy.md` section 5.4.
    """
    pytest.skip(
        "live provider verification is not implemented yet: it needs a real adapter, "
        "per-provider credentials from the environment, and a spend cap. Enabling it "
        "before those exist would either fail or silently test nothing."
    )
