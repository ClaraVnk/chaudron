"""Which model provider a household has, what it can promise, and how it is set up.

This is the read side of ADR-0005: it answers "what will happen if the user presses
the button?" *before* they press it. Two callers, one resolution path:

* ``GET /v1/providers/capabilities`` renders :class:`ProviderView`, which the PWA
  shows permanently rather than at the moment of failure;
* the recipe service asks for :class:`ActiveProvider`, and gets a refusal it can
  turn into a 409 when the household has nothing usable;
* the receipt import asks for :class:`ActiveReceiptParser`, and additionally
  refuses when the configured model has no vision -- the ``unavailable`` case of
  the ADR-0005 taxonomy, because a model that has not seen the image would
  happily produce a plausible receipt.

All three go through :meth:`ProviderService._load`, so the answers cannot disagree
-- an interface that says "ready" for a configuration the next call refuses is
worse than one that says nothing.

The refusal texts are in **French and in plain language** on purpose: they are user
interface, not diagnostics. Everything else in this file -- identifiers, comments,
exception messages -- stays in English, as does every log line.

One limit is deliberate and documented rather than hidden: the status is *static*.
It describes the stored configuration and never calls the provider, so it cannot know
that a key was revoked this morning. Probing on a status endpoint would spend the
household's money to draw a banner.

**One exception to "never calls anyone", and it is not a call to the provider.**
The consent exemption for ``ollama`` used to key on the mode enum; it now keys on
whether the endpoint is on a network the household controls, which for a hostname
means one DNS lookup (:func:`endpoint_is_local`). It runs only for an ``ollama``
row that carries no consent and whose ``base_url`` names a host rather than an
address -- the co-located topology of ADR-0007 -- and it is the same resolution the
factory performs a few lines later on that path anyway. Nothing is sent anywhere,
no credential is read, and the answer is re-taken on every load rather than cached,
so an endpoint that stops being local stops being exempt at the next request.

Mode ``byok`` -- the mode ADR-0007 is built around -- is fully served here. The
ciphertext is a deferred column, so reading it is an explicit gesture
(:meth:`ProviderService._sealed_credential`) rather than something a stray
``select`` can do by accident, and the plaintext never leaves the factory. ``status``
does decrypt, and throws the result away: ``configured`` means *usable*, and an
instance whose ``CHAUDRON_CREDENTIAL_ENCRYPTION_KEY`` was rotated must say so on the
banner instead of promising a call that would fail on the first click.

The **write** side lives at the bottom of this module and has the opposite posture
throughout. :class:`ProviderCredentialService` is the only place that accepts a key;
:class:`ProviderConfigService` is the only place that creates, alters, archives or
consents a configuration, and neither can produce a key: the view they return has
no field that could hold one, so a handler cannot disclose one by forgetting to
strip it -- the same structural argument ``services/export_targets.py`` makes.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.config import Settings
from chaudron.domain.llm_ports import (
    DEGRADATION_POLICY,
    CapabilitySource,
    CredentialDecryptionError,
    DegradationNotice,
    DegradationStrategy,
    LlmError,
    ProviderCapabilities,
    ProviderCapabilityUnavailable,
    ProviderNotConfigured,
    ReceiptParser,
    RecipeGenerator,
)
from chaudron.domain.models import (
    LlmConfigStatus,
    LlmProvider,
    LlmProviderConfig,
    LlmProviderMode,
)
from chaudron.infra.crypto import MIN_API_KEY_LENGTH, CredentialCipher, SealedCredential
from chaudron.infra.llm.anthropic_provider import ANTHROPIC_MODELS, anthropic_capabilities
from chaudron.infra.llm.factory import (
    BYOK_PROVIDERS,
    HouseholdProviderConfig,
    LlmProviderFactory,
)
from chaudron.infra.llm.gemini_provider import GEMINI_MODELS, gemini_capabilities
from chaudron.infra.llm.http import Resolver, system_resolver
from chaudron.infra.llm.ollama_provider import build_pinned_client, probe_capabilities
from chaudron.infra.llm.openai_compatible import (
    MISTRAL_MODELS,
    OPENAI_MODELS,
    mistral_capabilities,
    openai_capabilities,
)
from chaudron.infra.llm.settings import LlmSettings

logger = logging.getLogger(__name__)

#: ``llm_provider.code`` of the self-hosted provider, whose timeout budget is the
#: generous one: a local model on a small machine is slow, not broken.
OLLAMA_PROVIDER_CODE: Final = "ollama"

#: Which instance-owned key pays for which provider. The keys are the reference
#: codes seeded by migration ``0002``; a provider absent from this map simply has no
#: instance key, which the factory reports as "not configured".
_INSTANCE_OWNER_KEYS: Final[dict[str, Callable[[Settings], SecretStr | None]]] = {
    "anthropic": lambda settings: settings.anthropic_api_key,
    "openai": lambda settings: settings.openai_api_key,
    "gemini": lambda settings: settings.gemini_api_key,
    "mistral": lambda settings: settings.mistral_api_key,
}


class ProviderPorts(Protocol):
    """The slice of :class:`LlmProviderFactory` this service uses.

    Structural, so a test can substitute a factory wired to a double transport --
    the real adapter, a fake socket -- without the service knowing and without
    spending anything.

    ``recipe_generator`` and ``receipt_parser`` are coroutines because building the
    Ollama adapter resolves the household's hostname first, and that resolution *is*
    the anti-rebinding pin (``infra/llm/factory.py``). ``capabilities_for`` stays
    synchronous: it never calls anyone, which is what lets the capabilities endpoint
    answer without spending the household's money.
    """

    def capabilities_for(self, config: HouseholdProviderConfig) -> ProviderCapabilities: ...

    async def recipe_generator(self, config: HouseholdProviderConfig) -> RecipeGenerator: ...

    async def receipt_parser(self, config: HouseholdProviderConfig) -> ReceiptParser: ...


#: Built per request from the household's provider code: the instance key that
#: ``instance_owner`` may spend is per vendor, and only one of them applies.
type ProviderPortsBuilder = Callable[[str], ProviderPorts]


# --------------------------------------------------------------------------- #
# What the banner says
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A configuration that exists but cannot be used, and why -- in French.

    ``code`` is the English token that goes in the log and in the domain error; the
    two other fields are what the user reads.
    """

    code: str
    reason: str
    remedy: str

    def notice(self) -> DegradationNotice:
        return DegradationNotice(
            capability="configuration",
            strategy=DegradationStrategy.UNAVAILABLE,
            reason=self.reason,
            remedy=self.remedy,
        )


# --------------------------------------------------------------------------- #
# Where the remedies send the reader
# --------------------------------------------------------------------------- #
#
# Every remedy below that involves editing a configuration names ONE place, and
# it is named through this constant rather than spelled out five times.
#
# It used to read "dans la configuration du foyer". Nothing about that was true
# of the interface: `frontend/src/features/providers/ProviderConfigPanel.tsx` is
# mounted by `features/recipes/ProviderSetup.tsx`, which the recipes tab renders
# -- and the application also has a tab literally called *Foyer*, which is where
# a reader following that sentence goes and where there is no provider form at
# all. A remedy that names the wrong screen is worse than one that names none: it
# spends the reader's trust before it fails them.
#
# If the panel is ever also mounted on the household screen -- ProviderSetup's
# docstring calls that a one-line change -- this constant is the one line of copy
# that has to move with it.
_WHERE_TO_CONFIGURE: Final = "l'onglet Recettes"

#: What the receipt import says when nothing is configured at all. Its own
#: sentence rather than the recipe one, because the two features fail for the same
#: reason and are fixed on the same screen, but a user who has never asked for a
#: recipe should not be told about recipes.
NO_PROVIDER_FOR_RECEIPTS: Final = _Refusal(
    code="not_configured",
    reason=(
        # "utilisable" is load-bearing: the receipt path raises
        # `ProviderNotConfigured` both for a household that has registered nothing
        # and for one whose configuration exists but is refused -- a withdrawn
        # consent, a key the provider rejected. Saying flatly that nothing is
        # configured would contradict the screen that lists the configuration.
        "Aucun fournisseur de modèle utilisable n'est configuré pour ce foyer : la "
        "photo d'un ticket ne peut donc pas être lue."
    ),
    remedy=(
        f"Enregistrez un fournisseur multimodal depuis {_WHERE_TO_CONFIGURE}, ou "
        "importez le PDF de votre commande drive, qui se lit sans modèle."
    ),
)

_DISABLED: Final = _Refusal(
    code="config_disabled",
    reason="La configuration du fournisseur de ce foyer est désactivée.",
    remedy=f"Réactivez-la depuis {_WHERE_TO_CONFIGURE}, ou enregistrez-en une autre.",
)

_INVALID_CREDENTIALS: Final = _Refusal(
    code="invalid_credentials",
    reason="Le fournisseur a refusé la clé enregistrée pour ce foyer.",
    remedy=f"Remplacez la clé depuis {_WHERE_TO_CONFIGURE}.",
)

