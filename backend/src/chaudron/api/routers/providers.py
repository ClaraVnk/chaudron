"""What the household's model provider can do, and how it comes to exist at all.

Two halves, and they answer different questions.

``GET /v1/providers/capabilities`` is the **banner**: what will happen if the user
presses the button, rendered permanently rather than at the moment of failure. It
resolves the household's one configuration and reduces it to "usable, and what is
reduced".

Everything else here is the **configuration screen**: the routes that create,
list, alter and archive a household's provider configuration, and the two that
grant and withdraw the agreement that lets it be used. Until they existed,
nothing in ``api/`` created an ``LlmProviderConfig`` at all -- the headline feature
of the product could only be switched on with hand-written SQL, and since revision
``0016`` that SQL also had to date a consent in the household's name, which is not
a thing an operator may do on somebody else's behalf. Penetration test finding
O-02.

**A household has one live configuration or none**, since revision ``0025``.
``POST`` answers ``409`` to the second rather than replacing what is already
there: the row it would have overwritten carries a working API key, and retiring
that is the household's decision to take, on the archive route, deliberately.

**Writing is owner-only; reading is not.** ``POST`` accepts a third party's
credential and dates an agreement in the household's name; ``PATCH`` rotates that
credential; ``DELETE`` retires it; the two consent routes give and take back the
legal basis for sending what identifiable people eat, and who they are, to a
company in another jurisdiction. That is the same decision
``routers/export_targets.py`` is owner-only for, arriving from the same side, and
the argument is not seniority: a ``viewer`` could otherwise paste their own
Anthropic key, consent on everybody's behalf, and set the household's health data
in motion. Reading the list is left to any member, because it discloses no
credential -- ``api_key_last4`` is four characters kept precisely so a household
can recognise which of its keys is installed.

**No machine token reaches any of this.** Contract v1.1 section 10 closes the
whole ``/v1/providers`` area to bearer credentials and
``tests/api/test_route_authentication.py`` asserts it in both directions; the
owner-only routes are additionally unreachable because ``require_owner`` resolves
a browser session, which a token cannot produce.

**The key is never rendered.** No response model in this module has a field that
could hold one, which is structural rather than a convention -- a handler cannot
disclose a key by forgetting to strip one, because the service hands it a
:class:`~chaudron.services.providers.ProviderConfigView` that has nowhere to put
one. The key is never in a path, never in a query string, and never in a log line;
``tests/llm/test_no_key_leaks.py`` holds the standard and
``tests/api/test_provider_configuration.py`` drives these routes against it over
HTTP.

**Consent is per configuration, and whether it is needed depends on the
endpoint.** ``docs/security-model.md`` §8.3 requires local inference to stay fully
functional without an agreement -- but "local" is a property of ``base_url``, not
of the mode enum, and the two came apart in the penetration test: an operator who
allowlists a *hosted* Ollama produced a row that transmits to a third party with
the gate switched off by mode. ``consent_required`` on every response is the
service's answer to that question, computed rather than derived by the client, and
the routes refuse to store a configuration that would transmit without one.

The request and response models live here rather than in ``api/schemas.py``, the
way ``routers/export_targets.py``, ``routers/budget.py`` and ``routers/calendar.py``
keep their own: they are used by one router, and nothing else has a reason to
import them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from chaudron.api.deps import (
    CipherDep,
    HouseholdDep,
    OwnerDep,
    OwnerHouseholdDep,
    ProviderServiceDep,
    SessionDep,
    get_settings_dep,
)
from chaudron.api.errors import ProblemError
from chaudron.api.schemas import CapabilityFlagsOut, ProviderCapabilitiesOut
from chaudron.config import Settings
from chaudron.domain.llm_ports import DegradationNotice
from chaudron.domain.models import LlmProviderMode
from chaudron.infra.crypto import MIN_API_KEY_LENGTH
from chaudron.services.providers import (
    MAX_LABEL_LENGTH,
    CreateProviderConfig,
    DuplicateProviderLabel,
    InvalidProviderConfig,
    ProviderChoice,
    ProviderConfigAlreadyExists,
    ProviderConfigError,
    ProviderConfigNotFound,
    ProviderConfigService,
    ProviderConfigView,
    ProviderConsentNotRecorded,
    ProviderConsentRequired,
    ProviderProbeFailed,
    UnknownProvider,
    UpdateProviderConfig,
    provider_config_service,
)

router = APIRouter(prefix="/v1/providers", tags=["providers"])

#: No real provider key is anywhere near this long. The ceiling exists so that a
#: multi-megabyte "key" is refused by the schema rather than encrypted.
_MAX_API_KEY_CHARS = 512

#: ``llm_provider.code`` is ``varchar(40)``, and every code this build ships is a
#: lowercase word. Bounded and restricted here so an unknown value is refused by
#: shape before it reaches a query, and so nothing that could carry a separator
#: reaches a message.
_PROVIDER_CODE_PATTERN = r"^[a-z0-9_-]{1,40}$"

#: ``llm_provider_config.base_url`` is ``text``; every guard that matters is applied
#: by ``infra/llm/http.py`` (scheme, allowlist, no embedded credentials, DNS pin).
#: The bound here only stops a megabyte of "URL" from being stored.
_MAX_BASE_URL_CHARS = 2048


class StrictModel(BaseModel):
    """Unknown fields are rejected, not ignored -- as everywhere else at the boundary."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class ProviderChoiceOut(BaseModel):
    """One provider this instance offers, and what configuring it takes.

    The configuration screen is built from this rather than from a list compiled
    in the client: which providers an operator enabled is a property of the
    instance, and a screen offering a model this build cannot describe would
    produce a refusal the user could not have foreseen.
    """

    code: str
    display_name: str
    requires_api_key: bool
    requires_base_url: bool
    default_model: str | None
    #: Empty for ``ollama``, where the model is whatever the household pulled and
    #: only the probe can say what it does -- so the form offers a free text field
    #: there and a closed list everywhere else.
    models: list[str]


