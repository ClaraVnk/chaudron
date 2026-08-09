"""``DELETE /v1/account`` -- article 17 over the person rather than the household.

The half of pentest item ``O-01`` that stayed open: ``user_account`` held an
address, a display name and a password hash, and nothing in fifty-odd routes could
delete one.

Three things are asserted here that a happy-path test would not.

*The refusal is tested before the erasure.* The whole design of this route is a
refusal -- it declines to decide what becomes of a household whose last owner
leaves -- so the assertions that matter most are the ones proving nothing happened.
An account still there, a household still there, and a ``409`` naming it.

*What is gone is checked in raw SQL, keyed on the identifier*, and not by asking
the ORM whether it still holds an object: it would answer from its identity map,
and the claim under test is precisely that the row is not there. The tables are
named one by one, including the two nothing else in the suite looks at --
``password_reset_token``, which cascades, and ``rate_limit_bucket``, which does
not and is the reason ``forget_bucket_keys`` exists.

*What must survive is checked too.* An erasure that also took the household's
stock, or the other member's account, would pass every "is it gone?" assertion in
this file.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    HouseholdMember,
    MembershipRole,
    PasswordResetToken,
    RateLimitBucket,
    UserAccount,
    UserSession,
)
from chaudron.services.auth import hash_token, new_token
from chaudron.services.privacy import PrivacyService
from tests.conftest import MakeHousehold, MakeMember, MakeUser

pytestmark = pytest.mark.anyio

ERASE_URL = "/v1/account"


async def _count(session: AsyncSession, statement: str, **params: object) -> int:
    return int((await session.execute(sa.text(statement), params)).scalar_one())


async def _accounts(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await _count(session, "SELECT count(*) FROM user_account WHERE id = :id", id=user_id)


async def _memberships(session: AsyncSession, user_id: uuid.UUID) -> int:
    return await _count(
        session, "SELECT count(*) FROM household_member WHERE user_id = :id", id=user_id
    )


async def _add_bucket(session: AsyncSession, scope: str, key: str) -> None:
    session.add(
        RateLimitBucket(scope=scope, bucket_key=key, tokens=1.0, updated_at=datetime.now(UTC))
    )
    await session.flush()


async def _bucket_keys(session: AsyncSession, scope: str) -> set[str]:
    rows = await session.execute(
        sa.text("SELECT bucket_key FROM rate_limit_bucket WHERE scope = :scope"), {"scope": scope}
    )
    return {row.bucket_key for row in rows}


# --------------------------------------------------------------------------- #
# What must not happen
# --------------------------------------------------------------------------- #


async def test_the_erasure_is_refused_while_the_account_is_a_household_s_only_owner(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    signed_in_user: UserAccount,
) -> None:
    """The refusal this route is built around, and the one it must never soften.

    Erasing here would leave a household nobody can configure, invite to or erase
    -- data retained with no one able to reach it, which is the opposite of what an
    article 17 request asks for. ``services/memberships.py`` declines to create
    that state for a single membership; this declines it for all of them at once.
    """
    household = await make_household(name="Maison Seule")

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"].endswith("account-erasure-would-orphan-a-household")
    assert body["households"] == [{"household_id": str(household.id), "name": "Maison Seule"}]
    # And nothing happened.
    assert await _accounts(db_session, signed_in_user.id) == 1
    assert await _memberships(db_session, signed_in_user.id) == 1
    assert (
        await _count(db_session, "SELECT count(*) FROM household WHERE id = :id", id=household.id)
        == 1
    )


async def test_the_refusal_names_every_household_it_would_orphan(
    api_client: httpx.AsyncClient, make_household: MakeHousehold
) -> None:
    """All of them, not the first one found.

    A refusal that named one household at a time would send somebody round the loop
    once per household, learning the size of the job only at the end of it.
    """
    first = await make_household(name="Appartement")
    second = await make_household(name="Chalet")

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 409, response.text
    named = {entry["name"] for entry in response.json()["households"]}
    assert named == {"Appartement", "Chalet"}
    assert {entry["household_id"] for entry in response.json()["households"]} == {
        str(first.id),
        str(second.id),
    }


async def test_a_second_owner_is_enough_to_let_the_erasure_through(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
    signed_in_user: UserAccount,
) -> None:
    """The exact boundary of the rule: *no owner left*, not *this account is an owner*.

    Two owners, one leaves, the household still has somebody who can administer it
    -- so there is nothing to refuse and nothing to decide.
    """
    household = await make_household(name="Colocation")
    other = await make_user(email="colocataire@example.test")
    await make_member(household, other, role=MembershipRole.OWNER)

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    assert await _accounts(db_session, signed_in_user.id) == 0
    assert (
        await _count(db_session, "SELECT count(*) FROM household WHERE id = :id", id=household.id)
        == 1
    ), "the household went with the account"
    assert await _accounts(db_session, other.id) == 1, "the other owner's account went too"
    assert await _memberships(db_session, other.id) == 1


async def test_a_member_who_is_not_an_owner_simply_leaves(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
    signed_in_user: UserAccount,
) -> None:
    """The common case for a tester: invited into somebody's household, then gone."""
    household = await make_household(name="Chez Camille", member=False)
    owner = await make_user(email="camille@example.test")
    await make_member(household, owner, role=MembershipRole.OWNER)
    await make_member(household, signed_in_user, role=MembershipRole.MEMBER)

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    assert await _accounts(db_session, signed_in_user.id) == 0
    assert await _memberships(db_session, signed_in_user.id) == 0
    assert await _memberships(db_session, owner.id) == 1
    body = response.json()
    assert body["memberships_ended"] == [
        {"household_id": str(household.id), "name": "Chez Camille", "role": "member"}
    ]