_KEY_UNDECRYPTABLE: Final = _Refusal(
    code="credential_undecryptable",
    reason=(
        "La clé enregistrée pour ce foyer ne peut pas être déchiffrée : la clé de "
        "chiffrement de cette instance n'est plus celle qui l'a enregistrée."
    ),
    remedy="Saisissez à nouveau votre clé pour la réenregistrer.",
)

_KEY_MISSING: Final = _Refusal(
    code="credential_missing",
    reason="Aucune clé n'est enregistrée pour cette configuration.",
    remedy="Saisissez la clé d'API de votre fournisseur.",
)

#: No agreement on record for sending this household's data to a third party
#: (RGPD art. 6(1)(a); art. 9(2)(a) for the health signals a recipe prompt carries).
#: Raised for every mode *except* an ``ollama`` whose endpoint is on a network the
#: household controls -- see the docstring of
#: :meth:`ProviderService._consent_refusal`.
_CONSENT_MISSING: Final = _Refusal(
    code="consent_missing",
    reason=(
        "Ce foyer n'a pas donné son accord pour que ses données soient envoyées à ce "
        "fournisseur de modèle, qui est un tiers."
    ),
    remedy=(
        f"Donnez votre accord depuis {_WHERE_TO_CONFIGURE}, ou choisissez le mode "
        "Ollama, qui fait tourner le modèle sur votre machine et n'envoie rien."
    ),
)

#: The same missing agreement, for the one case where :data:`_CONSENT_MISSING`
#: would give advice that cannot be followed: an ``ollama`` whose endpoint is *not*
#: local. Telling that household to "choose the Ollama mode, which sends nothing"
#: when Ollama is precisely what it chose reads as the application not having
#: understood the question. The endpoint is what changed the answer, so the
#: endpoint is what the sentence names.
_CONSENT_MISSING_REMOTE_ENDPOINT: Final = _Refusal(
    code="consent_missing",
    reason=(
        "Ce foyer n'a pas donné son accord pour que ses données soient envoyées à ce "
        "serveur Ollama, qui n'est pas sur un réseau qu'il contrôle : son adresse est "
        "publique, donc un tiers les reçoit."
    ),
    remedy=(
        f"Donnez votre accord depuis {_WHERE_TO_CONFIGURE}, ou indiquez un serveur "
        "Ollama hébergé sur cette machine ou sur votre réseau local."
    ),
)

#: The agreement existed and the household took it back. A separate sentence from
#: :data:`_CONSENT_MISSING` because the two are different situations to be in: one
#: is a step never taken, the other a decision made on purpose, and telling somebody
#: who has just withdrawn their consent that they never gave any reads as the
#: application having lost it.
_CONSENT_REVOKED: Final = _Refusal(
    code="consent_revoked",
    reason=("Ce foyer a retiré son accord pour l'envoi de ses données à ce fournisseur de modèle."),
    remedy=(
        f"Redonnez votre accord depuis {_WHERE_TO_CONFIGURE} si vous souhaitez "
        "réutiliser ce fournisseur."
    ),
)

_UNPROBED: Final = _Refusal(
    code="ollama_unprobed",
    reason=(
        "Les capacités de ce serveur Ollama n'ont jamais été détectées : on ignore "
        "ce que le modèle installé sait faire."
    ),
    remedy="Enregistrez la configuration à nouveau pour interroger le serveur.",
)

_UNKNOWN_MODEL: Final = _Refusal(
    code="unknown_model",
    reason=(
        "Cette instance ne connaît pas le modèle configuré : elle ne peut donc pas "
        "dire ce qu'il sait faire."
    ),
    remedy="Choisissez un modèle proposé par la liste, puis enregistrez à nouveau.",
)

#: One sentence per missing capability, in the vocabulary of someone cooking
#: dinner. The strategy that goes with each is not decided here: it comes from
#: ``DEGRADATION_POLICY``, which is the single decision table of ADR-0005.
_DEGRADED_REASONS: Final[dict[str, str]] = {
    # Deliberately narrower than "l'import de tickets est désactivé", which is what
    # this said and which was wider than the control behind it. Only the *photo*
    # path goes through the model; a PDF order recap is read straight out of the
    # document, with no provider involved at all, and works on an instance that has
    # none. Telling a household its receipt import is off when half of it works is
    # the same failure as telling it nothing was omitted when something was.
    "vision": (
        "Le modèle configuré ne sait pas lire les images : photographier un ticket "
        "de caisse est indisponible. L'import d'un récapitulatif de commande en PDF "
        "fonctionne toujours, il ne passe par aucun modèle."
    ),
    "structured_output": (
        "Le modèle configuré ne garantit pas de réponse structurée : le format lui "
        "est demandé dans les instructions puis vérifié ici, ce qui échoue plus "
        "souvent."
    ),
    "prompt_caching": (
        "Le modèle configuré ne met pas les instructions en cache : elles sont "
        "renvoyées à chaque demande. Mêmes réponses, mais davantage de jetons "
        "facturés."
    ),
    "long_context": (
        "La fenêtre de contexte du modèle configuré est courte : les suggestions ne "
        "portent que sur les produits les plus proches de leur date limite, pas sur "
        "tout l'inventaire."
    ),
}

_DEGRADED_REMEDIES: Final[dict[str, str]] = {
    "vision": (
        f"Depuis {_WHERE_TO_CONFIGURE}, choisissez un modèle multimodal, puis relancez "
        "la détection des capacités."
    ),
    "long_context": (
        f"Depuis {_WHERE_TO_CONFIGURE}, choisissez un modèle à plus grande fenêtre de "
        "contexte pour que tout le stock soit pris en compte."
    ),
}


# --------------------------------------------------------------------------- #
# Where an Ollama endpoint actually is
# --------------------------------------------------------------------------- #
#
# The consent exemption used to be `row.mode is OLLAMA`, and the penetration test
# of 2026-08-04 (design note under finding O-02) is why it is not any more:
#
#     The consent gate exempts `mode = 'ollama'` on the reasoning that the model
#     runs on a machine the household controls. But `base_url` is a per-row column,
#     constrained only by the operator's `ollama_allowed_hosts`. An operator who
#     allowlists a hosted Ollama produces a configuration that transmits health data
#     to a third party **with the consent gate disabled by mode**. […] the exemption
#     should key on the endpoint being loopback or RFC 1918, not on the enum.
#
# The premise the exemption rests on is *no transmission*, and no enum value can
# establish that: `ollama` is a wire protocol, not a location. What establishes it
# is the address the request would be sent to.

#: Hostnames RFC 6761 section 6.3 reserves for the loopback interface. Resolved
#: without asking a resolver, because a name server that answered otherwise for
#: these would be misconfigured or hostile, and either way the answer must not
#: widen the exemption.
_LOOPBACK_NAMES: Final = frozenset({"localhost"})

_DEFAULT_SCHEME_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


