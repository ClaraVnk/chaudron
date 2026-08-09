"""Who may *sign in* to a household: invitations, and the memberships they create.

Not to be confused with ``services/members.py``, which owns ``household_person``
-- the **eaters**, who have allergens and a diet and no account. This module owns
``household_member``: a :class:`~chaudron.domain.models.UserAccount` with a role.
The two are deliberately different tables and
:class:`~chaudron.domain.models.HouseholdPerson` argues why at length; the short
version is that a six-month-old eats here and will never log in.

Until this module existed, ``HouseholdMember`` was constructed in exactly one
place in the whole repository -- ``services/auth.py``, at registration, as
``owner``. A household was therefore permanently one person, and ``member`` and
``viewer`` were values the schema could hold and nothing could produce. Every
role guard in ``api/deps.py`` was guarding against a state the application had no
way to reach.

Four properties are load-bearing here, and each one is asserted by a test.

**An invitation is a bearer credential, and it is treated as one.** Stored as a
SHA-256 digest, returned in clear exactly once, never logged. This module
installs no logger, for the same reason ``services/members.py`` does not: the
safest place to put a secret is nowhere.

**There is no outbound email, and nothing here waits for one.** The owner is
handed a value and passes it on however they like. An address stored on the row
and never verified would be a control in appearance only, so there is none.
Adding SMTP later changes the *delivery* and leaves this module untouched.

**Redemption is single-use because the database says so.** The conditional
``UPDATE ... WHERE redeemed_at IS NULL`` must return a row before any membership
is written. Two clients racing the same value produce one membership and one
refusal, and no ordering of the two requests changes that.

**Redemption resolves its tenant from the credential, never from a header.** The
redeemer is by definition not yet a member, so ``api/deps.py`` cannot resolve a
household for them: there is nothing to check a header against. The invitation
itself decides, through ``chaudron_resolve_household_invitation`` -- a
``SECURITY DEFINER`` function keyed on the digest, the same shape and the same
argument as ``chaudron_resolve_machine_token`` (migration ``0011``) and
``chaudron_user_memberships`` (migration ``0009``). The tenant is posted *after*
the row is found and from the row, which is the only ordering that is not the
inversion this whole design exists to avoid.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from chaudron.domain.models import (
    Household,
    HouseholdInvitation,
    HouseholdMember,
    MembershipRole,
    UserAccount,
)
from chaudron.infra.db import set_transaction_household
from chaudron.services.auth import TOKEN_BYTES, hash_token

__all__ = [
    "DEFAULT_EXPIRY_DAYS",
    "INVITATION_PREFIX",
    "MAX_EXPIRY_DAYS",
    "AlreadyAMemberError",
    "InvitationExpiryOutOfRangeError",
    "InvitationNotAcceptedError",
    "InvitationRoleNotAllowedError",
    "InvitationService",
    "InvitationSummary",
    "IssuedInvitation",
    "LastOwnerError",
    "MembershipNotFoundError",
    "MembershipRemovalNotPermittedError",
    "MembershipService",
    "MembershipSummary",
    "RedeemedInvitation",
    "TooManyInvitationsError",
]

#: What every invitation starts with. Not a secret and not entropy: it is there
#: so a value found in a chat log, a screenshot or a paste bin is recognisable as
#: a Chaudron credential, which is what makes an accidental leak reportable.
#: Deliberately distinct from ``chdr_`` (machine tokens) so the two cannot be
#: pasted into each other's field and produce a confusing refusal.
INVITATION_PREFIX: Final = "chdi_"

#: How many characters of the tail a listing may show. Four is enough to tell two
#: pending invitations apart and far too few to shorten a search against 256 bits.
LAST4_LENGTH: Final = 4

#: The expiry used when the caller names none. A week is long enough to hand a
#: code to somebody who lives with you and short enough that a forgotten one dies
#: on its own.
DEFAULT_EXPIRY_DAYS: Final = 7

#: The longest expiry a caller may ask for. Thirty days, and deliberately far
#: below ``machine_token``'s ten years: a token is an integration that must keep
#: running, an invitation is a doorbell somebody answers once.
MAX_EXPIRY_DAYS: Final = 30

#: How many live invitations one household may hold at a time. Generous, and it
#: exists so a loop in a misconfigured client cannot fill a table -- not to ration
#: a legitimate use.
MAX_PENDING_PER_HOUSEHOLD: Final = 20

#: The roles an invitation may grant. ``owner`` is absent and the database
#: refuses it too (``ck_household_invitation_role_not_owner``): handing somebody
#: ownership cannot be undone by the person who did it -- two owners may each
#: remove the other -- so it is not a thing a pasted code should be able to do.
#: Promotion to owner is a separate gesture, and one this change deliberately does
#: not invent.
INVITABLE_ROLES: Final = (MembershipRole.MEMBER, MembershipRole.VIEWER)

#: The resolver, through the function that is allowed to see the table. The digest
#: is bound, never interpolated.
_RESOLVE = (
    text("SELECT * FROM chaudron_resolve_household_invitation(:token_hash)")
    .bindparams(bindparam("token_hash", type_=HouseholdInvitation.__table__.c.token_hash.type))
    .columns(
        invitation_id=HouseholdInvitation.__table__.c.id.type,
        household_id=HouseholdInvitation.__table__.c.household_id.type,
        created_by_user_id=HouseholdInvitation.__table__.c.created_by_user_id.type,
    )
)


class MembershipError(Exception):
    """Base class for the failures this module reports to the HTTP layer."""


class InvitationRoleNotAllowedError(MembershipError):
    """An invitation was asked for with a role it may not carry -- today, ``owner``."""


class InvitationExpiryOutOfRangeError(MembershipError):
    """``expires_in_days`` was zero, negative, or past :data:`MAX_EXPIRY_DAYS`."""


class TooManyInvitationsError(MembershipError):
    """The household already holds as many live invitations as it is allowed."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"a household may hold at most {limit} pending invitations")
        self.limit = limit


