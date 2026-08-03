"""Domain ports for the two model-backed features of Chaudron (ADR-0005).

Two operations are expressed here as business verbs: *suggest recipes from what a
household has* and *read the lines off a photographed till receipt*. Neither
mentions a prompt, a message, a token or a vendor -- those belong to the
infrastructure layer, together with the five adapters that implement these ports.

Everything a caller needs in order to reason about a household's provider is in
this module and nowhere else:

* the two :class:`typing.Protocol` ports;
* the request/response value objects they exchange;
* :class:`ProviderCapabilities`, which carries its own *provenance* -- a static
  fact about a (provider, model) pair, or the answer an Ollama instance gave on a
  given date and which can therefore go stale;
* the degradation taxonomy of ADR-0005 as a single decision table, so that "what
  happens when this provider cannot do X" is a documented choice rather than
  whatever the code happens to do;
* the domain exceptions every adapter translates its vendor's failures into. No
  SDK exception ever crosses this boundary -- notably because an SDK error
  message can contain the household's API key (security review, SEC-003).
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "CAPABILITY_KEYS",
    "DEGRADATION_POLICY",
    "LONG_CONTEXT_MIN_TOKENS",
    "CapabilitySource",
    "CredentialDecryptionError",
    "DegradationStrategy",
    "InventoryItem",
    "LlmError",
    "ParsedReceipt",
    "ProviderCapabilities",
    "ProviderCapabilityUnavailable",
    "ProviderNotConfigured",
    "ProviderNotPermitted",
    "ProviderQuotaExceeded",
    "ProviderResponseInvalid",
    "ProviderUnavailable",
    "ReceiptLine",
    "ReceiptParser",
    "RecipeGenerator",
    "RecipeIngredient",
    "RecipeRequest",
    "RecipeSuggestion",
    "RecipeSuggestions",
]


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

#: Every capability the interface exposes. ADR-0005 designs for the *most* capable
#: provider, so each adapter must have an answer -- possibly "absent" -- for all of
#: them rather than the interface shrinking to the weakest common denominator.
CAPABILITY_KEYS: Final[tuple[str, ...]] = (
    "structured_output",
    "vision",
    "prompt_caching",
    "long_context",
)

#: Below this, the full inventory of a well-stocked household plus the instructions
#: no longer fit comfortably, and the recipe prompt has to be trimmed. The number is
#: a product decision, not a vendor one: it is what separates "send everything" from
#: the visible degraded mode.
LONG_CONTEXT_MIN_TOKENS: Final = 128_000


class CapabilitySource(enum.StrEnum):
    """Where a capability declaration comes from -- and therefore whether it ages.

    ADR-0005 makes this part of the *type* rather than a comment: the interface has
    to be able to say "this is a fact about the model" versus "this is what your
    Ollama answered on the 3rd of March", and only the second one can go stale and
    deserve a refresh button.
    """

    STATIC = "static"
    PROBED = "probed"


class DegradationStrategy(enum.StrEnum):
    """The three, and only three, ways ADR-0005 allows a missing capability to be handled."""

    #: Approximated by other means, with a documented loss (higher failure rate).
    EMULATED = "emulated"
    #: Offered in a reduced form, and the answer says what it left out.
    DEGRADED = "degraded"
    #: Refused outright, with the reason and the remedy.
    UNAVAILABLE = "unavailable"


#: The decision table ADR-0005 asks for, in one place rather than scattered across
#: five adapters and the UI. Each entry is a deliberate choice:
#:
#: * ``vision`` -- a model that has not seen the image cannot be allowed to invent a
#:   receipt, so receipt import is switched off with the reason shown.
#: * ``structured_output`` -- the JSON shape is asked for in the prompt instead and
#:   validated server-side with a bounded retry. The feature works; it fails more
#:   often, and the user is told so.
#: * ``prompt_caching`` -- the stable prefix is simply resent on every call. Same
#:   answers, more tokens: emulation with a cost, not a quality, penalty.
#: * ``long_context`` -- suggestions are computed on a subset of the inventory, and
#:   the answer states which part of the stock it looked at.
DEGRADATION_POLICY: Final[Mapping[str, DegradationStrategy]] = {
    "vision": DegradationStrategy.UNAVAILABLE,
    "structured_output": DegradationStrategy.EMULATED,
    "prompt_caching": DegradationStrategy.EMULATED,
    "long_context": DegradationStrategy.DEGRADED,
}


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What one (provider, model) pair can do, and how we know.

    Consumed identically whether it was read from an embedded table or obtained by
    asking a local instance; the difference lives in :attr:`source` and
    :attr:`probed_at` so the interface can offer a refresh only where that means
    something.
    """

    provider: str
    model: str
    context_window: int
    supports_structured_output: bool
    supports_vision: bool
    supports_prompt_caching: bool
    source: CapabilitySource = CapabilitySource.STATIC
    probed_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be a positive number of tokens")
        if self.source is CapabilitySource.PROBED:
            if self.probed_at is None:
                raise ValueError("probed capabilities must carry the date they were probed")
            if self.probed_at.tzinfo is None:
                raise ValueError("probed_at must be timezone-aware")
        elif self.probed_at is not None:
            raise ValueError("static capabilities cannot carry a probe date")

    @property
    def supports_long_context(self) -> bool:
        """Derived, not declared: the context window is the underlying fact."""
        return self.context_window >= LONG_CONTEXT_MIN_TOKENS

    def supports(self, capability: str) -> bool:
        if capability not in CAPABILITY_KEYS:
            raise KeyError(f"unknown capability {capability!r}")
        supported: bool = getattr(self, f"supports_{capability}")
        return supported

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(key for key in CAPABILITY_KEYS if not self.supports(key))

    def degradation_for(self, capability: str) -> DegradationStrategy | None:
        """``None`` when the capability is present, else the case that applies."""
        if self.supports(capability):
            return None
        return DEGRADATION_POLICY[capability]

    @property
    def is_degraded(self) -> bool:
        return bool(self.missing)

    def stale_since(self, now: dt.datetime, max_age: dt.timedelta) -> dt.timedelta | None:
        """How long a *probed* declaration has been older than ``max_age``.

        ``None`` for a static declaration, which cannot age, and for a probe that is
        still fresh. This is what the "your Ollama capabilities are from March"
        banner is rendered from.
        """
        if self.probed_at is None:
            return None
        age = now - self.probed_at
        return age - max_age if age > max_age else None