def _is_local_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether reaching this address means staying on a network somebody here owns.

    ``is_private`` is Python's name for "not globally routable", and it is wider
    than RFC 1918: it also covers loopback, IPv4 link-local (169.254/16), IPv6
    unique-local (fc00::/7) and the reserved documentation blocks. Every one of
    those is a network the operator or the household is on -- a packet to it does
    not leave the premises -- which is exactly the property the exemption needs.
    ``is_loopback`` is a subset and is named anyway, because it is the case the
    reader is looking for.

    Anything else is somebody's data centre, whatever the enum says.
    """
    return address.is_loopback or address.is_private


async def endpoint_is_local(base_url: str | None, resolver: Resolver) -> bool:
    """Whether this endpoint is reachable without the data leaving a local network.

    **Fail-closed at every step.** A URL that cannot be parsed, a host that cannot
    be resolved, a resolver answer with an address this cannot classify: all of
    them return ``False``, which means "consent is required". The failure mode of
    the opposite default is a household's health data reaching a third party with
    the gate switched off, which is the finding this function exists to close.

    **A name is resolved rather than guessed at.** The documented ADR-0007
    topology is a co-located container reached as ``http://ollama:11434``, and that
    name is not an address -- it resolves to an RFC 1918 address on the Podman
    network, and refusing to look would demand a consent for a configuration that
    genuinely transmits to nobody. Every address in the answer has to be local:
    a name resolving to one private and one public address is a name that can send
    the request either way.

    The answer is not cached. It costs one ``getaddrinfo`` on the one path that
    reaches it, it is the same lookup ``build_pinned_client`` makes immediately
    afterwards, and caching it would reintroduce in this module precisely the
    staleness ``GuardedHttpClient.assert_stable_resolution`` exists to prevent.
    """
    if not base_url:
        return False
    try:
        url = urlsplit(base_url.strip())
        host, port = url.hostname, url.port
    except ValueError:
        # `urlsplit` defers parsing the port until it is read, and raises here for
        # a URL such as `http://ollama:not-a-port`.
        return False
    if not host:
        return False

    try:
        return _is_local_address(ipaddress.ip_address(host))
    except ValueError:
        # Not a literal: it is a name, and names are resolved below.
        pass

    if host.lower() in _LOOPBACK_NAMES or host.lower().endswith(".localhost"):
        return True

    try:
        addresses = await resolver(host, port or _DEFAULT_SCHEME_PORTS.get(url.scheme, 80))
    except LlmError, OSError:
        # `system_resolver` turns a failed lookup into ProviderUnavailable. An
        # endpoint nobody can resolve is an endpoint nobody can show to be local.
        return False
    if not addresses:
        return False
    try:
        return all(_is_local_address(ipaddress.ip_address(address)) for address in addresses)
    except ValueError:
        # A scoped link-local such as `fe80::1%eth0`. `infra/llm/http.py` refuses to
        # dial one for the same reason: an answer this cannot classify must not be
        # silently dropped from the set it is being judged on.
        return False


def degradation_notices(capabilities: ProviderCapabilities) -> tuple[DegradationNotice, ...]:
    """One notice per capability the configuration lacks, in policy order."""
    return tuple(
        DegradationNotice(
            capability=capability,
            strategy=DEGRADATION_POLICY[capability],
            reason=_DEGRADED_REASONS[capability],
            remedy=_DEGRADED_REMEDIES.get(capability),
        )
        for capability in capabilities.missing
    )


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderView:
    """What ``GET /v1/providers/capabilities`` is rendered from.

    ``configured`` means *usable*, not *present*: a stored configuration nobody can
    call is reported as unconfigured, with the reason attached, because that is the
    state the interface has a screen for.
    """

    configured: bool
    mode: str | None = None
    provider: str | None = None
    model: str | None = None
    supports_vision: bool = False
    supports_structured_output: bool = False
    notices: tuple[DegradationNotice, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.notices)


@dataclass(frozen=True, slots=True)
class ActiveProvider:
    """A configuration the factory accepted, ready to be called."""

    config_id: uuid.UUID
    config: HouseholdProviderConfig
    capabilities: ProviderCapabilities
    generator: RecipeGenerator


@dataclass(frozen=True, slots=True)
class ActiveReceiptParser:
    """A configuration the factory accepted *and* that can see an image.

    Separate from :class:`ActiveProvider` rather than a wider version of it: the
    vision check is a precondition of this one and of nothing else, and a single
    type carrying an optional parser would let a caller reach for it without the
    check having run.
    """

    config_id: uuid.UUID
    config: HouseholdProviderConfig
    capabilities: ProviderCapabilities
    parser: ReceiptParser


class ProviderService:
    def __init__(
        self,
        session: AsyncSession,
        build_ports: ProviderPortsBuilder,
        cipher: CredentialCipher,
        *,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._session = session
        self._build_ports = build_ports
        self._cipher = cipher
        #: What decides whether an ``ollama`` endpoint is local, and therefore
        #: whether the consent exemption applies to it. Defaults to the real system
        #: resolver so production is armed by omission rather than by remembering --
        #: the same argument :func:`provider_ports_builder` makes for its own. The
        #: parameter exists so a test can prove both verdicts without a name server.
        self._resolver = resolver

    async def status(self, household_id: uuid.UUID) -> ProviderView:
        """Describe the household's provider. Never raises, never calls anyone."""
        row, refusal = await self._load(household_id)
        if row is None:
            return ProviderView(
                configured=False, notices=() if refusal is None else (refusal.notice(),)
            )
        if refusal is not None:
            return _view(row, configured=False, notices=(refusal.notice(),))

        sealed = await self._sealed_credential(row)
        if row.mode is LlmProviderMode.BYOK:
            if sealed is None:
                return _view(row, configured=False, notices=(_KEY_MISSING.notice(),))
            try:
                # The result is discarded on purpose: the question this endpoint
                # answers is "would the next call work?", and only an actual
                # decryption answers it. A rotated master key must show on the banner,
                # not on the first click.
                self._cipher.decrypt(sealed)
            except CredentialDecryptionError:
                return _view(row, configured=False, notices=(_KEY_UNDECRYPTABLE.notice(),))

        config = _to_provider_config(row, sealed)
        try:
            capabilities = self._build_ports(config.provider_code).capabilities_for(config)
        except ProviderNotConfigured:
            # The only two ways a stored configuration has no knowable capabilities:
            # an Ollama nobody probed, or a model this instance's table ignores.
            unusable = _UNPROBED if row.mode is LlmProviderMode.OLLAMA else _UNKNOWN_MODEL
            return _view(row, configured=False, notices=(unusable.notice(),))

        return _view(
            row,
            configured=True,
            notices=degradation_notices(capabilities),
            capabilities=capabilities,
        )

    async def for_recipes(self, household_id: uuid.UUID) -> ActiveProvider:
        """The provider that generates this household's recipes.

        Raises :class:`ProviderNotConfigured` -- or one of its subclasses,
        ``ProviderNotPermitted`` from the factory and ``CredentialDecryptionError``
        from the cipher -- when there is nothing usable. The API layer turns all of
        them into the 409 the client has a configuration screen for.
        """
        row, refusal = await self._load(household_id)
        if row is None:
            raise ProviderNotConfigured("this household has no model provider configured")
        if refusal is not None:
            raise ProviderNotConfigured(
                f"the model provider of this household cannot be used: {refusal.code}"
            )

        config = _to_provider_config(row, await self._sealed_credential(row))
        ports = self._build_ports(config.provider_code)
        capabilities = ports.capabilities_for(config)
        return ActiveProvider(
            config_id=row.id,
            config=config,
            capabilities=capabilities,
            generator=await ports.recipe_generator(config),
        )

    async def for_receipts(self, household_id: uuid.UUID) -> ActiveReceiptParser:
        """The provider that reads this household's photographed receipts.

        Two refusals, and they are different in kind. :class:`ProviderNotConfigured`
        means there is nothing usable and the household has a configuration screen
        for it. :class:`ProviderCapabilityUnavailable` means the configuration is
        perfectly fine and the model simply cannot see -- ADR-0005's ``unavailable``
        case, raised *here* rather than at the first byte of the image, so the
        interface can grey the button out with the reason instead of spending an
        upload to discover it.

        The vision check is not conditional on the provider. ADR-0005 warns that
        trusting a frontier model and distrusting a local one is exactly the shortcut
        the arithmetic laundering of section 3.4 makes dangerous, so the rule is the
        capability and nothing else.

        **This is the same configuration recipes use, and since revision ``0025``
        there is no other.** A household that wants a vision-less local model for
        recipes and a hosted one for photographed receipts has to pick one of the
        two; the refusal raised here is what it gets for the other. Nothing breaks
        on that path -- the notice names the missing capability and the remedy, and
        a PDF order recap is read with no model at all -- but the arrangement is
        genuinely no longer available. ``docs/data-model.md`` §4.13 records why.
        """
        row, refusal = await self._load(household_id)
        if row is None:
            raise ProviderNotConfigured(
                "this household has no model provider configured for receipt parsing"
            )
        if refusal is not None:
            raise ProviderNotConfigured(
                f"the model provider of this household cannot be used: {refusal.code}"
            )

        config = _to_provider_config(row, await self._sealed_credential(row))
        ports = self._build_ports(config.provider_code)
        capabilities = ports.capabilities_for(config)
        if not capabilities.supports_vision:
            raise ProviderCapabilityUnavailable(
                "vision",
                remedy=_DEGRADED_REMEDIES["vision"],
            )
        return ActiveReceiptParser(
            config_id=row.id,
            config=config,
            capabilities=capabilities,
            parser=await ports.receipt_parser(config),
        )

    async def _sealed_credential(self, row: LlmProviderConfig) -> SealedCredential | None:
        """Read the encrypted key of a ``byok`` row -- the deliberate gesture.

        ``api_key_ciphertext`` is a deferred column so that no ordinary
        ``select(LlmProviderConfig)`` can load a secret by accident
        (``docs/data-model.md`` §9.2). Fetching it is therefore its own query, named,
        greppable and reachable only from here -- and it is skipped entirely for the
        two modes the database already forbids from carrying a ciphertext.
        """
        if row.mode is not LlmProviderMode.BYOK:
            return None
        ciphertext = await self._session.scalar(
            select(LlmProviderConfig.api_key_ciphertext).where(
                LlmProviderConfig.household_id == row.household_id,
                LlmProviderConfig.id == row.id,
            )
        )
        if ciphertext is None or row.api_key_encryption_key_id is None:
            return None
        return SealedCredential(
            household_id=row.household_id,
            config_id=row.id,
            ciphertext=ciphertext,
            key_id=row.api_key_encryption_key_id,
        )

    async def _load(
        self, household_id: uuid.UUID
    ) -> tuple[LlmProviderConfig | None, _Refusal | None]:
        """The household's live configuration, and why it is unusable.

        There is at most one, and that is a schema guarantee rather than an
        expectation: ``uq_llm_provider_config_household_active`` is unique over
        ``household_id`` where ``archived_at IS NULL``. So no ordering is needed to
        make the answer deterministic and no plurality has to be arbitrated -- the
        query returns the row or nothing, and ``scalar`` says exactly that.

        The purpose the caller has in mind no longer selects between rows. It still
        decides what the row must be able to *do* -- :meth:`for_receipts` refuses a
        model without vision -- but that question is asked of the capabilities, not
        of the configuration set.
        """
        row = await self._session.scalar(
            select(LlmProviderConfig).where(
                LlmProviderConfig.household_id == household_id,
                LlmProviderConfig.archived_at.is_(None),
            )
        )
        if row is None:
            return None, None
        # Before status, and before `_sealed_credential` is ever reached: consent is
        # the question of whether this household's data may leave at all, so it is
        # answered before the questions about whether the departure would succeed.
        # `infra/todo/factory.py` orders the Todoist chain the same way, and a
        # penetration test confirmed it there by counting outbound calls after a
        # withdrawal: zero.
        consent = await self._consent_refusal(row)
        if consent is not None:
            return row, consent
        if row.status is LlmConfigStatus.DISABLED:
            return row, _DISABLED
        if row.status is LlmConfigStatus.INVALID_CREDENTIALS:
            return row, _INVALID_CREDENTIALS
        return row, None

    async def _consent_refusal(self, row: LlmProviderConfig) -> _Refusal | None:
        """Whether this household has agreed to its data reaching a third party.

        **Local inference is exempt, and "local" is a property of the endpoint.**
        The exemption is deliberate: a model running on a machine the household
        controls receives no transmission, so there is no art. 6(1)(a) consent to
        collect. ``docs/security-model.md`` §8.3 makes it an explicit requirement --
        "the ``ollama`` mode must remain fully functional without this consent" --
        and ADR-0007 rests on it: a household unwilling to send anything anywhere
        still gets the whole feature. Asking for agreement to send data to one's own
        computer would also teach people to click past consent screens, which costs
        more than it collects.

        What it does *not* rest on is the enum. ``mode = 'ollama'`` says which wire
        protocol is spoken; ``base_url`` says where. The two came apart in the
        penetration test of 2026-08-04: an operator who allowlists a **hosted**
        Ollama produces a row that ships a child's age band and a member's health
        note to a third party with this gate switched off by mode. So the question
        asked here is :func:`endpoint_is_local`, and a hosted Ollama is treated
        exactly like Anthropic -- because for the household's data, it is.

        Every other mode reaches a third party unconditionally. ``byok`` sends under
        the household's own key and ``instance_owner`` under the operator's, which
        changes who pays and not who receives -- and the prompt carries a child's age
        band, a member's free-text health note, or an entire receipt photograph
        either way.

        Read on every load rather than cached, so a withdrawal takes effect at the
        next request. That is the property that makes the consent revocable in the
        sense art. 7(3) means, rather than revocable at the next restart -- and it is
        why the endpoint verdict is re-taken here too rather than frozen into a
        column at save time, where a name that later points elsewhere would keep an
        exemption it no longer earns.

        **A withdrawal is honoured before the exemption is even considered**, and
        the order of the branches below is that decision. A household that has said
        "stop" has said it about this configuration, not about a network topology,
        and answering "the endpoint is local, so your instruction does not apply"
        would be the application arguing with a data subject. It also means the
        lookup runs on exactly one path -- an Ollama with no agreement on record,
        which is the co-located case the exemption exists for -- and on none of the
        others.
        """
        if row.consent_revoked_at is not None:
            return _CONSENT_REVOKED
        if row.consented_at is not None:
            return None
        if row.mode is LlmProviderMode.OLLAMA and await endpoint_is_local(
            row.base_url, self._resolver
        ):
            return None
        return (
            _CONSENT_MISSING_REMOTE_ENDPOINT
            if row.mode is LlmProviderMode.OLLAMA
            else _CONSENT_MISSING
        )