class InvitationNotAcceptedError(MembershipError):
    """The presented value is not a live invitation.

    Unknown, expired, revoked, already redeemed, belonging to an archived
    household, or issued by an account since disabled: **one branch**, one message,
    one amount of time. Five of the six are decided by the same ``WHERE`` clause in
    ``chaudron_resolve_household_invitation`` and the sixth -- losing the race to
    redeem -- reaches the caller through this same class, so nothing here can tell
    a holder which of them they hit.
    """


class AlreadyAMemberError(MembershipError):
    """The redeemer already belongs to that household.

    Refused **without spending the invitation**, so an owner who handed the code
    to the wrong person can still give it to the right one. It discloses nothing:
    the caller can already read their own membership list from
    ``GET /v1/auth/session``.
    """


class MembershipNotFoundError(MembershipError):
    """No such member of this household."""


class MembershipRemovalNotPermittedError(MembershipError):
    """A member tried to remove somebody other than themselves."""


class LastOwnerError(MembershipError):
    """Removing this membership would leave the household with no owner.

    Refused rather than resolved, and the refusal is the point. What becomes of a
    household whose last member leaves is a **product** question -- is it archived,
    erased, or does it linger ownerless? -- and the GDPR work deliberately left it
    unanswered rather than inventing a policy. Answering it here, in a ``DELETE``
    handler, would be inventing it in the worst possible place. So this route
    refuses the one case that would create the state nobody has decided about, and
    ``DELETE /v1/households`` (article 17) remains the answer for "I want this
    household gone".
    """


@dataclass(frozen=True, slots=True)
class InvitationSummary:
    """One pending invitation as its household may read it back. Carries no secret."""

    id: uuid.UUID
    role: MembershipRole
    prefix: str
    last4: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """A freshly minted invitation. ``token`` exists here and in one response body."""

    summary: InvitationSummary
    token: str