# --------------------------------------------------------------------------- #
# Recipe generation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class InventoryItem:
    """One line of what a household currently owns, as the domain sees it."""

    name: str
    quantity: str | None = None
    unit: str | None = None
    #: Negative for something already past its date; ``None`` when unknown.
    expires_in_days: int | None = None


@dataclass(frozen=True, slots=True)
class RecipeRequest:
    """Everything the domain knows when it asks for suggestions."""

    inventory: tuple[InventoryItem, ...]
    max_suggestions: int = 3
    #: Free-text constraints from the user ("quick, no oven").
    notes: str | None = None
    servings: int | None = None
    #: BCP-47 language the suggestions should be written in.
    language: str = "fr"

    def __post_init__(self) -> None:
        if self.max_suggestions < 1:
            raise ValueError("max_suggestions must be at least 1")


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    name: str
    amount: str | None = None
    unit: str | None = None
    #: Whether the ingredient was in the inventory that was sent.
    in_stock: bool = False


@dataclass(frozen=True, slots=True)
class RecipeSuggestion:
    title: str
    summary: str | None = None
    duration_minutes: int | None = None
    servings: int | None = None
    ingredients: tuple[RecipeIngredient, ...] = ()
    steps: tuple[str, ...] = ()
    uses_expiring_soon: bool = False


class RecipeSuggestions(list[RecipeSuggestion]):
    """The suggestions, plus the reason the answer is partial when it is.

    A ``list`` subclass rather than a wrapper object, because the port returns
    ``list[RecipeSuggestion]`` and that signature is what the services layer codes
    against. The extra attribute is what the persistent degraded-mode indicator
    renders: a degraded answer must say what it left out, or the user discovers the
    limit at the moment of the disappointment rather than before.
    """

    __slots__ = ("degradation_notice",)

    degradation_notice: str | None

    def __init__(
        self,
        items: Iterable[RecipeSuggestion] = (),
        *,
        degradation_notice: str | None = None,
    ) -> None:
        super().__init__(items)
        self.degradation_notice = degradation_notice


# --------------------------------------------------------------------------- #
# Receipt parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    label: str
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    total_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ParsedReceipt:
    lines: tuple[ReceiptLine, ...] = ()
    merchant: str | None = None
    purchased_on: dt.date | None = None
    currency: str | None = None
    total: Decimal | None = None
    #: Set when the answer is deliberately partial (ADR-0005, "degraded" case).
    degradation_notice: str | None = None


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


