"""Accounts, passwords and server-side sessions.

This module owns every decision about *who* is calling. The HTTP layer
(``api/deps.py``, ``api/routers/auth.py``) owns cookies and status codes and
nothing else; the household a request may touch is decided in ``api/deps.py``
from :class:`Principal`, which is produced here.

Four properties are load-bearing and each is asserted by a test.

**A session is a row, and revoking it is a write.** No JWT, no stateless token.
``docs/security-audit-2026-08.md`` AUD-001 asks for authentication; the reason it
is server-side is in :class:`chaudron.domain.models.UserSession`.

**The stored token is a digest.** :func:`hash_token` runs on the way in and on
every lookup; the plaintext exists in one place, the ``Set-Cookie`` header, and
is never written to the database or to a log.

**Login answers the same way, and in the same time, whatever went wrong.**
:meth:`AuthService.authenticate` returns ``None`` for an unknown address, a wrong
password, a disabled account and an account with no password at all -- and burns
one full Argon2 verification in each case, including the ones where there is
nothing to verify (``infra/passwords.py``). Two of those four used to be
distinguishable by response time in every implementation that returns early.

**Memberships are read through a ``SECURITY DEFINER`` function.**
``household_member`` carries row-level security keyed on the transaction-local
tenant (migration ``0004``), and the tenant is precisely what this code is trying
to establish: reading the table directly would require posting a household before
knowing whether the user may have it, which is the inversion the whole design
exists to avoid. ``chaudron_user_memberships`` (migration ``0009``) runs as the
table owner, is ``STABLE``, takes one argument, and returns nothing but the rows
for that one account.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.domain.models import (
    Household,
    HouseholdMember,
    MembershipRole,
    PasswordResetToken,
    UserAccount,
    UserSession,
)
from chaudron.infra.db import set_transaction_household
from chaudron.infra.passwords import (
    MAX_PASSWORD_BYTES,
    MIN_PASSWORD_LENGTH,
    Passwords,
    PasswordTooLongError,
)

#: Bytes of entropy behind a session cookie and behind a CSRF token. 32 bytes is
#: 256 bits, which puts guessing outside the reach of anything, and encodes to 43
#: URL-safe characters -- short enough to sit in a header without wrapping.
TOKEN_BYTES: Final = 32

#: The longest address the column accepts (``user_account.email`` is
#: ``String(320)``, the RFC 5321 maximum). Checked here so an oversized value is
#: a validation failure rather than a database error.
MAX_EMAIL_LENGTH: Final = 320

#: How stale ``last_used_at`` may get before a read refreshes it. Without this,
#: every authenticated ``GET`` would issue an ``UPDATE``; with it, an idle
#: deadline is still accurate to the minute.
_IDLE_REFRESH_GRANULARITY: Final = timedelta(minutes=1)

#: The membership lookup, through the function that is allowed to see the table.
#: ``:user_id`` is bound, never interpolated.
_MEMBERSHIPS = (
    text(
        """
        SELECT m.household_id, m.role, h.name AS household_name
        FROM chaudron_user_memberships(:user_id) AS m
        JOIN household AS h ON h.id = m.household_id
        WHERE h.archived_at IS NULL
        ORDER BY h.name, m.household_id
        """
    )
    .bindparams(bindparam("user_id", type_=UserAccount.__table__.c.id.type))
    .columns(household_id=UserAccount.__table__.c.id.type)
)


class AuthError(Exception):
    """Base class for the failures this module reports to the HTTP layer."""


class EmailAlreadyRegisteredError(AuthError):
    """The address already has an account.

    **No longer reported to the caller**, and that is the whole of pentest finding
    O-10f. It used to become a ``409``, which made the registration form an
    account enumerator: anybody could ask whether an address had an account here
    and be told. ``api/routers/auth.py`` now catches this and answers exactly what
    it answers for an address that was free -- ``202`` -- while the difference
    travels to the mailbox, which is the only party entitled to it
    (``services/account_email.py``).

    It stays an exception rather than becoming a return value because the caller
    has to distinguish the two paths *internally*: one of them mints a reset link
    for an account that already exists, the other does not.
    """


class WeakPasswordError(AuthError):
    """The candidate is shorter than :data:`MIN_PASSWORD_LENGTH`, or absurdly long."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvalidEmailError(AuthError):
    """The address is empty, too long, or has no ``@``."""


class InvalidCurrentPasswordError(AuthError):
    """The password offered as "the current one" is not it.

    Also raised for an account that has no password at all -- one created by an
    owner rather than by registration. That account cannot set a first password
    *here*, because this endpoint's whole authority is the secret the caller
    already holds and there is none: it recovers through
    :meth:`AuthService.issue_password_reset`, which proves control of the address
    instead. "No password" must never become "any password accepted".
    """


class PasswordResetTokenInvalidError(AuthError):
    """The reset token is unknown, expired, already used, or superseded.

    **One exception for all four**, and the API turns it into one body. Telling a
    caller that their link *existed* and has expired, as against never having
    existed, confirms that a reset was requested for the address they hold -- which
    is the enumeration answer the whole flow is arranged to withhold. The four are
    also a single ``WHERE`` clause here, so no future edit can pull them apart by
    adding a branch.
    """


@dataclass(frozen=True, slots=True)
class Membership:
    """One household this account may open, and with what authority."""

    household_id: uuid.UUID
    household_name: str
    role: MembershipRole

    @property
    def is_owner(self) -> bool:
        return self.role is MembershipRole.OWNER


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller: who, which session, and what they may open.

    Deliberately immutable and free of ORM instances. A handler that receives one
    of these cannot lazily load its way into another household's rows, and the
    object survives the transaction that produced it.
    """

    user_id: uuid.UUID
    email: str
    display_name: str
    session_id: uuid.UUID
    csrf_token: str
    memberships: tuple[Membership, ...]

    def membership(self, household_id: uuid.UUID) -> Membership | None:
        """The membership for *household_id*, or ``None`` if there is none.

        ``None`` is returned identically for "no such household" and "not a
        member of it", which is what keeps the caller from turning the answer
        into an existence probe.
        """
        return next((m for m in self.memberships if m.household_id == household_id), None)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A freshly minted session. ``token`` exists here and in one header, nowhere else."""

    principal: Principal
    token: str
    expires_at: datetime