# --------------------------------------------------------------------------- #
# Writing a key
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StoredKey:
    """Everything the caller is allowed to know after storing a key.

    Deliberately not the key. ``last4`` is the whole of what ADR-0007 lets a user see
    again -- enough to recognise which of their keys is installed, useless to anyone
    else -- and a key that cannot be re-read can only be replaced, which is how every
    serious secret manager behaves.
    """

    config_id: uuid.UUID
    last4: str
    set_at: dt.datetime


class ProviderCredentialService:
    """Write-only side of the household credential: encrypt, persist, never return.

    Separated from :class:`ProviderService` because the two have opposite postures.
    The read side must never produce a key; this one is the single place in the
    application that accepts one, and it holds the plaintext for the length of one
    method call. Storing a key over an existing one *is* the rotation procedure of
    ADR-0007: an idempotent write, the previous value overwritten rather than
    versioned.
    """

    def __init__(self, session: AsyncSession, cipher: CredentialCipher) -> None:
        self._session = session
        self._cipher = cipher

    async def store_api_key(
        self, household_id: uuid.UUID, config_id: uuid.UUID, api_key: str
    ) -> StoredKey:
        """Encrypt ``api_key`` for this configuration and persist the secret triplet.

        Every rejection below is phrased from our own fields: the submitted value is
        never quoted, not even to say it is too short, because a validation message
        is exactly the kind of string that ends up in a client log.
        """
        # Pasted keys arrive with trailing whitespace far more often than not, and a
        # stray newline turns a valid key into an authentication failure nobody can
        # see in a database dump.
        key = api_key.strip()
        if len(key) < MIN_API_KEY_LENGTH:
            raise ProviderNotConfigured(
                f"an API key of at least {MIN_API_KEY_LENGTH} characters is required"
            )

        row = await self._session.scalar(
            select(LlmProviderConfig).where(
                LlmProviderConfig.household_id == household_id,
                LlmProviderConfig.id == config_id,
                LlmProviderConfig.archived_at.is_(None),
            )
        )
        if row is None:
            # Same answer whether it does not exist or belongs to someone else:
            # distinguishing them would turn this into a configuration oracle.
            raise ProviderNotConfigured(
                "no active provider configuration with this identifier belongs to this household"
            )
        if row.mode is not LlmProviderMode.BYOK:
            # The database enforces this too (`ck_llm_provider_config_mode_requirements`);
            # refusing here turns a constraint violation into a sentence.
            raise ProviderNotConfigured(
                f"mode {row.mode.value!r} does not store a key; only 'byok' does"
            )

        stored = seal_into(row, key, cipher=self._cipher)
        await self._session.flush()

        logger.info(
            "household_api_key_stored",
            extra={"config_id": str(config_id), "provider": row.provider_code},
        )
        return stored


def seal_into(row: LlmProviderConfig, api_key: str, *, cipher: CredentialCipher) -> StoredKey:
    """Encrypt ``api_key`` onto ``row``'s secret triplet, and report only the last four.

    A module-level function rather than a method so that the two callers -- storing a
    key on an existing configuration (:meth:`ProviderCredentialService.store_api_key`)
    and creating one that is born with a key (:meth:`ProviderConfigService.create`) --
    run the *same* code. They cannot: a ``byok`` row and its ciphertext have to be
    written in one statement, because ``ck_llm_provider_config_mode_requirements``
    refuses a ``byok`` row with no ciphertext and the AAD binds the ciphertext to a
    row identifier that must therefore already be chosen. Two encryption sites would
    be two places to forget to reset ``status``, or to leave ``last_error`` claiming
    something about a key that no longer exists.

    ``api_key`` is expected to be stripped and length-checked already; the caller
    owns the message, because only the caller knows whether it is answering "store
    this key" or "create this configuration".
    """
    sealed = cipher.encrypt(api_key, household_id=row.household_id, config_id=row.id)
    row.api_key_ciphertext = sealed.ciphertext
    row.api_key_last4 = sealed.last4
    row.api_key_encryption_key_id = sealed.key_id
    set_at = dt.datetime.now(dt.UTC)
    row.api_key_set_at = set_at
    # A new key invalidates every earlier verdict about the old one, including a
    # stale `invalid_credentials` banner the household has already fixed.
    row.status = LlmConfigStatus.UNVERIFIED
    row.last_error = None
    return StoredKey(config_id=row.id, last4=sealed.last4, set_at=set_at)


# --------------------------------------------------------------------------- #
# Creating, altering, archiving and consenting a configuration
# --------------------------------------------------------------------------- #
#
# Until this existed, nothing in `api/` created an `LlmProviderConfig`: the
# flagship feature of the product could only be switched on with hand-written SQL,
# and since revision `0016` that SQL also had to date a consent in the household's
# name, which is not a thing an operator may do on somebody's behalf. Penetration
# test finding O-02.
#
# The shape follows `services/export_targets.py`, which solves the same problem one
# table over -- a third party's credential, stored with a recorded agreement that
# can be withdrawn -- and the resemblance is deliberate: two consent flows that
# behave differently is one of them being wrong.

#: The models each hosted provider's adapter can actually describe, read from the
#: adapters themselves rather than restated here. A configuration naming anything
#: else is refused at creation instead of becoming the ``unknown_model`` banner --
#: and the same lists are what the configuration screen offers, so the interface
#: cannot propose a model the instance would then decline.
KNOWN_MODELS: Final[dict[str, tuple[str, ...]]] = {
    "anthropic": tuple(sorted(ANTHROPIC_MODELS)),
    "openai": tuple(sorted(OPENAI_MODELS)),
    "gemini": tuple(sorted(GEMINI_MODELS)),
    "mistral": tuple(sorted(MISTRAL_MODELS)),
}