class ProviderConfigOut(BaseModel):
    """One configuration. Deliberately without a field for the key.

    ``api_key_last4`` is the only fragment of a stored key that ever leaves the
    instance: enough to recognise which of two keys is installed, useless to
    anybody else (ADR-0007, ``docs/data-model.md`` §9.2).
    """

    id: uuid.UUID
    label: str
    mode: str
    provider: str
    model: str
    #: Only ever set for ``ollama``. It is the household's own input and is shown
    #: back so it can be corrected.
    base_url: str | None
    api_key_last4: str | None
    api_key_set_at: dt.datetime | None
    status: str
    #: The *effective* capabilities: a table lookup for the hosted providers, the
    #: stored probe for Ollama (``docs/data-model.md`` §9.3).
    capabilities: CapabilityFlagsOut
    max_context_tokens: int | None
    last_verified_at: dt.datetime | None
    last_error: str | None
    consented_at: dt.datetime | None
    consent_revoked_at: dt.datetime | None
    #: Whether this configuration needs an agreement at all. ``false`` only for an
    #: Ollama the request would reach without leaving a local network -- computed
    #: by the server, because deriving it from ``mode`` is the mistake that let a
    #: hosted Ollama transmit with the gate disabled.
    consent_required: bool
    #: The one field a client should branch on to decide whether to offer the
    #: feature. Computed here rather than left to the caller to derive from two
    #: dates and a flag, because deriving it wrongly means sending personal data
    #: on an agreement that was withdrawn.
    is_permitted: bool
    is_consented: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    archived_at: dt.datetime | None


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class CreateConfigIn(StrictModel):
    """A configuration, and the agreement that is its own field.

    ``consent_granted`` is **required and has no default**, exactly as on
    ``routers/export_targets.py``. A default of ``true`` would be a pre-ticked box;
    a default of ``false`` would let a client omit the one field the whole feature
    turns on and meet a refusal it could not explain. Making it required forces the
    question to be asked, which is what an explicit agreement means (art. 4(11)).

    ``api_key`` is a :class:`~pydantic.SecretStr` so that a model dumped into a log
    line, a traceback frame or a debugger prints a mask rather than a household's
    billable credential. The validation handler in ``api/errors.py`` already strips
    pydantic's ``input`` echo from a 422; this is the second lock.
    """

    label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_LENGTH)]
    mode: LlmProviderMode
    provider: Annotated[str, Field(pattern=_PROVIDER_CODE_PATTERN)]
    model: Annotated[str, Field(min_length=1, max_length=120)]
    consent_granted: bool
    base_url: Annotated[str | None, Field(max_length=_MAX_BASE_URL_CHARS)] = None
    api_key: (
        Annotated[SecretStr, Field(min_length=MIN_API_KEY_LENGTH, max_length=_MAX_API_KEY_CHARS)]
        | None
    ) = None


