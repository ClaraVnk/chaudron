"""The data subject's levers: over their household, and over themselves.

``GET /v1/households/export`` is articles 15 and 20; ``DELETE /v1/households`` is
article 17 over a household; ``DELETE /v1/account`` is article 17 over the person.
The first two close open item O-01 of
``docs/security-pentest-2026-08-04.md``, which counted fifty-two routes and nine
``DELETE``s, none of which erased anything, and quoted
``docs/security-model.md`` conceding of access and portability: *"To be built.
Nothing exists."*

**Why the prefix is ``/v1/households`` and not ``/v1/privacy``.** It is the path
``services/export_targets.py`` has been pointing readers at since ADR-0010 --
*"withdrawal is not a deletion request; ``DELETE /v1/households`` is where erasure
lives"* -- and it named a route that did not exist. There is no identifier in the
path because there is none anywhere else in this API either: the tenant is
resolved server-side from the credential and, at most, selected by
``X-Household-Id`` (``api/deps.py``). A household in the path would be a household
a client asserts.

**Both are owner-only, and the two have different reasons.**

Erasure destroys data belonging to every member of the household, not only to
whoever asked. ``require_owner`` is the same guard that already covers handing out
a calendar credential and accepting a third party's export token: the decisions
whose consequence the household as a whole carries.

The export is owner-only for a narrower reason, and it is worth being plain about
what that costs. A member is a data subject too, and article 15 is their right,
not the owner's. But there is no per-person export to give them: an inventory, a
receipt, a shopping list belong to the household and cannot be split by person,
so any export a member could obtain would be the whole household's -- including
the other members' allergen records. What is on offer here is therefore the
*household's* copy, and it is bounded to the role that can already erase the same
data. **A member exercising article 15 against a Chaudron instance goes through
the operator**, who is the controller in any case (``docs/security-model.md``
section 8). That is a product decision, it is not a compliance opinion, and it sits
next to the row section 8.5 already flags as unsettled -- *"the member who leaves:
not settled"*.

**Neither is reachable by a machine token.** ``require_owner`` resolves a
:class:`~chaudron.services.auth.Principal`, which only a browser cookie produces,
so a long-lived credential in a home-automation appliance can neither read the
household out nor delete it -- whatever scopes it was issued with.
``tests/api/test_route_authentication.py`` asserts that in both directions.

**The third is neither owner-only nor household-scoped**, and the asymmetry is the
design rather than an omission. An account belongs to no household -- that is why
``user_account`` has no tenant column -- so there is no role to check and no tenant
to resolve: the only authority anybody needs over their own identity is holding it.
What that route *does* need is a rule about the households the account leaves
behind, and it takes none: it refuses when its own erasure would leave one with no
owner, which is ``services/memberships.py``'s ``LastOwnerError`` applied to every
membership at once. The unsettled row of section 8.5 -- *"the member who leaves"*
-- is still unsettled, and this route is careful not to settle it by accident.

**Nothing is logged here.** For the reason ``routers/members.py`` gives: the
safest way not to write a member's allergens into a log line is to have nowhere to
write one. The single record this feature keeps is written by
``services/privacy.py``, and it carries an identifier and a set of integers.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel

from chaudron.api.deps import (
    SESSION_COOKIE,
    OwnerDep,
    OwnerHouseholdDep,
    PrincipalDep,
    PrivacyServiceDep,
)
from chaudron.api.errors import ProblemError
from chaudron.services.privacy import (
    AccountErasureBlockedError,
    AccountErasureReceipt,
    ErasureBlockedError,
    ErasureReceipt,
)

router = APIRouter(prefix="/v1/households", tags=["privacy"])

#: A second router rather than a second path under the first, because the resource
#: is a different one. ``/v1/households`` is a tenant and every route under it
#: resolves one; an account belongs to no household -- that is the whole reason
#: ``user_account`` has no tenant column -- and a route that erases it must not
#: require an ``X-Household-Id`` to say which. It carries no identifier in the path
#: for the reason every other route here gives: the subject is resolved from the
#: credential, and an account in the path is an account a client asserts.
account_router = APIRouter(prefix="/v1/account", tags=["privacy"])


class ErasureReceiptOut(BaseModel):
    """What the erasure removed, table by table.

    Returned rather than a bare ``204`` because this is the only artefact the
    person who asked walks away with: after this call there is no account screen
    left to check, no row to point at, and no support ticket that can be answered
    by looking. The counts are the household's own data, they name nothing, and
    they are the same numbers written to the operator's log.
    """

    household_id: str
    erased_at: dt.datetime
    #: Table name to rows removed, counted immediately before the delete and
    #: verified to be zero immediately after it.
    rows_erased: dict[str, int]
    #: What deliberately survived, and why. Both entries are argued in
    #: ``services/privacy.py``; saying so here is what keeps "erased" from being
    #: read as more than it is.
    not_erased: dict[str, str]


#: Fixed text, so the two sentences that qualify every erasure cannot drift from
#: the reasoning that produced them.
_NOT_ERASED: dict[str, str] = {
    "product (public catalogue)": (
        "Open Food Facts entries carry no household and are shared by every "
        "instance. They are not this household's data to erase; the products it "
        "created itself were erased with it."
    ),
    "user_account": (
        "An account is independent of any household and may belong to several. "
        "Erasing this household ended its membership and left the identity, which "
        "may still open others. DELETE /v1/account erases the account itself."
    ),
}

#: The counterpart for an account erasure. Same purpose as ``_NOT_ERASED``: fixed
#: text, so the sentences that qualify what "erased" means cannot drift from the
#: reasoning in ``services/privacy.py``.
_ACCOUNT_NOT_ERASED: dict[str, str] = {
    "household (and everything in it)": (
        "A household survives its members. Erasing this account ended its "
        "memberships; every household it opened still has an owner, and its stock, "
        "receipts and lists belong to the household rather than to any one person. "
        "Erasing a household is DELETE /v1/households, by its owner."
    ),
    "household_person": (
        "An eater's record -- name, diet, allergens -- belongs to the household "
        "that plans meals around it, not to the account that happened to create "
        "it. The link from this account was cleared; the record itself is erased "
        "with its household, or from POST/DELETE /v1/members before that."
    ),
}


@router.get(
    "/export",
    response_model=None,
    summary="Everything this household holds, as a portable document",
)
async def export_household(
    household_id: OwnerHouseholdDep,
    principal: OwnerDep,
    service: PrivacyServiceDep,
) -> dict[str, Any]:
    """Article 15 and article 20, in one document.

    No response model on purpose. The body has one key per table of the schema and
    the schema's own column names, so a Pydantic model here would be a second
    declaration of the data model, kept in step by hand, and silently truncating
    the export the day the two disagreed. The shape is fixed by
    ``export_version``; the content is fixed by ``domain/models.py``.
    """
    return await service.export(household_id, requested_by=principal.user_id)


@router.delete(
    "",
    response_model=ErasureReceiptOut,
    summary="Erase this household and everything belonging to it",
)
async def erase_household(
    household_id: OwnerHouseholdDep, service: PrivacyServiceDep
) -> ErasureReceiptOut:
    """Article 17. Irreversible, and there is no archived state to fall back on.

    ``Household.archived_at`` exists and is written by nothing (the pentest says
    so, and it is still true after this route): archiving is not erasure and
    offering it here as a softer option would be the partial erasure the security
    model calls a non-compliance that looks like compliance.
    """
    try:
        receipt = await service.erase(household_id)
    except ErasureBlockedError as error:
        raise _erasure_blocked(error) from None
    return _to_out(receipt)


def _to_out(receipt: ErasureReceipt) -> ErasureReceiptOut:
    return ErasureReceiptOut(
        household_id=str(receipt.household_id),
        erased_at=receipt.erased_at,
        rows_erased=dict(receipt.rows_erased),
        not_erased=dict(_NOT_ERASED),
    )


class EndedMembershipOut(BaseModel):
    """One household this account used to open, and with what authority."""

    household_id: str
    name: str
    role: str


class AccountErasureReceiptOut(BaseModel):
    """What the erasure of an account removed, and what deliberately survived.

    Returned rather than a ``204`` for the same reason the household's receipt is:
    after this call the person has no account, therefore no screen, therefore no way
    to check anything. The households named here are ones they were a member of a
    moment ago and could already list from ``GET /v1/auth/session``.
    """

    user_id: str
    erased_at: dt.datetime
    memberships_ended: list[EndedMembershipOut]
    #: Rate-limit buckets keyed on this account's address, across every limiter.
    #: Reported because it is the one table no cascade reaches, so an erasure that
    #: silently skipped it would be the partial erasure section 8.5 calls a
    #: non-compliance -- and the number is how somebody checks it did not.
    rate_limit_rows_forgotten: int
    not_erased: dict[str, str]


@account_router.delete(
    "",
    response_model=AccountErasureReceiptOut,
    summary="Erase this account, its credentials and its memberships",
)
async def erase_account(
    response: Response, principal: PrincipalDep, service: PrivacyServiceDep
) -> AccountErasureReceiptOut:
    """Article 17 over the person rather than over the household.

    **The subject is the caller, and only ever the caller.** There is no identifier
    in the path and none in a body; the account erased is the one that owns the
    session presenting the request. So there is no role to check -- an owner has no
    authority over somebody else's identity, and a viewer needs none over their own
    -- and equally no household: this route is the one place in the API where
    ``X-Household-Id`` is not merely optional but meaningless.

    **Unreachable by a machine token.** :data:`PrincipalDep` resolves a browser
    session, so a credential left in an appliance cannot erase the account that
    issued it -- whatever scopes it holds. The same property, for the same reason,
    as the two routes above.

    **No re-authentication, deliberately, and it is the weaker of two consistent
    choices.** A stolen session can already erase every household this account owns
    through ``DELETE /v1/households``, which asks for no password either. Requiring
    one *here alone* would protect the smaller loss while leaving the larger one
    open, and read as a control where there is none. Asking for it on **both**
    destructive routes is a real hardening and worth doing; doing it on one is
    theatre. It is named here so the next person adding it knows there are two
    places, and that an account created through an external identity provider has
    ``password_hash IS NULL`` and needs an answer before either of them can demand
    a password.

    The cookie is cleared on the way out. The session row cascaded away with the
    account a moment earlier, so the credential is already dead; deleting the cookie
    stops the browser presenting a token that can only ever earn a ``401``.
    """
    try:
        receipt = await service.erase_account(principal.user_id)
    except AccountErasureBlockedError as error:
        raise _account_erasure_blocked(error) from None
    # Same attributes as when it was set, or the browser deletes nothing.
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")
    return _account_to_out(receipt)


def _account_to_out(receipt: AccountErasureReceipt) -> AccountErasureReceiptOut:
    return AccountErasureReceiptOut(
        user_id=str(receipt.user_id),
        erased_at=receipt.erased_at,
        memberships_ended=[
            EndedMembershipOut(
                household_id=str(membership.household_id),
                name=membership.name,
                role=membership.role.value,
            )
            for membership in receipt.memberships_ended
        ],
        rate_limit_rows_forgotten=receipt.rate_limit_rows_forgotten,
        not_erased=dict(_ACCOUNT_NOT_ERASED),
    )


def _account_erasure_blocked(error: AccountErasureBlockedError) -> ProblemError:
    """``409``: the request is legitimate and answering it would orphan a household.

    Not a ``403``: nothing about the caller's authority is in question, and this is
    their own account. Not a ``500``: nothing is broken. The same status as the
    household erasure's own refusal, because it is the same kind of answer -- the
    instance will not perform an act it cannot perform completely, and it says what
    has to happen first.

    The households are named in an extension field as well as in the sentence, so a
    client can offer the erasure of each one rather than asking the person to read a
    paragraph and find them. They are households the caller is a member of.
    """
    return ProblemError(
        slug="account-erasure-would-orphan-a-household",
        title="Account erasure cannot be completed",
        status=409,
        detail=str(error),
        households=[
            {"household_id": str(household.household_id), "name": household.name}
            for household in error.households
        ],
    )


def _erasure_blocked(error: ErasureBlockedError) -> ProblemError:
    """``409``: the request is legitimate and the instance cannot honour it yet.

    Not a ``500``. Nothing is broken -- a deployment has retained receipt images
    and this application has no object-storage client to delete them with, which
    is a configuration this build does not support rather than a failure. And not a
    ``202``: accepting the request and erasing the rows would leave the objects and
    report a complete erasure, which is exactly the shape of non-compliance
    ``docs/security-model.md`` section 8.5 warns about.

    ``str(error)`` is safe to pass through: the message is built from a count and
    constants, and quotes no key, no merchant and no line.
    """
    return ProblemError(
        slug="erasure-incomplete-by-construction",
        title="Erasure cannot be completed",
        status=409,
        detail=str(error),
        retained_images=error.retained_images,
    )