#: What each hosted provider's model is *known* to do, from the adapter that would
#: make the call. ``docs/data-model.md`` §9.3 calls the three columns on the
#: configuration row "effective capabilities, seeded from the provider defaults then
#: corrected by a connection probe" -- this is the seeding half, and without it a
#: freshly created ``byok`` row claims a model with no vision, no structured output
#: and no context window at all, which is what the configuration screen would then
#: display.
_STATIC_CAPABILITIES: Final[dict[str, Callable[[str], ProviderCapabilities]]] = {
    "anthropic": anthropic_capabilities,
    "openai": openai_capabilities,
    "gemini": gemini_capabilities,
    "mistral": mistral_capabilities,
}

#: Longest label the column accepts (``varchar(80)``), restated so the refusal is a
#: sentence rather than a ``DataError``.
MAX_LABEL_LENGTH: Final = 80

#: The modes that reach a third party whatever else is configured, and therefore
#: always need an agreement. ``ollama`` is absent because the answer for it depends
#: on the endpoint, not on the mode (:func:`endpoint_is_local`).
_ALWAYS_TRANSMITS: Final = frozenset({LlmProviderMode.BYOK, LlmProviderMode.INSTANCE_OWNER})


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    """One provider this instance offers, and what configuring it takes.

    Assembled from the ``llm_provider`` reference table *and* from the adapter's own
    model table, because neither is sufficient alone: the table says which providers
    the operator has enabled and which of them need a key or a URL, and the adapters
    say which models this build can describe. A screen built from the table alone
    would offer a model the capability lookup then refuses.
    """

    code: str
    display_name: str
    requires_api_key: bool
    requires_base_url: bool
    default_model: str | None
    #: Empty for ``ollama``, where the model is whatever the household pulled and
    #: only the probe can say what it does.
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderConfigView:
    """One configuration, in the only shape that may leave this layer.

    There is no ciphertext field and no key field, and that is structural rather
    than a convention: this type is what the router serialises, so a handler cannot
    disclose a key by forgetting to strip one. :attr:`api_key_last4` is the whole of
    what ADR-0007 lets a household see again.
    """

    id: uuid.UUID
    label: str
    mode: LlmProviderMode
    provider_code: str
    model: str
    base_url: str | None
    api_key_last4: str | None
    api_key_set_at: dt.datetime | None
    status: LlmConfigStatus
    supports_vision: bool
    supports_structured_output: bool
    max_context_tokens: int | None
    last_verified_at: dt.datetime | None
    last_error: str | None
    consented_at: dt.datetime | None
    consent_revoked_at: dt.datetime | None
    #: Whether this configuration needs an agreement at all -- ``False`` only for an
    #: Ollama the request would reach without leaving a local network. Computed here
    #: rather than derived by the client from ``mode``, because deriving it from the
    #: enum is exactly the mistake this work exists to correct.
    consent_required: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    archived_at: dt.datetime | None

    @property
    def is_consented(self) -> bool:
        return self.consented_at is not None and self.consent_revoked_at is None

    @property
    def is_permitted(self) -> bool:
        """Whether the consent gate would let this configuration be used right now."""
        return not self.consent_required or self.is_consented


@dataclass(frozen=True, slots=True)
class CreateProviderConfig:
    """What creating a configuration takes. ``consent_granted`` is not optional.

    It is a separate field rather than something inferred from the presence of a
    key, because the two are separate acts: pasting a credential says "I have an
    account there", and consenting says "send what my household eats, and who eats
    it, to this company". A build that inferred one from the other would be
    pre-ticking the box, which art. 4(11) does not accept as consent.
    """

    label: str
    mode: LlmProviderMode
    provider_code: str
    model: str
    consent_granted: bool
    #: Who is granting it. Recorded on the row, for the reason revision ``0014``
    #: recorded it for export destinations: a consent nobody signed is a consent
    #: nobody can be asked about.
    created_by_user_id: uuid.UUID | None = None
    base_url: str | None = None
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateProviderConfig:
    """A partial edit. ``None`` means "leave this alone", never "set it to null".

    Nothing here is legitimately settable back to null: an ``ollama`` row needs its
    ``base_url`` and no other mode may carry one, so "clear the URL" is not an
    operation this API has. The mode, the provider and the consent are absent on
    purpose -- changing the first two turns a configuration into a different one and
    is a create plus an archive, and the third has its own two routes because
    granting and withdrawing an agreement must never be a side effect of editing a
    label.
    """

    label: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    #: ``True`` disables the configuration without archiving it, ``False`` puts it
    #: back to ``unverified`` so the next call re-establishes the verdict.
    disabled: bool | None = None


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class ProviderConfigError(Exception):
    """Base of the refusals this service produces. Never carries a key.

    Every message below is built from our own fields and constants. The submitted
    value is never interpolated, not even to say it is too short, because a
    validation message is exactly the kind of string that ends up in a client log --
    and the router passes ``str(error)`` straight through as the problem detail.
    """