class UpdateConfigIn(StrictModel):
    """A partial edit. An absent field is left alone; there is no "set to null".

    Neither the mode nor the provider is editable: changing either turns a
    configuration into a different one, and doing that in place would silently
    reinterpret an agreement given about the first. Archive and create instead.

    The consent is not editable here either. It has two routes of its own, so that
    granting or withdrawing an agreement can never be a side effect of renaming a
    configuration.
    """

    label: Annotated[str | None, Field(min_length=1, max_length=MAX_LABEL_LENGTH)] = None
    model: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    base_url: Annotated[str | None, Field(max_length=_MAX_BASE_URL_CHARS)] = None
    api_key: (
        Annotated[SecretStr, Field(min_length=MIN_API_KEY_LENGTH, max_length=_MAX_API_KEY_CHARS)]
        | None
    ) = None
    #: ``true`` stops this configuration being used without retiring it -- the
    #: switch a household reaches for while it sorts out a billing problem.
    disabled: bool | None = None


class ConsentGrantIn(StrictModel):
    """The agreement, as an explicit act rather than as an empty ``POST``.

    ``granted`` is required and must be ``true``. A body-less grant route would
    make "the household agreed" the consequence of a URL being hit, which is not
    something anyone could later demonstrate under art. 7(1). ``false`` is refused
    rather than quietly treated as a withdrawal: the two are separate operations
    with separate audit trails, and ``DELETE`` is the one that withdraws.
    """

    granted: bool


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def get_provider_config_service(
    session: SessionDep,
    cipher: CipherDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ProviderConfigService:
    """The write side, assembled where the other provider wiring already lives.

    Declared here rather than in ``api/deps.py`` for the reason
    ``routers/export_targets.py`` declares its own: it is used by one router, and
    the projection of ``Settings`` it needs -- which household may spend the
    operator's credit, which Ollama endpoints this instance permits -- is done once,
    in ``services/providers.py``, beside the reading of the same variables that the
    call path uses.
    """
    return provider_config_service(session, cipher, settings)


ProviderConfigServiceDep = Annotated[ProviderConfigService, Depends(get_provider_config_service)]


def _reason(notice: DegradationNotice) -> str:
    """One readable French sentence per notice: what is reduced, and what to do."""
    return notice.reason if notice.remedy is None else f"{notice.reason} {notice.remedy}"


def _to_out(view: ProviderConfigView) -> ProviderConfigOut:
    return ProviderConfigOut(
        id=view.id,
        label=view.label,
        mode=view.mode.value,
        provider=view.provider_code,
        model=view.model,
        base_url=view.base_url,
        api_key_last4=view.api_key_last4,
        api_key_set_at=view.api_key_set_at,
        status=view.status.value,
        capabilities=CapabilityFlagsOut(
            vision=view.supports_vision,
            structured_output=view.supports_structured_output,
        ),
        max_context_tokens=view.max_context_tokens,
        last_verified_at=view.last_verified_at,
        last_error=view.last_error,
        consented_at=view.consented_at,
        consent_revoked_at=view.consent_revoked_at,
        consent_required=view.consent_required,
        is_permitted=view.is_permitted,
        is_consented=view.is_consented,
        created_at=view.created_at,
        updated_at=view.updated_at,
        archived_at=view.archived_at,
    )


def _choice_out(choice: ProviderChoice) -> ProviderChoiceOut:
    return ProviderChoiceOut(
        code=choice.code,
        display_name=choice.display_name,
        requires_api_key=choice.requires_api_key,
        requires_base_url=choice.requires_base_url,
        default_model=choice.default_model,
        models=list(choice.models),
    )


def _problem_for(error: ProviderConfigError) -> ProblemError:
    """Translate a refusal. ``str(error)`` is safe: no message here quotes a key.

    Every message in :class:`~chaudron.services.providers.ProviderConfigService` is
    built from our own fields and constants -- the submitted value is never
    interpolated, not even to say it is too short -- which is what makes passing it
    through as ``detail`` sound rather than convenient.
    """
    match error:
        case ProviderConfigNotFound():
            return ProblemError(
                slug="provider-config-not-found",
                title="No such provider configuration",
                status=404,
                detail=str(error),
            )
        case ProviderConsentRequired():
            # 422 rather than 403: the request is well-formed HTTP against a
            # resource the caller owns, and what is wrong with it is its content.
            return ProblemError(
                slug="provider-consent-required",
                title="Explicit agreement required",
                status=422,
                detail=str(error),
            )
        case ProviderConsentNotRecorded():
            return ProblemError(
                slug="provider-consent-not-recorded",
                title="No agreement to withdraw",
                status=409,
                detail=str(error),
            )
        case DuplicateProviderLabel():
            return ProblemError(
                slug="provider-label-taken",
                title="That name is already in use",
                status=409,
                detail=str(error),
            )
        case ProviderConfigAlreadyExists():
            # 409 and not 422: the body may be perfectly valid, and what refuses it
            # is the state of the resource collection -- a household already holds
            # the one configuration it is allowed. The remedy is a different
            # request (archive, or PATCH), which is what a conflict means.
            return ProblemError(
                slug="provider-config-already-exists",
                title="This household already has a provider configuration",
                status=409,
                detail=str(error),
            )
        case UnknownProvider():
            return ProblemError(
                slug="provider-not-offered",
                title="No such provider",
                status=422,
                detail=str(error),
            )
        case ProviderProbeFailed():
            # 422 and not 502, deliberately. What could not be reached is an
            # endpoint named *in this request body*, on a network the household
            # controls, so the content is what is wrong -- and charging a
            # household's typo to the instance's 5xx rate would bury the failures
            # that really are ours.
            return ProblemError(
                slug="provider-probe-failed",
                title="The Ollama endpoint could not be interrogated",
                status=422,
                detail=str(error),
            )
        case InvalidProviderConfig() | _:
            # The nine subclasses above are exhaustive, and the fallback is folded
            # into the last branch rather than left as a bare `case _` that a reader
            # has to check is unreachable: a tenth refusal added later without a
            # translation is answered as a refused configuration, which is what it
            # will be, rather than escaping as a 500.
            return ProblemError(
                slug="provider-config-invalid",
                title="Provider configuration refused",
                status=422,
                detail=str(error),
            )


# --------------------------------------------------------------------------- #
# The banner
# --------------------------------------------------------------------------- #


@router.get(
    "/capabilities",
    response_model=ProviderCapabilitiesOut,
    summary="Describe the household's model provider",
)
async def provider_capabilities(
    household_id: HouseholdDep, service: ProviderServiceDep
) -> ProviderCapabilitiesOut:
    """Always ``200``, including when nothing is configured.

    Having no provider is a normal state with a screen of its own, not an error: a
    4xx here would make the client render a failure for a household that has simply
    not finished setting up.
    """
    view = await service.status(household_id)
    return ProviderCapabilitiesOut(
        configured=view.configured,
        mode=view.mode,
        provider=view.provider,
        model=view.model,
        capabilities=CapabilityFlagsOut(
            vision=view.supports_vision,
            structured_output=view.supports_structured_output,
        ),
        degraded=view.degraded,
        degraded_reasons=[_reason(notice) for notice in view.notices],
    )


# --------------------------------------------------------------------------- #
# The configuration screen
# --------------------------------------------------------------------------- #


@router.get(
    "/catalogue",
    response_model=list[ProviderChoiceOut],
    summary="The providers this instance offers, and what each one needs",
)
async def provider_catalogue(
    _: HouseholdDep, service: ProviderConfigServiceDep
) -> list[ProviderChoiceOut]:
    """Reference data, not household data. Always an array, never a ``404``.

    Behind the household resolver all the same, for the reason
    ``test_every_v1_route_resolves_a_household_or_is_an_auth_route`` enforces: a
    ``/v1`` route that never posts a tenant runs outside row-level security, which
    fails safe but silently and confusingly.
    """
    return [_choice_out(choice) for choice in await service.choices()]


@router.get(
    "",
    response_model=list[ProviderConfigOut],
    summary="This household's provider configurations",
)
async def list_provider_configs(
    household_id: HouseholdDep, service: ProviderConfigServiceDep
) -> list[ProviderConfigOut]:
    """The household's live configuration, in a list of at most one.

    A list because that is the shape this route has always published, and because
    an empty array is the honest answer for a household that has configured
    nothing.

    Readable by any member: nothing here is a credential. Four characters of a key
    exist precisely so a household can recognise which of its keys is installed,
    and hiding the list from the people who use the feature would leave them unable
    to see why it is switched off.

    Archived configurations are absent. They are kept so the household can still
    answer "what did we send, to whom, and when did we agree?", which is a question
    for an art. 15 export rather than for a settings screen.
    """
    return [_to_out(view) for view in await service.list_configs(household_id)]


@router.post(
    "",
    response_model=ProviderConfigOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a provider configuration, with the agreement that lets it be used",
)
async def create_provider_config(
    body: CreateConfigIn,
    principal: OwnerDep,
    household_id: HouseholdDep,
    service: ProviderConfigServiceDep,
) -> ProviderConfigOut:
    """Store a configuration and, where one is needed, date the agreement. **Owner only.**

    Two things happen before anything is written, and both can fail the request.
    The consent is checked against what the configuration would actually transmit,
    so a hosted endpoint cannot be stored under the local exemption. And an
    ``ollama`` configuration is **probed**: ADR-0007 is explicit that a
    configuration whose abilities nobody established must not be stored, because
    the interface would then promise a feature on a guess.

    ``principal`` is here for
    :attr:`~chaudron.services.providers.CreateProviderConfig.created_by_user_id`
    and not only for the guard: an agreement nobody signed is what left an excluded
    member's export token running for months (audit AUD-027), and the same column
    exists here so the same question stays answerable.
    """
    try:
        created = await service.create(
            household_id,
            CreateProviderConfig(
                label=body.label,
                mode=body.mode,
                provider_code=body.provider,
                model=body.model,
                consent_granted=body.consent_granted,
                created_by_user_id=principal.user_id,
                base_url=body.base_url,
                # The one place the plaintext is read, and it goes straight into a
                # method that seals it.
                api_key=None if body.api_key is None else body.api_key.get_secret_value(),
            ),
        )
    except ProviderConfigError as error:
        raise _problem_for(error) from None
    return _to_out(created)


@router.patch(
    "/{config_id}",
    response_model=ProviderConfigOut,
    summary="Change a provider configuration, or replace its key",
)
async def update_provider_config(
    config_id: uuid.UUID,
    body: UpdateConfigIn,
    household_id: OwnerHouseholdDep,
    service: ProviderConfigServiceDep,
) -> ProviderConfigOut:
    """Edit what can be edited. **Owner only.**

    Sending ``api_key`` again *is* the rotation procedure of ADR-0007: an idempotent
    write, the previous value overwritten rather than versioned, and the stored
    verdict reset because the provider's opinion was about a key that no longer
    exists.

    Changing an ``ollama`` endpoint or model re-runs the probe, and a probe that
    fails fails the edit: capabilities that survive a model change are a claim about
    something else.
    """
    try:
        updated = await service.update(
            household_id,
            config_id,
            UpdateProviderConfig(
                label=body.label,
                model=body.model,
                base_url=body.base_url,
                api_key=None if body.api_key is None else body.api_key.get_secret_value(),
                disabled=body.disabled,
            ),
        )
    except ProviderConfigError as error:
        raise _problem_for(error) from None
    return _to_out(updated)


@router.delete(
    "/{config_id}",
    response_model=ProviderConfigOut,
    summary="Retire a provider configuration",
)
async def archive_provider_config(
    config_id: uuid.UUID,
    household_id: OwnerHouseholdDep,
    service: ProviderConfigServiceDep,
) -> ProviderConfigOut:
    """Stop using it, keep the record of it. **Owner only.**

    ``200`` with the archived configuration rather than ``204``, because this does
    not delete anything: the row survives so the household can still see what it
    authorised and when, and the response is what tells the interface the archive
    landed. Erasure is ``DELETE /v1/households``, and it is a different operation
    with different consequences.
    """
    try:
        archived = await service.archive(household_id, config_id)
    except ProviderConfigError as error:
        raise _problem_for(error) from None
    return _to_out(archived)


@router.post(
    "/{config_id}/consent",
    response_model=ProviderConfigOut,
    summary="Agree that this household's data may be sent to this provider",
)
async def grant_provider_consent(
    config_id: uuid.UUID,
    body: ConsentGrantIn,
    household_id: OwnerHouseholdDep,
    service: ProviderConfigServiceDep,
) -> ProviderConfigOut:
    """Record the agreement (art. 6(1)(a); art. 9(2)(a)). **Owner only.**

    Its own route rather than a field on the edit, so that agreeing is always a
    deliberate act performed against a screen that has just said what leaves the
    instance and who receives it -- which is the informing half of art. 7, and the
    half a checkbox buried in a settings form does not deliver.

    Idempotent in the direction that keeps the record honest: granting again on a
    live agreement leaves the original date alone. Granting after a withdrawal is a
    new agreement and takes today's date, and clears the withdrawal with it.
    """
    if not body.granted:
        raise ProblemError(
            slug="provider-consent-required",
            title="Explicit agreement required",
            status=422,
            detail=(
                "this route records an agreement; send granted: true, or withdraw the "
                "existing one with DELETE on the same path"
            ),
        )
    try:
        granted = await service.grant_consent(household_id, config_id)
    except ProviderConfigError as error:
        raise _problem_for(error) from None
    return _to_out(granted)


@router.delete(
    "/{config_id}/consent",
    response_model=ProviderConfigOut,
    summary="Withdraw the agreement to send this household's data to this provider",
)
async def withdraw_provider_consent(
    config_id: uuid.UUID,
    household_id: OwnerHouseholdDep,
    service: ProviderConfigServiceDep,
) -> ProviderConfigOut:
    """Stop the sending, keep the record. **Owner only.**

    Symmetrical with the grant above, and the symmetry is deliberate: whoever may
    give this household's agreement is whoever may take it back. The asymmetric
    reading -- anyone may switch a provider off -- sounds safe and is not, because a
    withdrawal is also how a household's feature is broken, and the row records who
    agreed and not who withdrew.

    It takes effect at the **next request**: the gate re-reads these two columns on
    every provider load, so nothing further is sent from the moment this returns.
    The configuration and its history stay, which is what art. 7(3) asks for --
    withdrawal must be as easy as consent, and must not cost the household the
    record of what it once authorised.
    """
    try:
        withdrawn = await service.withdraw_consent(household_id, config_id)
    except ProviderConfigError as error:
        raise _problem_for(error) from None
    return _to_out(withdrawn)


@router.post(
    "/{config_id}/probe",
    response_model=ProviderConfigOut,
    summary="Ask this household's Ollama again what its model can do",
)
async def probe_provider_config(
    config_id: uuid.UUID,
    household_id: OwnerHouseholdDep,
    service: ProviderConfigServiceDep,
) -> ProviderConfigOut:
    """Re-detect the effective capabilities. **Owner only, and Ollama only.**

    A household that pulls a vision model onto an endpoint already registered has
    no other way to tell Chaudron: the capability columns are what the interface
    disables the receipt photograph on, and until they are re-read the feature
    stays greyed out for a model that can now see.

    Refused for the hosted providers rather than answered with a no-op: their
    capabilities come from this instance's model table, and reporting "refreshed"
    after doing nothing is a screen that lies. It calls no model and spends
    nothing -- the two requests are ``/api/version`` and ``/api/show`` against the
    household's own server.
    """
    try:
        probed = await service.reprobe(household_id, config_id)
    except ProviderConfigError as error:
        raise _problem_for(error) from None
    return _to_out(probed)