# --------------------------------------------------------------------------- #
# What goes
# --------------------------------------------------------------------------- #


async def test_the_account_its_sessions_and_its_reset_tokens_all_go(
    api_client: httpx.AsyncClient, db_session: AsyncSession, signed_in_user: UserAccount
) -> None:
    """Every table that holds a credential of this account, checked one by one.

    ``ON DELETE CASCADE`` is a property of three foreign keys, and no test runs in
    production -- so the cascade is asserted rather than assumed. The reset token in
    particular is the one nothing else in the suite checks, and it is the row that
    would let somebody take the address back.
    """
    db_session.add(
        PasswordResetToken(
            user_id=signed_in_user.id,
            token_hash=hash_token(new_token()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    assert await _accounts(db_session, signed_in_user.id) == 0
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM user_session WHERE user_id = :id",
            id=signed_in_user.id,
        )
        == 0
    )
    assert (
        await _count(
            db_session,
            "SELECT count(*) FROM password_reset_token WHERE user_id = :id",
            id=signed_in_user.id,
        )
        == 0
    )


async def test_the_address_is_forgotten_in_the_rate_limit_table(
    api_client: httpx.AsyncClient, db_session: AsyncSession, signed_in_user: UserAccount
) -> None:
    """The one table no cascade reaches, and the reason it had to be named.

    ``rate_limit_bucket`` carries no ``household_id`` by design (revision ``0018``)
    and no foreign key to an account, so an erasure that trusted the cascade would
    leave the person's e-mail address behind -- in a table that, until this change,
    nothing had ever deleted a row from.

    A second account's bucket is seeded in the same scope. An erasure that swept
    the scope, or the table, rather than the key would pass without it.
    """
    scope = f"test-forget-{uuid.uuid4().hex[:8]}"
    stranger = "quelqu-un-dautre@example.test"
    await _add_bucket(db_session, scope, signed_in_user.email)
    await _add_bucket(db_session, scope, stranger)

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    assert await _bucket_keys(db_session, scope) == {stranger}
    assert response.json()["rate_limit_rows_forgotten"] >= 1


async def test_the_buckets_are_matched_on_the_normalised_address(
    db_session: AsyncSession, make_user: MakeUser
) -> None:
    """The case-folding, which is the whole match rather than a nicety.

    ``routers/auth.py`` charges both e-mail-keyed limiters against
    ``normalise_email(...)``, so an account registered as ``Sujet@Example.Test``
    has its buckets under ``sujet@example.test``. A delete keyed on the column
    value alone would find none of them and report a clean erasure over a retained
    address.

    Driven through the service rather than the route: this needs an account whose
    stored address has capitals in it, and the signed-in fixture's does not.
    """
    scope = f"test-fold-{uuid.uuid4().hex[:8]}"
    account = await make_user(email="Sujet@Example.Test")
    await _add_bucket(db_session, scope, "sujet@example.test")
    await _add_bucket(db_session, scope, "Sujet@Example.Test")
    await _add_bucket(db_session, scope, "voisin@example.test")

    receipt = await PrivacyService(db_session).erase_account(account.id)

    assert receipt.rate_limit_rows_forgotten == 2
    assert await _bucket_keys(db_session, scope) == {"voisin@example.test"}


async def test_the_erasure_clears_the_session_cookie(
    api_client: httpx.AsyncClient, signed_in_user: UserAccount
) -> None:
    """The row is already gone; the browser must stop presenting the token anyway.

    Not decoration: a cookie that survives is one the client keeps sending, and
    every request it decorates earns a ``401`` from a session table that no longer
    has the row. The clearing is what makes the answer "you are signed out" rather
    than "something is wrong".
    """
    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    cleared = response.headers.get("set-cookie", "")
    assert "__Host-chaudron_session=" in cleared
    assert "Max-Age=0" in cleared or "expires=Thu, 01 Jan 1970" in cleared.lower()


async def test_the_log_line_carries_counts_and_no_identifier_at_all(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
    signed_in_user: UserAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole audit record, and it names nobody -- unlike the household's.

    ``infra/logging.py`` writes ``household_id`` on every request and never a
    ``user_id``. Recording one here would make the erasure of an account the single
    place in the system where a person's identifier outlives their account, which is
    what migration ``0017`` refused to build a table for.
    """
    household = await make_household(name="Maison Témoin", member=False)
    owner = await make_user(email="proprietaire@example.test")
    await make_member(household, owner, role=MembershipRole.OWNER)
    await make_member(household, signed_in_user, role=MembershipRole.MEMBER)

    with caplog.at_level("INFO", logger="chaudron.services.privacy"):
        response = await api_client.delete(ERASE_URL)
    assert response.status_code == 200, response.text

    [record] = [entry for entry in caplog.records if entry.message == "account_erased"]
    assert record.memberships_ended == 1  # type: ignore[attr-defined]
    assert isinstance(record.rate_limit_rows_forgotten, int)  # type: ignore[attr-defined]
    assert str(signed_in_user.id) not in caplog.text
    assert signed_in_user.email not in caplog.text
    assert signed_in_user.display_name not in caplog.text
    assert "Maison Témoin" not in caplog.text


async def test_the_receipt_says_what_it_did_not_erase(
    api_client: httpx.AsyncClient,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
    signed_in_user: UserAccount,
) -> None:
    """ "Erased" must not be read as more than it is.

    The household survives, and so does the eater record this account may have
    created -- which is health data, and belongs to the household planning meals
    around it rather than to whoever typed it in. Saying so is what keeps the
    receipt honest.
    """
    household = await make_household(member=False)
    owner = await make_user(email="hote@example.test")
    await make_member(household, owner, role=MembershipRole.OWNER)
    await make_member(household, signed_in_user, role=MembershipRole.MEMBER)

    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    assert set(response.json()["not_erased"]) == {
        "household (and everything in it)",
        "household_person",
    }


async def test_an_account_with_no_household_erases_cleanly(
    api_client: httpx.AsyncClient, db_session: AsyncSession, signed_in_user: UserAccount
) -> None:
    """Registration creates an account before it creates anything else.

    A tester who signs up, looks around and asks to be deleted has no household at
    all, and the survey has to answer "nothing to orphan" rather than raise on an
    empty result.
    """
    response = await api_client.delete(ERASE_URL)

    assert response.status_code == 200, response.text
    assert response.json()["memberships_ended"] == []
    assert await _accounts(db_session, signed_in_user.id) == 0


async def test_a_stranger_cannot_erase_an_account(anonymous_client: httpx.AsyncClient) -> None:
    """There is no identifier in the request, so the only way in is a live session."""
    response = await anonymous_client.delete(ERASE_URL)

    assert response.status_code == 401, response.text


async def test_the_erasure_leaves_other_accounts_and_households_alone(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    make_household: MakeHousehold,
    make_user: MakeUser,
    make_member: MakeMember,
    signed_in_user: UserAccount,
) -> None:
    """The assertion no cascade can make for itself."""
    shared = await make_household(name="Partagée", member=False)
    owner = await make_user(email="autre-proprietaire@example.test")
    await make_member(shared, owner, role=MembershipRole.OWNER)
    await make_member(shared, signed_in_user, role=MembershipRole.VIEWER)
    untouched = Household(name="Sans rapport")
    db_session.add(untouched)
    await db_session.flush()
    db_session.add(
        HouseholdMember(household_id=untouched.id, user_id=owner.id, role=MembershipRole.OWNER)
    )
    outsider = await make_user(email="inconnu@example.test")
    db_session.add(
        UserSession(
            user_id=outsider.id,
            token_hash=hash_token(new_token()),
            csrf_token=new_token(),
            expires_at=datetime.now(UTC) + timedelta(days=30),
            idle_expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await db_session.flush()

    response = await api_client.delete(ERASE_URL)
    assert response.status_code == 200, response.text

    assert await _accounts(db_session, owner.id) == 1
    assert await _accounts(db_session, outsider.id) == 1
    assert (
        await _count(
            db_session, "SELECT count(*) FROM user_session WHERE user_id = :id", id=outsider.id
        )
        == 1
    )
    assert await _memberships(db_session, owner.id) == 2
    for household in (shared, untouched):
        assert (
            await _count(
                db_session, "SELECT count(*) FROM household WHERE id = :id", id=household.id
            )
            == 1
        )