def new_token() -> str:
    """A URL-safe, 256-bit random string from the operating system."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_reset_token() -> str:
    """A 256-bit random string, in **hexadecimal**, for a password-reset link.

    Hex rather than :func:`new_token`'s URL-safe base64, and the difference is a
    logging property rather than an aesthetic one. ``infra/redaction.py`` blanks
    every unbroken run of 32 or more alphanumeric characters, on length alone;
    ``secrets.token_hex(32)`` is exactly such a run, 64 characters long, so a value
    that ever reached a log line would be blanked by the formatter before the line
    was written. ``secrets.token_urlsafe`` emits ``-`` and ``_``, each of which
    *breaks* that run, and a session cookie printed by accident would survive
    intact -- which is tolerable for a value that lives only in a ``Set-Cookie``
    header and is not for a value that travels in a URL, through a mail server,
    into an inbox, and back through a query string.

    Nothing here logs it. This is the second lock, for the line somebody adds in a
    hurry, and ``tests/api/test_password_reset.py`` asserts on the *rendered* log
    output rather than on the call.

    Entropy is unchanged: 32 bytes from the operating system either way. Hex is
    twice as long on the wire, which costs a URL 21 characters.
    """
    return secrets.token_hex(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The digest stored for *token*. See :class:`UserSession` for why SHA-256."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalise_email(raw: str) -> str:
    """Trim and lower-case, matching ``uq_user_account_email_lower``.

    The index is on ``lower(email)``, so an address that differs only in case is
    the same account; normalising here means the application and the constraint
    agree rather than merely coinciding.
    """
    return raw.strip().lower()


@dataclass(frozen=True, slots=True)
class SessionLifetimes:
    """How long a session lives, absolutely and while idle."""

    absolute: timedelta
    idle: timedelta


@dataclass(frozen=True, slots=True)
class ResetGrant:
    """A freshly minted reset link. ``token`` exists here and in one message.

    Carries the account's *stored* address rather than the one the caller typed:
    the message goes to what the database holds, so a request spelled
    ``Owner@Example.TEST`` cannot redirect a reset to an address of the caller's
    choosing, however the normalisation might change one day.
    """

    address: str
    display_name: str
    token: str
    expires_at: datetime


class AuthService:
    """Registration, login, session resolution and revocation."""

    def __init__(
        self, session: AsyncSession, passwords: Passwords, lifetimes: SessionLifetimes
    ) -> None:
        self._session = session
        self._passwords = passwords
        self._lifetimes = lifetimes

    # -- Registration ------------------------------------------------------ #

    async def register(
        self, *, email: str, password: str, display_name: str, household_name: str
    ) -> UserAccount:
        """Create an account, its first household, and the owner membership.

        The three rows are one unit of work: an account without a household could
        sign in and see nothing, and a household without an owner could never be
        administered. The caller's transaction commits all of it or none of it.

        **It no longer starts a session, and that is the point** (pentest finding
        O-10f). Signing the caller in is only possible on the branch where the
        address was free, so an endpoint that did it would answer one way for a
        new address and another for one that already has an account -- which is
        the enumeration oracle, restated as a status code. Registration therefore
        answers identically either way and the person is asked to sign in, which
        they can do immediately with the password they just chose
        (``api/routers/auth.py``).

        The Argon2 hash below is computed **before** the insert can fail, so both
        branches pay it. That is not incidental: it is what keeps the two paths
        indistinguishable by a stopwatch as well as by a response, exactly as
        :meth:`authenticate` does for sign-in.
        """
        address = self._validated_email(email)
        self._validate_password(password)
        digest = self._passwords.hash(password)

        user = UserAccount(
            email=address,
            password_hash=digest,
            display_name=display_name.strip() or address.split("@")[0],
        )
        try:
            # A savepoint, so a lost race on the unique index leaves the
            # transaction usable instead of poisoning it. The pre-check below is
            # for the message; this is for the correctness.
            #
            # `add` belongs INSIDE the block, and the distinction is not
            # cosmetic: `begin_nested()` autoflushes while taking its snapshot,
            # *before* the SAVEPOINT statement reaches the server. An instance
            # added beforehand is therefore inserted outside the savepoint, the
            # `IntegrityError` aborts the request's own transaction, and the 409
            # this `except` exists to raise can no longer be written -- the
            # request dies at COMMIT instead. Found in the location repository,
            # which documents the same trap; this call site had the unsafe shape
            # and survived only because nothing touched the database after it
            # failed.
            async with self._session.begin_nested():
                self._session.add(user)
                await self._session.flush()
        except IntegrityError as exc:
            raise EmailAlreadyRegisteredError(address) from exc

        household = Household(name=household_name.strip() or "Mon foyer")
        self._session.add(household)
        await self._session.flush()

        # `household_member` is row-level-security protected and its WITH CHECK
        # reads the transaction-local tenant, so the household has to be posted
        # before the row can be inserted. It is the household this request just
        # created, which is as validated as a household gets.
        await set_transaction_household(self._session, household.id)
        self._session.add(
            HouseholdMember(household_id=household.id, user_id=user.id, role=MembershipRole.OWNER)
        )
        await self._session.flush()

        return user

    async def email_is_taken(self, email: str) -> bool:
        """Whether an account already holds *email*, case-insensitively."""
        address = normalise_email(email)
        found = await self._session.scalar(
            select(UserAccount.id).where(UserAccount.email == address).limit(1)
        )
        return found is not None

    # -- Login ------------------------------------------------------------- #

    async def authenticate(self, *, email: str, password: str) -> UserAccount | None:
        """Return the account, or ``None`` -- in constant time and with one answer.

        Every path performs exactly one Argon2 verification. An unknown address
        verifies against a placeholder digest and then fails, so the timing of
        "no such account" is the timing of "wrong password" (``infra/passwords.py``).
        """
        # Before the lookup, not after: an oversized candidate is refused without
        # a database round trip, so the refusal costs the same for an address that
        # exists and one that does not. Doing it inside `verify` alone would still
        # be correct for timing -- the check runs before the digest is chosen --
        # but this saves the query outright, and sign-in is the one endpoint an
        # attacker can reach with no account (`infra/passwords.py`).
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            return None

        address = normalise_email(email)
        user = await self._session.scalar(
            select(UserAccount).where(UserAccount.email == address).limit(1)
        )
        stored = user.password_hash if user is not None else None
        try:
            matched = self._passwords.verify(password, stored)
        except PasswordTooLongError:  # pragma: no cover - the guard above precedes it
            return None

        if not matched or user is None:
            return None
        if user.disabled_at is not None:
            return None

        # A successful login is the only moment a rehash is both safe and free:
        # the plaintext is in hand and the user is already waiting on Argon2.
        if user.password_hash is not None and self._passwords.needs_rehash(user.password_hash):
            user.password_hash = self._passwords.hash(password)
        user.last_login_at = datetime.now(UTC)
        return user

    async def issue_session(self, user: UserAccount) -> IssuedSession:
        """Mint a brand-new session for *user*.

        Always a new row with a new random token, which is what closes session
        fixation: whatever identifier the browser arrived with, the one it leaves
        with was chosen here, after authentication.
        """
        token = new_token()
        now = datetime.now(UTC)
        expires_at = now + self._lifetimes.absolute
        record = UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_token=new_token(),
            expires_at=expires_at,
            idle_expires_at=now + self._lifetimes.idle,
        )
        self._session.add(record)
        await self._session.flush()

        principal = Principal(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            session_id=record.id,
            csrf_token=record.csrf_token,
            memberships=await self.memberships(user.id),
        )
        return IssuedSession(principal=principal, token=token, expires_at=expires_at)

    # -- Changing a password ------------------------------------------------ #

    async def change_password(
        self, *, user_id: uuid.UUID, current_password: str, new_password: str
    ) -> UserAccount:
        """Replace the stored digest, having proved the caller knows the old one.

        **The old password is required and there is no way around it.** This
        instance has no outbound mail, so there is no reset link and no proof of
        control over an address; the only thing that can stand in for "you are
        this person" is the secret they already hold. An endpoint that skipped it
        would turn a stolen cookie -- which is precisely what
        :meth:`revoke_all` exists to answer -- into a permanent takeover.

        The verification runs even when the account has no digest at all, against
        the placeholder (``infra/passwords.py``), so "there is nothing to check"
        costs the same as "that is the wrong password". It is the same reasoning
        as :meth:`authenticate`, and it matters here for a smaller reason: this
        endpoint needs a live session to reach, so the caller already knows the
        account exists. It stays constant-time because the *next* thing somebody
        adds is an unauthenticated variant.

        Returns the account, so the caller can mint the replacement session that
        must follow. Revoking is deliberately *not* done here: which sessions
        survive a password change is an HTTP-layer decision about the cookie in
        this very request, and ``api/routers/auth.py`` is where it is taken.
        """
        self._validate_password(new_password)

        user = await self._session.get(UserAccount, user_id)
        if user is None:  # pragma: no cover - the session resolved from this row
            raise InvalidCurrentPasswordError("no such account")

        if len(current_password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise InvalidCurrentPasswordError("the current password does not match")
        try:
            matched = self._passwords.verify(current_password, user.password_hash)
        except PasswordTooLongError:  # pragma: no cover - the guard above precedes it
            raise InvalidCurrentPasswordError("the current password does not match") from None
        if not matched or user.password_hash is None:
            raise InvalidCurrentPasswordError("the current password does not match")

        user.password_hash = self._passwords.hash(new_password)
        await self._invalidate_reset_tokens(user.id)
        await self._session.flush()
        return user

    # -- Resetting a forgotten password ------------------------------------- #
    #
    # The one unauthenticated path that changes an account, and therefore the one
    # place in this module where every property has to be argued rather than
    # inherited. Four of them are load-bearing and each is asserted by a test in
    # ``tests/api/test_password_reset.py``:
    #
    # *The token is a row, and using it is a write.* Same argument as the session
    # and the machine token: a credential the server merely validates is one it
    # cannot withdraw.
    #
    # *The stored value is a digest.* ``hash_token`` on the way in and on the
    # lookup. A dump of ``password_reset_token`` opens no account.
    #
    # *Single use, and superseded by anything that moves the password.* Following
    # a link consumes it; so does a password change, a later request, and a
    # completed reset. A link sitting in an inbox after the password moved on is a
    # key to a rekeyed door.
    #
    # *Unknown, expired, used and superseded are one branch.* One ``WHERE``, one
    # exception, one body at the API. Telling the four apart would confirm that a
    # reset had been asked for -- the enumeration answer this flow exists to
    # withhold.

    async def issue_password_reset(self, email: str, *, ttl: timedelta) -> ResetGrant | None:
        """Mint a reset link for *email*, or ``None`` if there is nobody to mint it for.

        ``None`` covers "no such address" and "the account is disabled"
        identically. The caller must treat both the same as a success: the HTTP
        layer answers ``202`` in all three cases and the difference reaches the
        mailbox, never the response (``api/routers/auth.py``).

        **Every outstanding link for the account is consumed first.** Asking again
        because the first message has not arrived must not leave two live keys in
        two inboxes; the newest one wins, which is also what a person expects when
        they click the button twice.

        A disabled account gets nothing on purpose. Re-enabling it is an
        operator's decision, and a reset that quietly restored access to an
        account somebody deliberately switched off would route around that
        decision without recording it.
        """
        address = normalise_email(email)
        user = await self._session.scalar(
            select(UserAccount).where(UserAccount.email == address).limit(1)
        )
        if user is None or user.disabled_at is not None:
            return None

        await self._invalidate_reset_tokens(user.id)
        token = new_reset_token()
        expires_at = datetime.now(UTC) + ttl
        self._session.add(
            PasswordResetToken(user_id=user.id, token_hash=hash_token(token), expires_at=expires_at)
        )
        await self._session.flush()
        return ResetGrant(
            address=user.email,
            display_name=user.display_name,
            token=token,
            expires_at=expires_at,
        )

    async def reset_password(self, *, token: str, new_password: str) -> UserAccount:
        """Set a new password from a link, or raise.

        The order of the two checks is deliberate. :meth:`_validate_password` runs
        **first**, before the token is looked at, so that a caller who submits a
        password the policy refuses is told so whether or not their link is any
        good. The other order would answer "password too weak" for a live token
        and "invalid link" for a dead one, which is a probe for token validity
        wearing a helpful message.

        On success: the digest is replaced, **every** session of the account is
        revoked, and every outstanding reset link -- this one included -- is
        consumed. All three in one transaction. A reset that left an intruder's
        session alive would have accomplished nothing, which is the whole reason
        ``revoke_all`` is called here rather than left to the HTTP layer as it is
        for a *voluntary* password change: there, the caller holds a session worth
        keeping; here, the caller holds no session at all and every existing one
        is suspect by construction.
        """
        self._validate_password(new_password)

        now = datetime.now(UTC)
        record = await self._session.scalar(
            select(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == hash_token(token),
                PasswordResetToken.consumed_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .limit(1)
        )
        if record is None:
            raise PasswordResetTokenInvalidError("this reset link is not usable")

        user = await self._session.get(UserAccount, record.user_id)
        if user is None or user.disabled_at is not None:
            # The account went away or was switched off between the message and
            # the click. Same answer as a bad token: the holder of the link learns
            # nothing about which of the two happened.
            raise PasswordResetTokenInvalidError("this reset link is not usable")

        user.password_hash = self._passwords.hash(new_password)
        await self._invalidate_reset_tokens(user.id, now=now)
        await self.revoke_all(user.id)
        await self._session.flush()
        return user

    async def _invalidate_reset_tokens(
        self, user_id: uuid.UUID, *, now: datetime | None = None
    ) -> None:
        """Consume every outstanding link for an account.

        Called from three places -- a new request, a voluntary password change and
        a completed reset -- because all three mean the same thing: whatever links
        are in flight are no longer the current way into this account.
        """
        await self._session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.consumed_at.is_(None),
            )
            .values(consumed_at=now if now is not None else datetime.now(UTC))
        )

    async def issue_session_for(self, user_id: uuid.UUID) -> IssuedSession:
        """A new session for an account the caller has already been proved to be.

        The one-argument form of :meth:`issue_session`, for the routes that hold a
        :class:`Principal` rather than a row -- signing every device out, which has
        to mint the replacement credential after it has revoked the old ones. It
        authenticates nothing on its own and must never be reached from a path
        that has not.
        """
        user = await self._session.get(UserAccount, user_id)
        if user is None:  # pragma: no cover - the live session joined to this row
            raise AuthError("no such account")
        return await self.issue_session(user)

    # -- Resolution -------------------------------------------------------- #

    async def resolve(self, token: str) -> Principal | None:
        """The caller behind a cookie value, or ``None`` if there is none.

        ``None`` covers absent, unknown, expired, idle-expired, revoked, and
        belonging to an account since disabled. The HTTP layer answers ``401``
        for all of them with one body: which of the six it was is information the
        caller has not earned.
        """
        now = datetime.now(UTC)
        row = (
            await self._session.execute(
                select(UserSession, UserAccount)
                .join(UserAccount, UserAccount.id == UserSession.user_id)
                .where(
                    UserSession.token_hash == hash_token(token),
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                    UserSession.idle_expires_at > now,
                    UserAccount.disabled_at.is_(None),
                )
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        record, user = row

        await self._touch(record, now)
        return Principal(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            session_id=record.id,
            csrf_token=record.csrf_token,
            memberships=await self.memberships(user.id),
        )

    async def _touch(self, record: UserSession, now: datetime) -> None:
        """Slide the idle deadline forward, but not on every single request."""
        if now - record.last_used_at < _IDLE_REFRESH_GRANULARITY:
            return
        record.last_used_at = now
        record.idle_expires_at = now + self._lifetimes.idle

    async def memberships(self, user_id: uuid.UUID) -> tuple[Membership, ...]:
        """Every household *user_id* belongs to, read past row-level security."""
        rows = await self._session.execute(_MEMBERSHIPS, {"user_id": user_id})
        return tuple(
            Membership(
                household_id=row.household_id,
                household_name=row.household_name,
                role=MembershipRole(row.role),
            )
            for row in rows
        )

    # -- Revocation -------------------------------------------------------- #

    async def revoke(self, session_id: uuid.UUID) -> None:
        """End one session now. The next request carrying its cookie is anonymous."""
        await self._session.execute(
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_all(self, user_id: uuid.UUID) -> None:
        """End every session of an account. The lever for a suspected compromise."""
        await self._session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    # -- Validation -------------------------------------------------------- #

    def _validated_email(self, raw: str) -> str:
        address = normalise_email(raw)
        if not address or "@" not in address or address.startswith("@") or address.endswith("@"):
            raise InvalidEmailError("an email address is required")
        if len(address) > MAX_EMAIL_LENGTH:
            raise InvalidEmailError(
                f"an email address may not exceed {MAX_EMAIL_LENGTH} characters"
            )
        return address

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise WeakPasswordError(
                f"a password must be at least {MIN_PASSWORD_LENGTH} characters long"
            )