@dataclass(frozen=True, slots=True)
class RedeemedInvitation:
    """What a redemption produced: a membership, and the household it opens."""

    household_id: uuid.UUID
    household_name: str
    role: MembershipRole


@dataclass(frozen=True, slots=True)
class MembershipSummary:
    """One account that may open this household, and with what authority.

    ``email`` is included, and that is a deliberate answer to a data-minimisation
    question rather than an oversight. The people on this list can read every
    eater's allergens and infant age band; a household that cannot see *who* holds
    that access cannot audit it, and an anonymous list of display names is exactly
    how two accounts named "Alex" become one nobody removes.
    """

    user_id: uuid.UUID
    display_name: str
    email: str
    role: MembershipRole
    joined_at: datetime


def new_invitation_token() -> str:
    """A recognisable prefix followed by 256 bits from the operating system."""
    return f"{INVITATION_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


class InvitationService:
    """Issuing, listing, revoking and redeeming household invitations."""

    def __init__(self, session: AsyncSession, *, max_pending: int = MAX_PENDING_PER_HOUSEHOLD):
        self._session = session
        self._max_pending = max_pending

    # -- Issuing ------------------------------------------------------------ #

    async def create(
        self,
        *,
        household_id: uuid.UUID,
        created_by_user_id: uuid.UUID,
        role: MembershipRole,
        expires_in_days: int | None,
    ) -> IssuedInvitation:
        """Mint an invitation for *household_id*, and return it in clear **once**.

        The household is the one the owner's session already resolved and is
        written onto the row. There is no header a holder of the value could send
        to point it at another household -- the same property, for the same reason,
        as ``machine_token`` (``services/tokens.py``).
        """
        if role not in INVITABLE_ROLES:
            raise InvitationRoleNotAllowedError(
                "an invitation may grant "
                + " or ".join(candidate.value for candidate in INVITABLE_ROLES)
                + ", never owner"
            )
        expires_at = self._expiry(expires_in_days)

        held = await self._session.scalar(
            select(func.count()).select_from(HouseholdInvitation).where(*self._live(household_id))
        )
        if held is not None and held >= self._max_pending:
            raise TooManyInvitationsError(self._max_pending)

        value = new_invitation_token()
        record = HouseholdInvitation(
            household_id=household_id,
            created_by_user_id=created_by_user_id,
            role=role,
            token_hash=hash_token(value),
            prefix=INVITATION_PREFIX,
            last4=value[-LAST4_LENGTH:],
            expires_at=expires_at,
        )
        self._session.add(record)
        await self._session.flush()
        # `created_at` is a server default, so the row has to be read back before
        # the summary can carry it -- otherwise the response would quote a
        # timestamp Python invented next to one PostgreSQL chose.
        await self._session.refresh(record, ["created_at"])
        return IssuedInvitation(summary=_summarise(record), token=value)

    @staticmethod
    def _expiry(expires_in_days: int | None) -> datetime:
        """``None`` means :data:`DEFAULT_EXPIRY_DAYS`. There is no "never"."""
        days = DEFAULT_EXPIRY_DAYS if expires_in_days is None else expires_in_days
        if days < 1 or days > MAX_EXPIRY_DAYS:
            raise InvitationExpiryOutOfRangeError(
                f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}"
            )
        return datetime.now(UTC) + timedelta(days=days)

    # -- Listing and revoking ----------------------------------------------- #

    @staticmethod
    def _live(household_id: uuid.UUID) -> tuple[ColumnElement[bool], ...]:
        """What "pending" means, written once so a listing and a count agree."""
        return (
            HouseholdInvitation.household_id == household_id,
            HouseholdInvitation.revoked_at.is_(None),
            HouseholdInvitation.redeemed_at.is_(None),
            HouseholdInvitation.expires_at > func.now(),
        )

    async def list_pending(self, household_id: uuid.UUID) -> tuple[InvitationSummary, ...]:
        """Every invitation still redeemable, newest first.

        Spent, revoked and expired rows are excluded rather than flagged: what an
        owner needs from this list is "who could still walk in", and a list that
        accumulates tombstones is a list nobody reads to the end of.
        """
        rows = await self._session.scalars(
            select(HouseholdInvitation)
            .where(*self._live(household_id))
            .order_by(HouseholdInvitation.created_at.desc(), HouseholdInvitation.id.desc())
        )
        return tuple(_summarise(row) for row in rows)

    async def revoke(self, *, household_id: uuid.UUID, invitation_id: uuid.UUID) -> bool:
        """End one invitation now. Returns whether there was one to end.

        ``household_id`` is in the predicate as well as in the row-level security
        policy. The policy is what enforces it; repeating it here means the
        statement still says what it means when read on its own, and a maintenance
        connection that bypasses the policy cannot revoke across households.
        """
        revoked = await self._session.scalar(
            update(HouseholdInvitation)
            .where(
                HouseholdInvitation.id == invitation_id,
                HouseholdInvitation.household_id == household_id,
                HouseholdInvitation.revoked_at.is_(None),
                HouseholdInvitation.redeemed_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
            .returning(HouseholdInvitation.id)
        )
        return revoked is not None

    # -- Redemption --------------------------------------------------------- #

    async def redeem(self, *, presented: str, user_id: uuid.UUID) -> RedeemedInvitation:
        """Spend an invitation and create the membership it describes.

        The ordering below is the whole design and none of it is interchangeable.

        1. **Resolve past row-level security.** The caller is not a member yet, so
           no tenant can have been posted; ``chaudron_resolve_household_invitation``
           is what may read the row, and it refuses an unknown, expired, revoked,
           already-redeemed, disabled-issuer or archived-household value from one
           ``WHERE`` clause.
        2. **Post the tenant from the row.** Not from anything the client sent.
           Everything after this line runs inside the household the invitation
           named, under the policies of migration ``0004``.
        3. **Refuse an existing member without spending anything.** Consuming the
           value here would burn an owner's invitation because they handed it to
           somebody who already lives there.
        4. **Spend it conditionally.** ``WHERE redeemed_at IS NULL`` returning no
           row means another request won the race, and the answer is the same
           refusal as an unknown value.
        5. **Only then write the membership.** So a failure at any earlier step
           leaves no partial join, and the two writes commit together with the
           caller's transaction.
        """
        row = (await self._session.execute(_RESOLVE, {"token_hash": hash_token(presented)})).first()
        if row is None:
            raise InvitationNotAcceptedError("this invitation cannot be used")

        await set_transaction_household(self._session, row.household_id)

        already = await self._session.scalar(
            select(HouseholdMember.user_id).where(
                HouseholdMember.household_id == row.household_id,
                HouseholdMember.user_id == user_id,
            )
        )
        if already is not None:
            raise AlreadyAMemberError("this account already belongs to that household")

        spent = await self._session.scalar(
            update(HouseholdInvitation)
            .where(
                HouseholdInvitation.id == row.invitation_id,
                HouseholdInvitation.redeemed_at.is_(None),
            )
            .values(redeemed_at=datetime.now(UTC), redeemed_by_user_id=user_id)
            .returning(HouseholdInvitation.id)
        )
        if spent is None:
            # Another request redeemed it between the resolve and here. Reported as
            # the same refusal as an unknown value: which of the two it was is not
            # information the caller has earned, and it changes nothing they can do.
            raise InvitationNotAcceptedError("this invitation cannot be used")

        role = MembershipRole(row.role)
        self._session.add(
            HouseholdMember(
                household_id=row.household_id,
                user_id=user_id,
                role=role,
                invited_by_user_id=row.created_by_user_id,
            )
        )
        await self._session.flush()

        name = await self._session.scalar(
            select(Household.name).where(Household.id == row.household_id)
        )
        return RedeemedInvitation(
            household_id=row.household_id,
            household_name=name or "",
            role=role,
        )


class MembershipService:
    """Reading the household's members, and removing one."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_members(self, household_id: uuid.UUID) -> tuple[MembershipSummary, ...]:
        """Every account that may open this household, oldest membership first.

        Readable by anybody in the household, ``viewer`` included. Knowing who can
        see your household's data is not a privilege: it is the precondition for
        noticing that somebody who should not be there is.
        """
        rows = await self._session.execute(
            select(
                HouseholdMember.user_id,
                HouseholdMember.role,
                HouseholdMember.joined_at,
                UserAccount.display_name,
                UserAccount.email,
            )
            .join(UserAccount, UserAccount.id == HouseholdMember.user_id)
            .where(HouseholdMember.household_id == household_id)
            .order_by(HouseholdMember.joined_at, HouseholdMember.user_id)
        )
        return tuple(
            MembershipSummary(
                user_id=row.user_id,
                display_name=row.display_name,
                email=row.email,
                role=MembershipRole(row.role),
                joined_at=row.joined_at,
            )
            for row in rows
        )

    async def remove(
        self, *, household_id: uuid.UUID, actor_user_id: uuid.UUID, target_user_id: uuid.UUID
    ) -> None:
        """Revoke somebody's access, or leave the household yourself.

        **An owner may remove anybody; anybody else may remove only themselves.**
        The narrower half is what makes "leave" possible without a second endpoint,
        and the wider half is what an owner needs when a flatmate moves out and
        does not think to press the button.

        The actor's role is re-read from ``household_member`` here rather than
        taken from the caller's :class:`~chaudron.services.auth.Principal`. Both
        come from the same table, but only one of them was read inside this
        transaction, and an authorisation decision should be made against the row
        it is about.

        **What this does not do is as important as what it does.** It writes no
        tombstone and it touches no other table -- and it does not have to. The
        removed account's machine tokens stop resolving on their next request,
        because ``chaudron_resolve_machine_token`` joins back to
        ``household_member`` on every call (migration ``0011``); their browser
        sessions stay valid for their *other* households and simply stop listing
        this one. Nothing has to be remembered to be revoked, which is the property
        that makes this safe to expose.
        """
        actor = await self._role_of(household_id, actor_user_id)
        if actor is None:  # pragma: no cover - the role guard resolved this membership
            raise MembershipRemovalNotPermittedError("you do not belong to this household")
        if actor is not MembershipRole.OWNER and actor_user_id != target_user_id:
            raise MembershipRemovalNotPermittedError(
                "only an owner may remove another member of this household"
            )

        target = await self._role_of(household_id, target_user_id)
        if target is None:
            raise MembershipNotFoundError("no member of this household holds that identifier")

        if target is MembershipRole.OWNER and await self._owner_count(household_id) <= 1:
            raise LastOwnerError(
                "this is the household's only owner; removing them would leave it "
                "with nobody who can administer it"
            )

        await self._session.execute(
            delete(HouseholdMember).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.user_id == target_user_id,
            )
        )
        await self._session.flush()

    async def _role_of(self, household_id: uuid.UUID, user_id: uuid.UUID) -> MembershipRole | None:
        role = await self._session.scalar(
            select(HouseholdMember.role).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.user_id == user_id,
            )
        )
        return None if role is None else MembershipRole(role)

    async def _owner_count(self, household_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(HouseholdMember)
            .where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.role == MembershipRole.OWNER,
            )
        )
        return count or 0


def _summarise(record: HouseholdInvitation) -> InvitationSummary:
    return InvitationSummary(
        id=record.id,
        role=MembershipRole(record.role),
        prefix=record.prefix,
        last4=record.last4,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )
