"""A second person can join a household, and only in the ways that were intended.

Everything below is driven over HTTP against a real PostgreSQL, because the
properties being asserted are not properties of a Python function: single use is a
conditional ``UPDATE``, the tenant of a redemption comes from a ``SECURITY
DEFINER`` function, and "an invitation may not grant ownership" is a ``CHECK``
constraint as well as a validator.

Before this feature the whole file would have been unwritable. ``HouseholdMember``
was created in exactly one place -- registration, as ``owner`` -- so a household
had one account for its entire life and ``member`` and ``viewer`` were unreachable
states. ``tests/api/test_role_authorisation.py`` had to *demote the caller with an
UPDATE* to test a viewer at all; from here on a viewer is something the API can
produce.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE
from chaudron.domain.models import (
    Household,
    HouseholdInvitation,
    HouseholdMember,
    MembershipRole,
    UserAccount,
    UserSession,
)
from chaudron.services.auth import hash_token, new_token
from chaudron.services.memberships import INVITATION_PREFIX
from tests.conftest import MakeHousehold, MakeUser, household_headers

pytestmark = pytest.mark.integration

_INVITATIONS: Final = "/v1/households/invitations"
_MEMBERS: Final = "/v1/households/members"
_REDEEM: Final = "/v1/auth/invitations/redeem"


# --------------------------------------------------------------------------- #
# A second signed-in account
# --------------------------------------------------------------------------- #
#
# The suite's `api_client` is one account. Every test here needs two, because the
# whole point of the feature is the person who is *not* already in the household.


@dataclass(frozen=True, slots=True)
class Guest:
    """Another account, signed in, belonging to nothing yet."""

    user: UserAccount
    client: httpx.AsyncClient


@pytest.fixture
async def guest(
    api_app: FastAPI, db_session: AsyncSession, make_user: MakeUser
) -> AsyncIterator[Guest]:
    """A second account with its own live session, built the way ``signed_in`` is.

    A real ``user_session`` row rather than a call to ``POST /v1/auth/login``, for
    the reason the shared fixture gives: a fixture that issued an HTTP request
    would make every test here depend on the sign-in endpoint working.
    """
    user = await make_user(display_name="Guest")
    token = new_token()
    now = datetime.now(UTC)
    record = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=new_token(),
        expires_at=now + timedelta(days=30),
        idle_expires_at=now + timedelta(days=7),
    )
    db_session.add(record)
    await db_session.flush()

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: token},
        headers={CSRF_HEADER: record.csrf_token},
    ) as client:
        yield Guest(user=user, client=client)


async def _invite(
    client: httpx.AsyncClient, household: Household, *, role: str = "member"
) -> tuple[str, str]:
    """Create an invitation and return ``(token, invitation_id)``."""
    response = await client.post(
        _INVITATIONS, json={"role": role}, headers=household_headers(household)
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["token"], body["id"]


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #


async def test_an_owner_issues_an_invitation_and_sees_it_once(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    household = await make_household()
    response = await api_client.post(
        _INVITATIONS, json={"role": "member"}, headers=household_headers(household)
    )

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["token"].startswith(INVITATION_PREFIX)
    assert created["role"] == "member"
    assert created["last4"] == created["token"][-4:]

    listed = await api_client.get(_INVITATIONS, headers=household_headers(household))
    assert listed.status_code == 200
    (pending,) = listed.json()
    assert pending["id"] == created["id"]
    # The whole property of a credential returned once: the listing carries the
    # tail and nothing that could be assembled back into a usable value.
    assert "token" not in pending
    assert created["token"] not in listed.text


async def test_a_member_may_not_invite(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Issuing is owner-only: an invitation creates a peer who outlives the inviter."""
    household = await make_household(role=MembershipRole.MEMBER)
    response = await api_client.post(
        _INVITATIONS, json={"role": "member"}, headers=household_headers(household)
    )
    assert response.status_code == 403, response.text


async def test_an_invitation_may_not_grant_ownership(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Refused at the boundary, and the database would refuse it too."""
    household = await make_household()
    response = await api_client.post(
        _INVITATIONS, json={"role": "owner"}, headers=household_headers(household)
    )
    assert response.status_code == 422, response.text


async def test_the_database_refuses_an_owner_invitation_whatever_the_service_does(
    db_session: AsyncSession, make_household: MakeHousehold
) -> None:
    """``ck_household_invitation_role_not_owner``, asserted against PostgreSQL.

    The validator above can be deleted by a refactor; this cannot be, and the two
    are stated separately on purpose.
    """
    household = await make_household()
    owner = await db_session.scalar(
        select(HouseholdMember.user_id).where(HouseholdMember.household_id == household.id)
    )
    assert owner is not None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                HouseholdInvitation(
                    household_id=household.id,
                    created_by_user_id=owner,
                    role=MembershipRole.OWNER,
                    token_hash="0" * 64,
                    prefix=INVITATION_PREFIX,
                    last4="abcd",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await db_session.flush()


# --------------------------------------------------------------------------- #
# Redeeming
# --------------------------------------------------------------------------- #


async def test_a_guest_joins_the_household(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    household = await make_household(name="Colocation")
    token, _ = await _invite(api_client, household)

    redeemed = await guest.client.post(_REDEEM, json={"token": token})
    assert redeemed.status_code == 201, redeemed.text
    assert redeemed.json() == {
        "household_id": str(household.id),
        "household_name": "Colocation",
        "role": "member",
    }

    # The membership is what the rest of the application reads, so assert it
    # through the application rather than through the row.
    session = await guest.client.get("/v1/auth/session")
    assert session.status_code == 200
    assert [h["id"] for h in session.json()["households"]] == [str(household.id)]
    assert (await guest.client.get("/v1/inventory")).status_code == 200


async def test_an_invitation_cannot_be_redeemed_twice(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    db_session: AsyncSession,
    guest: Guest,
    make_user: MakeUser,
) -> None:
    """Single use, and the second attempt is refused as though the value never existed.

    The second redeemer is a *third* account, so the refusal cannot be explained
    away as "you were already a member": it is the invitation that is spent.
    """
    household = await make_household()
    token, _ = await _invite(api_client, household)

    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201

    third = await make_user(display_name="Third")
    third_token = new_token()
    now = datetime.now(UTC)
    db_session.add(
        UserSession(
            user_id=third.id,
            token_hash=hash_token(third_token),
            csrf_token=new_token(),
            expires_at=now + timedelta(days=30),
            idle_expires_at=now + timedelta(days=7),
        )
    )
    await db_session.flush()

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={SESSION_COOKIE: third_token},
        headers={CSRF_HEADER: "irrelevant"},
    ) as client:
        # The CSRF header has to be the third session's own, so fetch it the way a
        # browser does rather than guessing.
        session = await client.get("/v1/auth/session")
        client.headers[CSRF_HEADER] = session.json()["csrf_token"]
        again = await client.post(_REDEEM, json={"token": token})

    assert again.status_code == 403, again.text
    assert again.json()["type"].endswith("invitation-not-accepted")

    members = await db_session.scalars(
        select(HouseholdMember.user_id).where(HouseholdMember.household_id == household.id)
    )
    assert third.id not in set(members.all())


async def test_a_revoked_invitation_is_refused(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    household = await make_household()
    token, invitation_id = await _invite(api_client, household)

    revoked = await api_client.delete(
        f"{_INVITATIONS}/{invitation_id}", headers=household_headers(household)
    )
    assert revoked.status_code == 204

    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 403
    listed = await api_client.get(_INVITATIONS, headers=household_headers(household))
    assert listed.json() == []


async def test_an_expired_invitation_is_refused(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    db_session: AsyncSession,
    guest: Guest,
) -> None:
    """Expiry is in the resolver's ``WHERE``, not a check the service performs after."""
    household = await make_household()
    token, invitation_id = await _invite(api_client, household)
    await db_session.execute(
        update(HouseholdInvitation)
        .where(HouseholdInvitation.id == uuid.UUID(invitation_id))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.flush()

    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 403


async def test_an_unknown_value_is_refused_the_same_way(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    await make_household()
    response = await guest.client.post(_REDEEM, json={"token": f"{INVITATION_PREFIX}nonsense"})
    assert response.status_code == 403
    assert response.json()["type"].endswith("invitation-not-accepted")


async def test_an_existing_member_is_refused_without_spending_the_invitation(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """Handing the code to the wrong person must not burn it."""
    household = await make_household()
    token, _ = await _invite(api_client, household)

    response = await api_client.post(_REDEEM, json={"token": token})
    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("already-a-member")

    still_pending = await api_client.get(_INVITATIONS, headers=household_headers(household))
    assert len(still_pending.json()) == 1


async def test_a_stranger_cannot_redeem(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    anonymous_client: httpx.AsyncClient,
) -> None:
    """Redemption needs an account: there is nobody to attach a membership to."""
    household = await make_household()
    token, _ = await _invite(api_client, household)
    assert (await anonymous_client.post(_REDEEM, json={"token": token})).status_code == 401


async def test_a_viewer_invitation_produces_a_viewer(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    """The role on the invitation is the role in the household, and it bites.

    This is the assertion the whole feature was blocking: until now a ``viewer``
    could only be produced by an ``UPDATE`` in a fixture, which is why the finding
    about what one could spend was classed as latent.
    """
    household = await make_household()
    token, _ = await _invite(api_client, household, role="viewer")
    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201

    assert (await guest.client.get("/v1/inventory")).status_code == 200
    refused = await guest.client.post("/v1/locations", json={"name": "Cave", "kind": "cellar"})
    assert refused.status_code == 403, refused.text


# --------------------------------------------------------------------------- #
# The value never leaks
# --------------------------------------------------------------------------- #


async def test_the_invitation_never_reaches_the_database_or_a_log(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    db_session: AsyncSession,
    guest: Guest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two halves of one property: hashed at rest, and absent from every log record.

    The digest is what the row holds; the plaintext exists in one response body
    and in the invitee's hands. A logger added "just for debugging" in any module
    the request passes through would fail this.
    """
    household = await make_household()
    with caplog.at_level(logging.DEBUG):
        token, invitation_id = await _invite(api_client, household)
        assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201

    stored = await db_session.get(HouseholdInvitation, uuid.UUID(invitation_id))
    assert stored is not None
    assert stored.token_hash == hash_token(token)
    assert token not in stored.token_hash
    assert stored.redeemed_by_user_id == guest.user.id

    emitted = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [str(record.args) for record in caplog.records]
    )
    assert token not in emitted
    # Not merely "the whole value is absent": the secret half is what matters, and
    # a truncated log line would still be a leak.
    assert token[len(INVITATION_PREFIX) :] not in emitted


# --------------------------------------------------------------------------- #
# Membership listing and removal
# --------------------------------------------------------------------------- #


async def test_the_household_can_see_who_holds_access(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    household = await make_household()
    token, _ = await _invite(api_client, household)
    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201

    listed = await api_client.get(_MEMBERS, headers=household_headers(household))
    assert listed.status_code == 200
    rows = {row["user_id"]: row for row in listed.json()}
    assert len(rows) == 2
    assert rows[str(guest.user.id)]["role"] == "member"
    assert rows[str(guest.user.id)]["is_self"] is False
    assert rows[str(guest.user.id)]["email"] == guest.user.email

    # A guest sees the same list: knowing who can read your household's health
    # data is the precondition for noticing somebody who should not be there.
    from_guest = await guest.client.get(_MEMBERS)
    assert from_guest.status_code == 200
    assert {row["user_id"] for row in from_guest.json()} == set(rows)


async def test_an_owner_removes_a_member(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    household = await make_household()
    token, _ = await _invite(api_client, household)
    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201

    removed = await api_client.delete(
        f"{_MEMBERS}/{guest.user.id}", headers=household_headers(household)
    )
    assert removed.status_code == 204, removed.text

    # Nothing else had to be revoked: the household simply stops being theirs.
    assert (await guest.client.get("/v1/auth/session")).json()["households"] == []
    assert (await guest.client.get("/v1/inventory")).status_code == 403


async def test_a_member_leaves_on_their_own(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    household = await make_household()
    token, _ = await _invite(api_client, household)
    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201

    left = await guest.client.delete(f"{_MEMBERS}/{guest.user.id}")
    assert left.status_code == 204, left.text
    assert (await guest.client.get("/v1/auth/session")).json()["households"] == []


async def test_a_member_may_not_remove_somebody_else(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    db_session: AsyncSession,
    guest: Guest,
) -> None:
    household = await make_household()
    token, _ = await _invite(api_client, household)
    assert (await guest.client.post(_REDEEM, json={"token": token})).status_code == 201
    owner = await db_session.scalar(
        select(HouseholdMember.user_id).where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.role == MembershipRole.OWNER,
        )
    )

    refused = await guest.client.delete(f"{_MEMBERS}/{owner}")
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"].endswith("membership-removal-forbidden")


async def test_the_last_owner_may_not_leave(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, db_session: AsyncSession
) -> None:
    """The refusal that keeps an unanswered product question from being answered here.

    What becomes of a household with no members is not decided anywhere in this
    repository, deliberately. So the route refuses to create that state rather than
    picking an answer in a ``DELETE`` handler; ``DELETE /v1/households`` is what
    somebody who wants the household gone actually calls.
    """
    household = await make_household()
    owner = await db_session.scalar(
        select(HouseholdMember.user_id).where(HouseholdMember.household_id == household.id)
    )

    refused = await api_client.delete(f"{_MEMBERS}/{owner}", headers=household_headers(household))
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith("household-would-have-no-owner")


async def test_removing_somebody_who_is_not_a_member_is_a_404(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    household = await make_household()
    response = await api_client.delete(
        f"{_MEMBERS}/{guest.user.id}", headers=household_headers(household)
    )
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #


async def test_an_invitation_opens_exactly_one_household(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, guest: Guest
) -> None:
    """The tenant is on the row, and no header the redeemer sends can widen it."""
    household = await make_household(name="Maison")
    other = await make_household(name="Coloc")
    token, _ = await _invite(api_client, household)

    redeemed = await guest.client.post(
        _REDEEM, json={"token": token}, headers=household_headers(other)
    )
    assert redeemed.status_code == 201
    assert redeemed.json()["household_id"] == str(household.id)

    session = await guest.client.get("/v1/auth/session")
    assert [h["id"] for h in session.json()["households"]] == [str(household.id)]


# --------------------------------------------------------------------------- #
# What cannot reach the redemption route
# --------------------------------------------------------------------------- #


async def test_a_bearer_credential_cannot_redeem(
    api_client: httpx.AsyncClient, make_household: MakeHousehold, api_app: FastAPI
) -> None:
    """Redemption is closed to machine tokens, and closed by construction.

    The route declares no scope, so ``resolve_caller`` refuses every bearer value
    before the handler exists -- and it asks for a :class:`Principal`, which only a
    cookie can produce. A token that could enrol its holder into a household would
    be a token that grants more than the person who issued it.
    """
    household = await make_household()
    token, _ = await _invite(api_client, household)

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            _REDEEM,
            json={"token": token},
            headers={"Authorization": "Bearer chdr_not-a-real-token"},
        )
    assert response.status_code == 401, response.text


async def test_redemption_requires_the_csrf_token(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    api_app: FastAPI,
    guest: Guest,
) -> None:
    """Without it, any page on the internet could enrol a visitor into a household.

    The cookie rides along on a cross-site ``POST`` whatever the page is; the
    header does not. Driven with the guest's own cookie and no header, so the only
    thing missing is the one being tested.
    """
    household = await make_household()
    token, _ = await _invite(api_client, household)

    cookie = guest.client.cookies.get(SESSION_COOKIE)
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver", cookies={SESSION_COOKIE: cookie or ""}
    ) as client:
        response = await client.post(_REDEEM, json={"token": token})
    assert response.status_code == 403, response.text
    assert response.json()["type"].endswith("csrf-token-invalid")


async def test_an_invitation_dies_with_the_authority_that_issued_it(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    db_session: AsyncSession,
    guest: Guest,
) -> None:
    """The issuer must still be an owner when the code is used, not merely when it was made.

    Migration ``0022`` joins back to ``household_member`` with ``role = 'owner'``,
    the same shape as the machine-token resolver. So an owner who is demoted or
    removed cannot leave a working way in behind them, and nobody has to remember
    to go and revoke what they issued.
    """
    household = await make_household()
    token, _ = await _invite(api_client, household)

    owner = await db_session.scalar(
        select(HouseholdMember.user_id).where(HouseholdMember.household_id == household.id)
    )
    await db_session.execute(
        update(HouseholdMember)
        .where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.user_id == owner,
        )
        .values(role=MembershipRole.MEMBER)
    )
    await db_session.flush()

    refused = await guest.client.post(_REDEEM, json={"token": token})
    assert refused.status_code == 403, refused.text