@runtime_checkable
class RecipeGenerator(Protocol):
    """Turn what a household owns into things it could cook tonight."""

    async def suggest(self, request: RecipeRequest) -> list[RecipeSuggestion]: ...


@runtime_checkable
class ReceiptParser(Protocol):
    """Turn a photographed till receipt into structured purchase lines."""

    async def parse(self, image: bytes, media_type: str) -> ParsedReceipt: ...


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """The three facts every support ticket needs, attached to every failure.

    ADR-0005 lists "harder user support" as a cost of the decision: a failure can
    come from the household's key, their quota, their Ollama, the model they chose,
    or from us. Carrying the provider, the model and the failure mode on the
    exception is what makes that distinguishable without reproducing the incident.
    """

    provider: str
    model: str
    #: Short, stable token naming *how* it failed (``timeout``, ``rate_limited``...).
    failure_mode: str | None = None


class LlmError(Exception):
    """Base class for every model-provider failure the domain understands.

    The message is always built from our own fields. A provider SDK's message is
    never interpolated: it can contain the API key it was called with, and a key in
    a log line survives every rotation policy (security review, SEC-003).
    """

    #: Human-readable, French-free, safe to log and safe to show a developer.
    def __init__(self, message: str, *, context: ProviderContext | None = None) -> None:
        self.context = context
        if context is not None:
            suffix = f" [provider={context.provider} model={context.model}"
            if context.failure_mode:
                suffix += f" failure={context.failure_mode}"
            suffix += "]"
            message += suffix
        super().__init__(message)


class ProviderUnavailable(LlmError):  # noqa: N818 -- name fixed by ADR-0005
    """The provider could not be reached, timed out, or answered with a server error."""


class ProviderQuotaExceeded(LlmError):  # noqa: N818 -- name fixed by ADR-0005
    """Rate limit or exhausted credit. The household, not the instance, must act."""


class ProviderResponseInvalid(LlmError):  # noqa: N818 -- name fixed by ADR-0005
    """The provider answered, but not with something we can turn into domain objects."""


class ProviderNotConfigured(LlmError):  # noqa: N818 -- name fixed by ADR-0005
    """No usable configuration: missing, invalid, or rejected credentials.

    Also the home of the single most predictable support ticket -- a consumer
    subscription (Claude Pro, ChatGPT Plus, Gemini Advanced) pasted where an API key
    is expected. The remediation belongs in the message, not in a document.
    """


class CredentialDecryptionError(ProviderNotConfigured):
    """The stored API key exists but cannot be turned back into a usable key.

    A subclass of :class:`ProviderNotConfigured` because that is what it means to the
    caller: there is no usable credential, and the way out is to enter the key again.
    It is a distinct type because the *cause* is different in kind -- the household
    did nothing wrong and its provider was never contacted; the instance's
    ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` was rotated, or the row was tampered with.

    The message names the environment variable and the remedy. Nothing else is said:
    an exception from this path carries no key, no ciphertext and no ``__cause__``
    (security review, SEC-003).
    """


class ProviderNotPermitted(ProviderNotConfigured):
    """The household asked for a mode it is not allowed to use.

    In practice: ``instance_owner``, which spends the operator's own money and is
    locked to the single household named by the instance's environment (ADR-0007).
    """


class ProviderCapabilityUnavailable(LlmError):  # noqa: N818 -- name fixed by ADR-0005
    """The feature is switched off because the configured model cannot do it.

    Deliberately *not* one of the three provider-failure types: nothing failed. This
    is the ``unavailable`` case of the ADR-0005 taxonomy, and it must carry both the
    reason and the way out.
    """

    def __init__(
        self,
        capability: str,
        *,
        context: ProviderContext | None = None,
        remedy: str | None = None,
    ) -> None:
        self.capability = capability
        self.remedy = remedy
        message = f"capability {capability!r} is not available with this configuration"
        if remedy:
            message += f"; {remedy}"
        super().__init__(message, context=context)


@dataclass(frozen=True, slots=True)
class DegradationNotice:
    """One line of the persistent degraded-mode indicator.

    Built from the capabilities alone, so the interface can show it *before* the
    user tries something rather than after it fails.
    """

    capability: str
    strategy: DegradationStrategy
    reason: str
    remedy: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """What ``GET /v1/providers/capabilities`` is rendered from."""

    capabilities: ProviderCapabilities
    notices: tuple[DegradationNotice, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        return bool(self.notices)
