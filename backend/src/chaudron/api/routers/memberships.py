"""``/v1/households/members`` and ``/v1/households/invitations`` -- who may sign in.

The second person. Before this module a household had exactly one account, for
the whole of its life: ``services/auth.py`` wrote the only ``household_member``
row that existed anywhere, at registration, as ``owner``. There was no way to
invite, no way to join and no way to leave, which is why a security finding about
what a ``viewer`` could spend was filed as *latent* -- there was no way to become
one.

**Do not confuse this with ``/v1/members``.** That resource is
``household_person``: the **eaters**, with allergens and a diet and no account,
including the six-month-old and the grandmother who comes on Sundays. This one is
``household_member``: an account with a role, which can sign in.
:class:`chaudron.domain.models.HouseholdPerson` argues why they are two tables.

Five routes here, plus one that lives elsewhere on purpose.

**Inviting is owner-only.** ``api/deps.py`` already draws that line in one place:
a route that *hands out* a credential (``GET /v1/calendar/subscription``) or
*accepts* one on the household's behalf (the export targets) belongs to whoever
carries the consequence. An invitation is the strongest credential this
application produces -- it grants a membership, and a membership reads every
eater's allergens and infant age band, health data under GDPR article 9. A
``member`` may issue a *machine token*, because the resolver kills it the moment
their own membership goes (``routers/tokens.py``); an invitation is not like that
at all. It creates a **peer**, who stays after the person who invited them has
left.

**An invitation may never grant ``owner``**, and the database says so as well as
this module (``ck_household_invitation_role_not_owner``). That answer is argued in
``services/memberships.py``: ownership handed out cannot be taken back by the
person who handed it out, and it should not be reachable by pasting a code into a
form.

**Removal is guarded as a member action, and the handler decides the rest.** An
owner may remove anybody; anybody else may remove only themselves, which is what
makes "leave this household" possible without a second endpoint. The one case
that is refused rather than resolved is the removal that would leave a household
with no owner at all -- see :class:`~chaudron.services.memberships.LastOwnerError`
for why inventing an answer there would be worse than refusing.

One consequence is worth stating because it is a limitation rather than an
oversight: a ``viewer`` cannot remove themselves, since ``MemberDep`` is what
"changes state in this household" means everywhere else in this API and a viewer
does not pass it. A viewer who wants out asks an owner. Widening that would mean
a role guard whose vocabulary is not the census's, and the census is worth more.

**Redemption is not here.** It is ``POST /v1/auth/invitations/redeem``, defined at
the bottom of this file on its own router, and the placement is the design rather
than an accident of prefixes. Everything under ``/v1/households`` resolves a
household from the caller's memberships; a redeemer has none in that household --
that is the entire point -- so there is nothing for ``X-Household-Id`` to be
checked against. Like ``POST /v1/auth/register``, it is a route *about the
account*: it changes what ``GET /v1/auth/session`` answers, and it is one of the
routes somebody with no membership at all calls. The tenant comes from the
invitation, resolved past row-level security by a ``SECURITY DEFINER`` function
(migration ``0022``), exactly as a machine token's does.

**Nothing here logs an invitation.** This module installs no logger, and the value
appears in exactly one response body in the whole application -- the ``201`` that
creates it. There is no route that reads one back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, SecretStr

from chaudron.api.deps import (
    HouseholdDep,
    InvitationServiceDep,
    MemberDep,
    MembershipServiceDep,
    OwnerDep,
    OwnerHouseholdDep,
    PrincipalDep,
    require_session,
)
from chaudron.api.errors import ProblemError
from chaudron.api.schemas import StrictModel
from chaudron.domain.models import MembershipRole
from chaudron.services.memberships import (
    MAX_EXPIRY_DAYS,
    AlreadyAMemberError,
    InvitationExpiryOutOfRangeError,
    InvitationNotAcceptedError,
    InvitationRoleNotAllowedError,
    InvitationSummary,
    IssuedInvitation,
    LastOwnerError,
    MembershipNotFoundError,
    MembershipRemovalNotPermittedError,
    MembershipSummary,
    TooManyInvitationsError,
)

#: Declared on the router, so it applies to every route in this module and to
#: every route added to it later. None of these is a thing a program left in an
#: appliance does: reading the membership list is a map of who holds access to a
#: household's health data, and the other four change it.
router = APIRouter(
    prefix="/v1/households", tags=["memberships"], dependencies=[Depends(require_session)]
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
#
# Declared here rather than in `api/schemas.py`, following `routers/auth.py`: they
# are the vocabulary of one resource, nothing else refers to them, and the shared
# module is edited by every change at once.


#: The two roles an invitation may carry, as a closed vocabulary at the boundary:
#: ``{"role": "owner"}`` is a ``422`` naming the field rather than a refusal raised
#: three layers down. It is stated three times on purpose -- here, in
#: ``services/memberships.py``, and as a ``CHECK`` constraint -- because this is the
#: rule whose accidental removal would be least visible in review.
InvitableRole = Literal[MembershipRole.MEMBER, MembershipRole.VIEWER]


class InvitationCreateIn(StrictModel):
    role: InvitableRole = MembershipRole.MEMBER
    expires_in_days: int | None = Field(default=None, ge=1, le=MAX_EXPIRY_DAYS)


class InvitationRedeemIn(StrictModel):
    """The value the invitee was handed.

    :class:`~pydantic.SecretStr` so that a model dumped into a log line, a
    traceback frame or a debugger prints a mask rather than the credential --
    the same lock ``routers/auth.py`` puts on a password and
    ``routers/export_targets.py`` on a third party's token.
    """

    token: SecretStr = Field(min_length=1, max_length=256)


class InvitationOut(BaseModel):
    """A pending invitation, with no secret in it."""

    id: str
    role: str
    prefix: str
    last4: str
    created_at: str
    expires_at: str


class InvitationCreatedOut(InvitationOut):
    """The **only** response in this API that carries an invitation value.

    There is no route that reads one back. If it is lost it is revoked and
    re-issued, which costs one click; a credential a screenshot can steal is a
    credential the household cannot reason about.
    """

    token: str


class MembershipOut(BaseModel):
    user_id: str
    display_name: str
    email: str
    role: str
    joined_at: str
    #: Whether this row is the caller. The interface needs it to label the
    #: "leave this household" button, and computing it server-side saves the
    #: client from comparing identifiers it would have to fetch separately.
    is_self: bool


class RedeemedOut(BaseModel):
    """What the redemption produced. The client re-reads ``GET /v1/auth/session``
    afterwards for the authoritative membership list; this is what to show now."""

    household_id: str
    household_name: str
    role: str


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #


@router.get(
    "/members",
    response_model=list[MembershipOut],
    summary="The accounts that may open this household",
)
async def list_memberships(
    household_id: HouseholdDep, principal: PrincipalDep, service: MembershipServiceDep
) -> list[MembershipOut]:
    """Readable by everybody in the household, ``viewer`` included.

    Knowing who can see your household's data is not a privilege; it is the
    precondition for noticing that somebody who should not be there is.
    """
    return [
        _membership_out(summary, caller=principal.user_id)
        for summary in await service.list_members(household_id)
    ]


@router.delete(
    "/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove somebody's access, or leave the household",
)
async def remove_membership(
    user_id: uuid.UUID,
    household_id: MemberDep,
    principal: PrincipalDep,
    service: MembershipServiceDep,
) -> Response:
    """Delete one ``household_member`` row. Who may delete which is the service's.

    Nothing else has to happen for the removal to take effect. The account's
    machine tokens for this household stop resolving on their next request --
    ``chaudron_resolve_machine_token`` joins back to ``household_member`` every
    time (migration ``0011``) -- and their browser sessions simply stop listing
    this household. There is no revocation to remember.
    """
    try:
        await service.remove(
            household_id=household_id,
            actor_user_id=principal.user_id,
            target_user_id=user_id,
        )
    except MembershipRemovalNotPermittedError as exc:
        raise ProblemError(
            slug="membership-removal-forbidden",
            title="Not allowed to remove this member",
            status=403,
            detail=str(exc),
        ) from None
    except MembershipNotFoundError as exc:
        raise ProblemError(
            slug="membership-not-found",
            title="No such member",
            status=404,
            detail=str(exc),
        ) from None
    except LastOwnerError as exc:
        raise ProblemError(
            slug="household-would-have-no-owner",
            title="A household needs an owner",
            status=409,
            detail=str(exc),
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #


@router.get(
    "/invitations",
    response_model=list[InvitationOut],
    summary="The invitations that could still be used",
)
async def list_invitations(
    household_id: OwnerHouseholdDep, service: InvitationServiceDep
) -> list[InvitationOut]:
    """Owner-only, like issuing one: this is the list of open doors.

    Spent, revoked and expired rows are absent rather than flagged. What an owner
    needs from this list is "who could still walk in".
    """
    return [_invitation_out(summary) for summary in await service.list_pending(household_id)]


@router.post(
    "/invitations",
    response_model=InvitationCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Invite somebody to this household, shown once",
)
async def create_invitation(
    household_id: HouseholdDep,
    owner: OwnerDep,
    service: InvitationServiceDep,
    payload: InvitationCreateIn,
) -> InvitationCreatedOut:
    """Mint an invitation for the resolved household. **The only response with a value.**

    Both ``HouseholdDep`` and ``OwnerDep`` are asked for, and FastAPI solves the
    household once for the pair: the first is the tenant written onto the row, the
    second is the person recorded as having vouched for it. There is no header the
    holder of the value could later send to point it at another household.

    The value is returned here and nowhere else, ever. Hand it over out of band --
    read it out, show it as a code, type it into a message. Chaudron sends no
    email, and this route does not pretend it does.
    """
    try:
        issued = await service.create(
            household_id=household_id,
            created_by_user_id=owner.user_id,
            role=payload.role,
            expires_in_days=payload.expires_in_days,
        )
    except InvitationRoleNotAllowedError as exc:
        raise ProblemError(
            slug="invitation-role-not-allowed",
            title="An invitation cannot grant that role",
            status=422,
            detail=str(exc),
        ) from None
    except InvitationExpiryOutOfRangeError as exc:
        raise ProblemError(
            slug="invitation-expiry-out-of-range",
            title="Expiry out of range",
            status=422,
            detail=str(exc),
        ) from None
    except TooManyInvitationsError as exc:
        raise ProblemError(
            slug="invitation-limit-reached",
            title="Too many pending invitations",
            status=409,
            detail=(
                f"This household already has {exc.limit} invitations waiting to be used. "
                f"Revoke one that is no longer needed before creating another."
            ),
        ) from None

    return _invitation_created_out(issued)


@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an invitation, immediately",
)
async def revoke_invitation(
    invitation_id: uuid.UUID, household_id: OwnerHouseholdDep, service: InvitationServiceDep
) -> Response:
    """Close the door now. A value handed out before this call stops working.

    ``204`` whether or not a row was there to revoke -- the same choice
    ``routers/tokens.py`` makes and for the same reason: a ``404`` would let
    somebody discover which identifiers exist, and "it is not usable any more" is
    true either way.
    """
    await service.revoke(household_id=household_id, invitation_id=invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Redemption
# --------------------------------------------------------------------------- #
#
# Its own router, under `/v1/auth`, and the module docstring argues the placement.
# In one line: the caller is not a member of the household yet, so no household can
# be resolved for them from a header or from a membership list -- the invitation is
# what decides it. That makes this a route about the *account*, like registration,
# and it changes exactly what `GET /v1/auth/session` reports.

redeem_router = APIRouter(prefix="/v1/auth/invitations", tags=["memberships"])


@redeem_router.post(
    "/redeem",
    response_model=RedeemedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Join a household with an invitation",
)
async def redeem_invitation(
    payload: InvitationRedeemIn, principal: PrincipalDep, service: InvitationServiceDep
) -> RedeemedOut:
    """Spend an invitation and become a member of the household that issued it.

    Behind :data:`~chaudron.api.deps.PrincipalDep`, so it needs a signed-in
    account **and** a valid ``X-CSRF-Token``, and a machine token cannot reach it.
    Both matter. An account is what the membership is attached to -- there is
    nobody to add otherwise -- and without the CSRF check any page on the internet
    could quietly enrol a visitor into a household they have never heard of.

    The invitation is not a way to *create* an account, and deliberately so:
    somebody with none registers first, which gives them their own household, and
    then joins this one. An account belonging to two households is the case
    ``X-Household-Id`` has existed for since authentication landed.

    Every refusal that is about the value itself is one answer -- unknown, expired,
    revoked, already spent, issued by an account since disabled or since demoted --
    because they are one ``WHERE`` clause in migration ``0022`` plus one lost race
    reported identically. "You already belong to that household" is the single
    case answered differently, and it costs nothing: the caller can read their own
    membership list.
    """
    try:
        redeemed = await service.redeem(
            presented=payload.token.get_secret_value(), user_id=principal.user_id
        )
    except AlreadyAMemberError as exc:
        raise ProblemError(
            slug="already-a-member",
            title="Already a member of this household",
            status=409,
            detail=str(exc),
        ) from None
    except InvitationNotAcceptedError:
        raise ProblemError(
            slug="invitation-not-accepted",
            title="This invitation cannot be used",
            status=403,
            detail=(
                "This invitation is not usable. It may never have existed, or it may "
                "have expired, been revoked, or already been used. Ask whoever invited "
                "you for a new one."
            ),
        ) from None
    return RedeemedOut(
        household_id=str(redeemed.household_id),
        household_name=redeemed.household_name,
        role=str(redeemed.role),
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _invitation_out(summary: InvitationSummary) -> InvitationOut:
    return InvitationOut(
        id=str(summary.id),
        role=str(summary.role),
        prefix=summary.prefix,
        last4=summary.last4,
        created_at=_instant(summary.created_at),
        expires_at=_instant(summary.expires_at),
    )


def _invitation_created_out(issued: IssuedInvitation) -> InvitationCreatedOut:
    return InvitationCreatedOut(**_invitation_out(issued.summary).model_dump(), token=issued.token)


def _membership_out(summary: MembershipSummary, *, caller: uuid.UUID) -> MembershipOut:
    return MembershipOut(
        user_id=str(summary.user_id),
        display_name=summary.display_name,
        email=summary.email,
        role=str(summary.role),
        joined_at=_instant(summary.joined_at),
        is_self=summary.user_id == caller,
    )


def _instant(value: datetime) -> str:
    """The contract's ``…Z`` spelling, not Python's ``+00:00``.

    ``api/schemas.py`` makes the same correction on every timestamp it serialises,
    and clients do compare strings.
    """
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["redeem_router", "router"]