class UnknownProvider(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """A provider code this instance has not enabled."""


class InvalidProviderConfig(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """The mode, the provider, the model, the URL and the key do not agree."""


class ProviderConsentRequired(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """Storing this configuration would authorise a transfer nobody agreed to."""


class ProviderConsentNotRecorded(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """There is no agreement here to withdraw."""


class DuplicateProviderLabel(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """The household already has an active configuration under this name."""


class ProviderConfigAlreadyExists(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """The household already has a live configuration, and one is the maximum."""


#: Said once, raised from two places -- the pre-check and the constraint violation
#: behind it -- because a household that loses a race must read the same sentence as
#: one that does not. English like every other :class:`ProviderConfigError` message,
#: which are diagnostics; the French the household actually reads is in
#: ``frontend/src/features/providers/ProviderConfigPanel.tsx``, next to the button
#: it belongs to.
_ALREADY_CONFIGURED: Final = (
    "this household already has a provider configuration, and one is the maximum; "
    "archive it before registering another, or edit it in place"
)


class ProviderConfigNotFound(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """No active configuration with this identifier belongs to this household."""


class ProviderProbeFailed(ProviderConfigError):  # noqa: N818 -- mirrors the domain
    """The Ollama endpoint could not be interrogated, so nobody knows what it does."""


# --------------------------------------------------------------------------- #
# The probe seam
# --------------------------------------------------------------------------- #

#: Ask an Ollama endpoint what the named model can do. A narrow callable rather
#: than a port object because there is exactly one operation, and because the thing
#: a test needs to substitute is the *answer*, not an object graph.
type OllamaProber = Callable[[str, str], Awaitable[ProviderCapabilities]]


def ollama_prober(settings: Settings, *, resolver: Resolver = system_resolver) -> OllamaProber:
    """The production probe: allowlist, DNS pin, bounded timeout, bounded body.

    Built through :func:`build_pinned_client` rather than by hand, so the household
    -supplied URL passes every guard in ``infra/llm/http.py`` before a socket is
    opened -- the allowlist, the scheme check, the refusal of embedded credentials,
    and the resolution pin that makes rebinding between the two calls of the probe
    (``/api/version`` then ``/api/show``) fail rather than succeed against a second
    host.
    """
    llm_settings = llm_settings_for(settings, OLLAMA_PROVIDER_CODE)

    async def probe(base_url: str, model: str) -> ProviderCapabilities:
        client = await build_pinned_client(base_url, llm_settings, resolver=resolver)
        return await probe_capabilities(client, model)

    return probe


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class ProviderConfigService:
    """Creates, alters, archives and consents a household's provider configurations.

    Separated from :class:`ProviderService` for the reason the read and write sides
    of the credential are separated: the read side must never be able to produce a
    key, and this is one of exactly two places in the application that accept one.

    It holds no plaintext beyond the length of one method call, and it returns
    :class:`ProviderConfigView`, which has nowhere to put one.
    """

    def __init__(
        self,
        session: AsyncSession,
        cipher: CredentialCipher,
        *,
        probe: OllamaProber,
        instance_owner_household_id: uuid.UUID | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._probe = probe
        #: Which household may spend the operator's credit (ADR-0007). Injected
        #: rather than read from ``Settings`` here so the refusal is testable without
        #: an environment, and defaulting to ``None`` keeps the door closed: the
        #: failure mode of getting this wrong is a stranger's invoice.
        self._instance_owner = instance_owner_household_id
        self._resolver = resolver

    # -- reads ------------------------------------------------------------- #

    async def choices(self) -> list[ProviderChoice]:
        """The providers this instance offers, in the reference table's own order."""
        rows = (
            await self._session.scalars(
                select(LlmProvider)
                .where(LlmProvider.is_enabled.is_(True))
                .order_by(LlmProvider.sort_order, LlmProvider.code)
            )
        ).all()
        return [
            ProviderChoice(
                code=row.code,
                display_name=row.display_name,
                requires_api_key=row.requires_api_key,
                requires_base_url=row.requires_base_url,
                default_model=row.default_model,
                models=KNOWN_MODELS.get(row.code, ()),
            )
            for row in rows
        ]

    async def list_configs(self, household_id: uuid.UUID) -> list[ProviderConfigView]:
        """The household's live configuration, in a list of at most one.

        A list and not an object, because that is the shape the route has always
        published and an empty array is the honest answer for a household that has
        configured nothing. No ordering: the unique index makes plurality
        impossible, so there is nothing to order.

        ``api_key_ciphertext`` is a deferred column and is deliberately *not*
        undeferred: listing configurations has no reason to load a secret, and the
        one query that does is :meth:`ProviderService._sealed_credential`, on the
        path that is about to spend it.
        """
        rows = (
            await self._session.scalars(
                select(LlmProviderConfig).where(
                    LlmProviderConfig.household_id == household_id,
                    LlmProviderConfig.archived_at.is_(None),
                )
            )
        ).all()
        return [await self._view(row) for row in rows]

    # -- writes ------------------------------------------------------------ #

    async def create(
        self, household_id: uuid.UUID, command: CreateProviderConfig
    ) -> ProviderConfigView:
        """Register **the** configuration, with the agreement that lets it be used.

        Everything is validated before anything is written, and the ``ollama`` probe
        runs before the flush: ADR-0007 is explicit that a configuration whose
        abilities nobody established must not be stored, because the interface would
        then promise a feature on a guess. An unreachable endpoint fails the save.

        **A household that already has a live one is refused, not served.** Since
        revision ``0025`` a household has one configuration or none. Replacing the
        existing row here -- or archiving it on the household's behalf -- would
        destroy a working setup because somebody pressed "create", and the API key
        behind it is not ours to retire. So the answer is
        :class:`ProviderConfigAlreadyExists`, and the household chooses: archive the
        old configuration, or edit it in place.

        The refusal is raised **before** the probe, so a creation that cannot succeed
        does not first dial somebody's Ollama endpoint.
        """
        # The label is checked first, deliberately. Both refusals are true of a
        # household that re-submits the name it already uses, and the more specific
        # one is the more useful sentence -- it also keeps
        # `uq_llm_provider_config_label` a rule something still exercises rather
        # than an index no request can reach.
        label = await self._checked_label(household_id, command.label)
        await self._refuse_a_second_configuration(household_id)
        await self._require_enabled(command.provider_code)
        base_url = _blank_to_none(command.base_url)
        api_key = None if command.api_key is None else command.api_key.strip()
        self._check_coherence(
            household_id,
            mode=command.mode,
            provider_code=command.provider_code,
            model=command.model,
            base_url=base_url,
            api_key=api_key,
        )
        await self._require_consent(command.mode, base_url, granted=command.consent_granted)

        # Drawn here rather than left to the column default: it is half of the AAD,
        # so the ciphertext cannot be built until the row it is bound to has a name.
        row = LlmProviderConfig(
            id=uuid.uuid7(),
            household_id=household_id,
            label=label,
            mode=command.mode,
            provider_code=command.provider_code,
            model=command.model.strip(),
            base_url=base_url,
            status=LlmConfigStatus.UNVERIFIED,
            created_by_user_id=command.created_by_user_id,
            consented_at=dt.datetime.now(dt.UTC) if command.consent_granted else None,
        )
        if api_key is not None:
            seal_into(row, api_key, cipher=self._cipher)
        if command.mode is LlmProviderMode.OLLAMA:
            await self._apply_probe(row)
        else:
            _seed_static_capabilities(row)
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError:
            # Two owners pressing "create" at the same instant: the pre-check above
            # saw nothing and both got through it. `uq_llm_provider_config_household
            # _active` is the arbiter, and this turns its violation into the same
            # answer the pre-check gives rather than a 500 -- the shape
            # `services/receipts.py` uses for its duplicate-upload constraint.
            raise ProviderConfigAlreadyExists(_ALREADY_CONFIGURED) from None
        await self._session.refresh(row)

        # No label and no URL: the household is already on every log line through
        # `household_id_var`, and neither field is ours to republish.
        logger.info(
            "provider_config_created",
            extra={
                "config_id": str(row.id),
                "provider": row.provider_code,
                "mode": row.mode.value,
                "consented": command.consent_granted,
            },
        )
        return await self._view(row)

    async def update(
        self, household_id: uuid.UUID, config_id: uuid.UUID, command: UpdateProviderConfig
    ) -> ProviderConfigView:
        """Change what can be changed about a configuration, and re-probe if needed.

        Replacing the key *is* the rotation procedure of ADR-0007: an idempotent
        write, the previous value overwritten rather than versioned, routed through
        :meth:`ProviderCredentialService.store_api_key`'s own sealing so a rotation
        and a first write cannot behave differently.
        """
        row = await self._active(household_id, config_id)
        if command.label is not None:
            row.label = await self._checked_label(household_id, command.label, excluding=row.id)
        if command.model is not None:
            model = command.model.strip()
            self._check_model(row.mode, row.provider_code, model)
            row.model = model
        if command.base_url is not None:
            if row.mode is not LlmProviderMode.OLLAMA:
                raise InvalidProviderConfig(
                    f"mode {row.mode.value!r} has no base URL to change; only 'ollama' does"
                )
            row.base_url = _blank_to_none(command.base_url)
            if row.base_url is None:
                raise InvalidProviderConfig("mode 'ollama' requires a base URL")
        if command.api_key is not None:
            if row.mode is not LlmProviderMode.BYOK:
                raise InvalidProviderConfig(
                    f"mode {row.mode.value!r} does not store a key; only 'byok' does"
                )
            key = command.api_key.strip()
            if len(key) < MIN_API_KEY_LENGTH:
                raise InvalidProviderConfig(
                    f"an API key of at least {MIN_API_KEY_LENGTH} characters is required"
                )
            seal_into(row, key, cipher=self._cipher)
        if command.disabled is not None:
            # `disabled` is a status and not a fourth date column, so switching it
            # off has to put the row back into a state the next call re-establishes
            # rather than into the verdict it held before it was disabled.
            row.status = (
                LlmConfigStatus.DISABLED if command.disabled else LlmConfigStatus.UNVERIFIED
            )
            if not command.disabled:
                row.last_error = None

        # An endpoint or a model that moved makes every stored capability a claim
        # about something else. Re-probing here is what keeps `supports_vision` a
        # fact rather than a memory -- and it fails the edit, exactly as it fails a
        # creation, when the endpoint cannot answer.
        if row.mode is LlmProviderMode.OLLAMA and (
            command.base_url is not None or command.model is not None
        ):
            await self._apply_probe(row)
        elif command.model is not None:
            _seed_static_capabilities(row)
        # A withdrawn agreement is not silently restored by an edit: re-granting is
        # its own route, and it is the one that puts the sentence about what leaves
        # the instance back in front of somebody.
        await self._session.flush()
        await self._session.refresh(row)
        logger.info(
            "provider_config_updated",
            extra={
                "config_id": str(row.id),
                "provider": row.provider_code,
                "key_replaced": command.api_key is not None,
            },
        )
        return await self._view(row)

    async def archive(self, household_id: uuid.UUID, config_id: uuid.UUID) -> ProviderConfigView:
        """Retire a configuration. **The row survives, and so does its history.**

        Not a deletion, for the same reason a withdrawn export destination is not
        deleted: the household keeps the record of what it once authorised, to whom
        and when it stopped, and that record is what an art. 15 request asks for.
        ``DELETE /v1/households`` is where erasure lives.

        **It is also how a household changes provider.** One live configuration is
        the rule (``uq_llm_provider_config_household_active``), so archiving is the
        gesture that frees the slot: the index excludes archived rows, and the
        creation that was refused a moment ago succeeds immediately afterwards.

        **The stored key is left sealed rather than blanked**, and that is a
        constraint rather than a preference: ``ck_..._mode_requirements`` requires a
        ``byok`` row to carry a ciphertext and ``ck_..._secret_triplet`` requires the
        three secret columns to move together, so emptying them here would need the
        mode rewritten -- which would falsify the record this method exists to keep.
        Nothing can reach the key afterwards: every read path filters on
        ``archived_at IS NULL``, and there is no route back out of the archive. A
        household that wants the credential gone at the provider rotates it there,
        which is the only place that actually revokes it.
        """
        row = await self._active(household_id, config_id)
        row.archived_at = dt.datetime.now(dt.UTC)
        row.status = LlmConfigStatus.DISABLED
        await self._session.flush()
        await self._session.refresh(row)
        logger.info(
            "provider_config_archived",
            extra={"config_id": str(row.id), "provider": row.provider_code},
        )
        return await self._view(row)

    async def grant_consent(
        self, household_id: uuid.UUID, config_id: uuid.UUID
    ) -> ProviderConfigView:
        """Record that this household agrees to its data reaching this provider.

        Idempotent, and idempotent in the direction that keeps the record honest: a
        second grant on a live agreement leaves the original date alone. Moving it
        forward would quietly rewrite when the household actually agreed, which is
        the one fact art. 7(1) asks the controller to be able to demonstrate.

        Re-granting after a withdrawal *does* move the date, because that is a new
        agreement -- and it clears the withdrawal, since a row carrying both would
        violate ``ck_llm_provider_config_revocation_follows_consent`` and, worse,
        would show two contradictory dates on the same screen.
        """
        row = await self._active(household_id, config_id)
        if row.consented_at is None or row.consent_revoked_at is not None:
            row.consented_at = dt.datetime.now(dt.UTC)
            row.consent_revoked_at = None
            await self._session.flush()
            await self._session.refresh(row)
            logger.info(
                "provider_consent_granted",
                extra={"config_id": str(row.id), "provider": row.provider_code},
            )
        return await self._view(row)

    async def withdraw_consent(
        self, household_id: uuid.UUID, config_id: uuid.UUID
    ) -> ProviderConfigView:
        """Withdraw the agreement, keeping the row and its history (art. 7(3)).

        It takes effect at the **next request**: the gate re-reads these two columns
        on every provider load, so nothing further is sent from the moment this
        returns. Nothing is deleted -- the household keeps the record of what it
        authorised and when it stopped, and so does whoever has to answer for it.

        Idempotent: withdrawing twice keeps the first date. The first refusal is the
        one that matters, and overwriting it would rewrite when the household
        stopped consenting.
        """
        row = await self._active(household_id, config_id)
        if row.consented_at is None:
            raise ProviderConsentNotRecorded(
                "this configuration carries no agreement to withdraw; it either never "
                "needed one, or none was ever given"
            )
        if row.consent_revoked_at is None:
            row.consent_revoked_at = dt.datetime.now(dt.UTC)
            await self._session.flush()
            await self._session.refresh(row)
            logger.info(
                "provider_consent_withdrawn",
                extra={"config_id": str(row.id), "provider": row.provider_code},
            )
        return await self._view(row)

    async def reprobe(self, household_id: uuid.UUID, config_id: uuid.UUID) -> ProviderConfigView:
        """Ask the household's Ollama again what its model can do.

        Only ``ollama`` has anything to ask: the four hosted providers describe their
        models from a table in this build, and there is no request that would tell us
        more without also being billed. Calling this on one is a refusal rather than
        a no-op, because a screen that reports "capabilities refreshed" after doing
        nothing is a screen that lies.
        """
        row = await self._active(household_id, config_id)
        if row.mode is not LlmProviderMode.OLLAMA:
            raise InvalidProviderConfig(
                f"mode {row.mode.value!r} has no endpoint to interrogate; its capabilities "
                "come from this instance's model table, not from a probe"
            )
        await self._apply_probe(row)
        await self._session.flush()
        await self._session.refresh(row)
        return await self._view(row)

    # -- internals --------------------------------------------------------- #

    async def _active(self, household_id: uuid.UUID, config_id: uuid.UUID) -> LlmProviderConfig:
        row = await self._session.scalar(
            select(LlmProviderConfig).where(
                LlmProviderConfig.household_id == household_id,
                LlmProviderConfig.id == config_id,
                LlmProviderConfig.archived_at.is_(None),
            )
        )
        if row is None:
            # Same answer whether it does not exist or belongs to someone else:
            # distinguishing them would turn this into a configuration oracle.
            raise ProviderConfigNotFound(
                "no active provider configuration with this identifier belongs to this household"
            )
        return row

    async def _checked_label(
        self, household_id: uuid.UUID, label: str, *, excluding: uuid.UUID | None = None
    ) -> str:
        cleaned = label.strip()
        if not cleaned or len(cleaned) > MAX_LABEL_LENGTH:
            raise InvalidProviderConfig(
                f"a label of 1 to {MAX_LABEL_LENGTH} characters is required"
            )
        query = select(LlmProviderConfig.id).where(
            LlmProviderConfig.household_id == household_id,
            func.lower(LlmProviderConfig.label) == cleaned.lower(),
            LlmProviderConfig.archived_at.is_(None),
        )
        if excluding is not None:
            query = query.where(LlmProviderConfig.id != excluding)
        if await self._session.scalar(query) is not None:
            # Pre-checked rather than left to `uq_llm_provider_config_label`, so the
            # household gets a sentence instead of an IntegrityError -- the unique
            # index is still what makes it true under a concurrent second request.
            raise DuplicateProviderLabel(
                "this household already has an active configuration under that name"
            )
        return cleaned

    async def _refuse_a_second_configuration(self, household_id: uuid.UUID) -> None:
        """One household, one live configuration -- checked before anything is built.

        Pre-checked rather than left to ``uq_llm_provider_config_household_active``
        so the household gets a sentence and a remedy instead of an
        ``IntegrityError``, exactly as :meth:`_checked_label` does for the label. The
        unique index remains what makes the rule *true*: two concurrent creations
        both pass this read, and only one survives the flush.
        """
        existing = await self._session.scalar(
            select(LlmProviderConfig.id).where(
                LlmProviderConfig.household_id == household_id,
                LlmProviderConfig.archived_at.is_(None),
            )
        )
        if existing is not None:
            raise ProviderConfigAlreadyExists(_ALREADY_CONFIGURED)

    async def _require_enabled(self, provider_code: str) -> None:
        row = await self._session.scalar(
            select(LlmProvider).where(
                LlmProvider.code == provider_code, LlmProvider.is_enabled.is_(True)
            )
        )
        if row is None:
            offered = await self.choices()
            raise UnknownProvider(
                f"{provider_code!r} is not a provider this instance offers; "
                f"available: {', '.join(choice.code for choice in offered)}"
            )

    def _check_coherence(
        self,
        household_id: uuid.UUID,
        *,
        mode: LlmProviderMode,
        provider_code: str,
        model: str,
        base_url: str | None,
        api_key: str | None,
    ) -> None:
        """The mode decides what the other four fields may be. The database agrees.

        ``ck_llm_provider_config_mode_requirements`` enforces the key and URL halves
        of this on the row itself; refusing here turns a constraint violation into a
        sentence, and adds the two rules the database cannot see -- which providers a
        household may bring its own key for, and who may spend the operator's.
        """
        match mode:
            case LlmProviderMode.OLLAMA:
                if provider_code != OLLAMA_PROVIDER_CODE:
                    raise InvalidProviderConfig(
                        f"mode 'ollama' serves the {OLLAMA_PROVIDER_CODE!r} provider only, "
                        f"not {provider_code!r}"
                    )
                if base_url is None:
                    raise InvalidProviderConfig("mode 'ollama' requires a base URL")
                if api_key is not None:
                    raise InvalidProviderConfig("mode 'ollama' stores no API key")
            case LlmProviderMode.BYOK:
                if provider_code not in BYOK_PROVIDERS:
                    allowed = ", ".join(sorted(BYOK_PROVIDERS))
                    raise InvalidProviderConfig(
                        f"provider {provider_code!r} cannot be used with a household key; "
                        f"supported: {allowed}"
                    )
                if base_url is not None:
                    raise InvalidProviderConfig(
                        "mode 'byok' calls the provider's own endpoint and takes no base URL"
                    )
                if api_key is None:
                    raise InvalidProviderConfig("mode 'byok' requires the household's API key")
                if len(api_key) < MIN_API_KEY_LENGTH:
                    raise InvalidProviderConfig(
                        f"an API key of at least {MIN_API_KEY_LENGTH} characters is required"
                    )
            case LlmProviderMode.INSTANCE_OWNER:
                if provider_code not in BYOK_PROVIDERS:
                    allowed = ", ".join(sorted(BYOK_PROVIDERS))
                    raise InvalidProviderConfig(
                        f"provider {provider_code!r} has no instance key; supported: {allowed}"
                    )
                if api_key is not None or base_url is not None:
                    raise InvalidProviderConfig(
                        "mode 'instance_owner' spends the operator's key and takes neither "
                        "an API key nor a base URL"
                    )
                if self._instance_owner is None or self._instance_owner != household_id:
                    # The factory refuses this at call time too. Refusing at creation
                    # keeps a household from storing a configuration that can only
                    # ever produce a 409, and keeps the operator's invoice closed by
                    # default rather than by the second check remembering.
                    raise InvalidProviderConfig(
                        "mode 'instance_owner' is reserved for the household that owns this "
                        "instance; bring your own API key, or run a local Ollama"
                    )
        self._check_model(mode, provider_code, model)

    @staticmethod
    def _check_model(mode: LlmProviderMode, provider_code: str, model: str) -> None:
        """Refuse a model the capability lookup would not recognise.

        Not applied to ``ollama``: the model there is whatever the household pulled,
        no table can enumerate it, and the probe is what establishes what it does.
        """
        cleaned = model.strip()
        if not cleaned:
            raise InvalidProviderConfig("a model name is required")
        if mode is LlmProviderMode.OLLAMA:
            return
        known = KNOWN_MODELS.get(provider_code, ())
        if cleaned not in known:
            raise InvalidProviderConfig(
                f"this instance cannot describe a model named that for {provider_code!r}; "
                f"it knows: {', '.join(known)}"
            )

    async def _require_consent(
        self, mode: LlmProviderMode, base_url: str | None, *, granted: bool
    ) -> None:
        """Refuse to store a configuration that would transmit without an agreement.

        The gate in :meth:`ProviderService._consent_refusal` already fails such a row
        closed at every read. Refusing it *here* is what migration ``0021`` makes
        structural for the two modes that always transmit -- and what keeps the third
        case, a hosted Ollama, from being stored as a configuration that can never
        work while its screen shows nothing wrong with it.
        """
        if granted:
            return
        if mode in _ALWAYS_TRANSMITS:
            raise ProviderConsentRequired(
                f"mode {mode.value!r} sends this household's data to a third party and "
                "cannot be stored without an explicit agreement to it"
            )
        if not await endpoint_is_local(base_url, self._resolver):
            raise ProviderConsentRequired(
                "this Ollama endpoint is not on a network this household controls, so "
                "using it sends personal data to a third party; it cannot be stored "
                "without an explicit agreement to that"
            )

    async def _apply_probe(self, row: LlmProviderConfig) -> None:
        """Interrogate the endpoint and write what it answered onto the row.

        The three capability columns and ``last_verified_at`` are set together and
        from one answer, because a row holding a fresh context window beside a stale
        vision flag is a row the banner would read as fact.
        """
        if not row.base_url:  # pragma: no cover - `_check_coherence` refused it first
            raise InvalidProviderConfig("mode 'ollama' requires a base URL")
        try:
            capabilities = await self._probe(row.base_url, row.model)
        except LlmError as error:
            # `str(error)` is safe: `ollama` carries no credential, and every message
            # in `infra/llm/` is built from our own constants and from the
            # household's own URL. The chain is dropped so no transport traceback
            # travels one frame from the handler.
            raise ProviderProbeFailed(str(error)) from None
        row.supports_vision = capabilities.supports_vision
        row.supports_structured_output = capabilities.supports_structured_output
        row.max_context_tokens = capabilities.context_window
        row.last_verified_at = capabilities.probed_at or dt.datetime.now(dt.UTC)
        row.status = LlmConfigStatus.VERIFIED
        row.last_error = None

    async def _view(self, row: LlmProviderConfig) -> ProviderConfigView:
        return ProviderConfigView(
            id=row.id,
            label=row.label,
            mode=row.mode,
            provider_code=row.provider_code,
            model=row.model,
            base_url=row.base_url,
            api_key_last4=row.api_key_last4,
            api_key_set_at=row.api_key_set_at,
            status=row.status,
            supports_vision=row.supports_vision,
            supports_structured_output=row.supports_structured_output,
            max_context_tokens=row.max_context_tokens,
            last_verified_at=row.last_verified_at,
            last_error=row.last_error,
            consented_at=row.consented_at,
            consent_revoked_at=row.consent_revoked_at,
            consent_required=not (
                row.mode is LlmProviderMode.OLLAMA
                and await endpoint_is_local(row.base_url, self._resolver)
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
            archived_at=row.archived_at,
        )


def _seed_static_capabilities(row: LlmProviderConfig) -> None:
    """Copy what the adapter knows about this model onto the row's three columns.

    ``last_verified_at`` and ``status`` are deliberately left alone: nothing was
    verified, because nothing was called. This is a *declaration* copied from a
    table, which is what ``CapabilitySource.STATIC`` means, and dating it as a
    verification would put a green tick against a key nobody has tried yet.

    The lookup cannot fail here -- ``_check_model`` has already refused a model the
    tables do not carry -- so a provider absent from the map (there is none today,
    and ``ollama`` never reaches this) leaves the columns as they were rather than
    raising from a projection.
    """
    lookup = _STATIC_CAPABILITIES.get(row.provider_code)
    if lookup is None:  # pragma: no cover - the four hosted providers are exhaustive
        return
    capabilities = lookup(row.model)
    row.supports_vision = capabilities.supports_vision
    row.supports_structured_output = capabilities.supports_structured_output
    row.max_context_tokens = capabilities.context_window


def _blank_to_none(value: str | None) -> str | None:
    """An empty string from a form is an absent value, not a value that is empty."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def provider_ports_builder(
    settings: Settings,
    cipher: CredentialCipher,
    *,
    resolver: Resolver = system_resolver,
) -> ProviderPortsBuilder:
    """The production builder: a factory carrying the key that provider would spend.

    ``resolver`` is what arms the DNS-rebinding guard. Without it the factory builds
    an Ollama client with no pinned address, and
    ``GuardedHttpClient.assert_stable_resolution`` returns on its first line -- a
    control that reads as present in every review and protects nothing. It defaults
    to the real system resolver so that production is armed by omission rather than
    by remembering; the parameter exists so a test can substitute a resolver that
    rebinds, which is the only way to prove the wiring is still there.
    """

    def build(provider_code: str) -> ProviderPorts:
        return LlmProviderFactory(
            llm_settings_for(settings, provider_code), cipher=cipher, resolver=resolver
        )

    return build


def provider_config_service(
    session: AsyncSession,
    cipher: CredentialCipher,
    settings: Settings,
    *,
    resolver: Resolver = system_resolver,
) -> ProviderConfigService:
    """The production write side, with every projection of ``Settings`` made here.

    A counterpart to :func:`provider_ports_builder`, and in this module for the same
    reason: which household may spend the operator's credit, and which Ollama hosts
    the operator permits, are readings of the environment that already live beside
    each other here. A router assembling them itself would be a second place to get
    ``instance_owner_household_id`` wrong, and the failure mode of that is a
    stranger's invoice.
    """
    return ProviderConfigService(
        session,
        cipher,
        probe=ollama_prober(settings, resolver=resolver),
        instance_owner_household_id=_instance_owner(settings),
        resolver=resolver,
    )


def llm_settings_for(settings: Settings, provider_code: str) -> LlmSettings:
    """Project the application configuration onto what the provider layer reads."""
    read_key = _INSTANCE_OWNER_KEYS.get(provider_code)
    key = None if read_key is None else read_key(settings)
    return LlmSettings(
        ollama_allowed_hosts=frozenset(host.lower() for host in settings.ollama_allowed_hosts),
        instance_owner_household_id=_instance_owner(settings),
        timeout_seconds=(
            settings.ollama_timeout_seconds
            if provider_code == OLLAMA_PROVIDER_CODE
            else settings.llm_timeout_seconds
        ),
        instance_owner_api_key=None if key is None else key.get_secret_value(),
    )


def _instance_owner(settings: Settings) -> uuid.UUID | None:
    """The household allowed to spend the operator's credit, or nobody.

    A malformed value closes the door rather than opening it, and the value itself
    never reaches the log: the failure mode of getting ADR-0007 wrong is a
    stranger's invoice.
    """
    raw = settings.instance_owner_household_id
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        logger.error("instance_owner_household_id_is_not_a_uuid")
        return None


def _view(
    row: LlmProviderConfig,
    *,
    configured: bool,
    notices: tuple[DegradationNotice, ...],
    capabilities: ProviderCapabilities | None = None,
) -> ProviderView:
    return ProviderView(
        configured=configured,
        mode=row.mode.value,
        provider=row.provider_code,
        model=row.model,
        supports_vision=capabilities is not None and capabilities.supports_vision,
        supports_structured_output=(
            capabilities is not None and capabilities.supports_structured_output
        ),
        notices=notices,
    )


def _to_provider_config(
    row: LlmProviderConfig, sealed: SealedCredential | None
) -> HouseholdProviderConfig:
    return HouseholdProviderConfig(
        household_id=row.household_id,
        mode=row.mode,
        provider_code=row.provider_code,
        model=row.model,
        base_url=row.base_url,
        # Never a plaintext key from this path. The sealed credential travels to the
        # factory, which decrypts it at the moment of use and does not keep it.
        api_key=None,
        sealed_api_key=sealed,
        probed_capabilities=_probed_capabilities(row),
    )


def _probed_capabilities(row: LlmProviderConfig) -> ProviderCapabilities | None:
    """The stored Ollama probe, or ``None`` when there is nothing trustworthy.

    Absence is not a default here: a configuration whose abilities nobody
    established cannot be used to promise a feature, so the factory refuses it and
    the interface says why.
    """
    if row.mode is not LlmProviderMode.OLLAMA:
        return None
    if row.max_context_tokens is None or row.last_verified_at is None:
        return None
    return ProviderCapabilities(
        provider=row.provider_code,
        model=row.model,
        context_window=row.max_context_tokens,
        supports_structured_output=row.supports_structured_output,
        supports_vision=row.supports_vision,
        # Ollama exposes no prompt cache: the stable prefix is resent every call,
        # which `DEGRADATION_POLICY` treats as emulation with a token cost.
        supports_prompt_caching=False,
        source=CapabilitySource.PROBED,
        probed_at=_as_utc(row.last_verified_at),
    )


def _as_utc(moment: dt.datetime) -> dt.datetime:
    """Probed capabilities must carry a timezone; a naive column value gets UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
