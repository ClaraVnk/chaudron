"""Sign up, sign in, sign out, "who am I?", the levers over a session, and recovery.

Ten routes and one cookie. The cookie is the only credential this API accepts,
and everything that makes it safe is set here in one place, so there is a single
line to read when somebody asks what protects it:

``__Host-`` (browser-enforced: ``Secure``, ``Path=/``, no ``Domain``, so no
sibling subdomain can write it), ``HttpOnly`` (script cannot read it, which is
what keeps an XSS from becoming a session export), ``SameSite=Lax`` (a
cross-site POST does not carry it) and ``Max-Age`` matching the row's absolute
expiry so the browser forgets it at roughly the moment the server does.

**There is no development mode and no way to weaken any of that.** The one that
would be tempting -- dropping ``Secure`` so a plain-HTTP deployment works -- is
absent on purpose: browsers already accept ``Secure`` cookies from ``localhost``
and ``127.0.0.1``, so the local loop does not need it, and a flag that exists is
a flag that eventually ships. ``Settings`` refuses to start a production instance
whose ``base_url`` is not ``https://`` instead (``config.py``).

**What this module used to say it could not do.** For the whole of the
application's life until this change, the paragraph here read: *there is no
outbound email anywhere in Chaudron, so there is no address verification and --
the one that matters -- no password reset. A forgotten password is a forgotten
account.* That was the right call while there was no mail server, and it stopped
being right the moment somebody installed this for a family member: an owner
re-inviting the person is not a recovery path when the owner *is* the person.

``docs/security-model.md`` listed what it would take, and this change is that
list, in its order: SMTP validated at startup (``config.py``, three model
validators, fail-fast like everything else there), single-use short-lived tokens
stored hashed exactly like these ones (``services/auth.py``,
``PasswordResetToken``), rate limiting per address *and* per source on the
request as well as on the consumption (``api/throttling.py``, three new
limiters), and invalidation of **every** session on a completed reset.

**Two endpoints stopped answering the question they were asked.** That is the
part worth reading before the code.

``POST /v1/auth/register`` used to answer ``409 email-already-registered``, which
made the sign-up form an account enumerator: anybody could ask whether an address
had an account here (pentest finding O-10f, accepted at the time only because it
was blocked on SMTP). It now answers ``202`` for both cases -- and, because a
session can only be started on one of the two branches, it no longer starts one
at all. The difference travels to the mailbox instead: "here is your account"
against "somebody tried to register your address; you already have one, here is a
reset link". The mailbox is the only party entitled to that answer.

``POST /v1/auth/password/reset-request`` is the same discipline applied to a
route written after it: identical ``202``, identical body, whether or not the
address has an account, and a message either way -- including one saying *there is
no account here*, so that "nothing arrived" is never the answer somebody is left
with.

**The mail is never sent from the handler.** Both routes hand the message to a
Starlette background task, so the response has already gone before a socket is
opened. That is not a latency optimisation: an SMTP dial-out inside the handler
makes "does this address exist?" answerable with a stopwatch, and would hand back
the oracle the ``202`` was arranged to close.

**And the capability may be absent.** This is self-hosted software; plenty of
installs have no relay. ``GET /v1/auth/capabilities`` says so to an unauthenticated
caller, so the interface can leave out a link that would lead to an apology, and
the two request routes answer ``503`` rather than accepting a request they cannot
honour. Silently doing nothing is the one behaviour that is not on offer.

**What it can now do, and could not before.** ``POST /v1/auth/sessions/revoke-all``
and ``POST /v1/auth/password`` are the two things a person who suspects their
cookie has leaked needs, and until this change neither existed: the service method
behind the first had no caller anywhere in the repository, and there was no way to
change a password at all (audit AUD-028). The only bound on a stolen session was
the 30-day absolute expiry, and the only remedy was an operator with a psql
prompt. Both routes revoke **every** session including the one making the request
-- a copied cookie is a copy of *this* row, so sparing it spares the thief -- and
both mint a replacement before answering, so the remedy does not sign out the
person applying it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Final, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from chaudron.api.deps import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthServiceDep,
    PrincipalDep,
    ThrottlesDep,
    get_settings_dep,
)
from chaudron.api.errors import (
    ProblemError,
    invalid_credentials,
    rate_limited,
)
from chaudron.api.throttling import AtCapacityError, Throttles
from chaudron.config import Settings
from chaudron.domain.email_ports import (
    InvalidRecipientError,
    Mailer,
    MailerError,
    OutboundMessage,
    validate_recipient,
)
from chaudron.domain.models import UserAccount
from chaudron.infra.passwords import MIN_PASSWORD_LENGTH
from chaudron.services import account_email
from chaudron.services.auth import (
    AuthService,
    EmailAlreadyRegisteredError,
    InvalidCurrentPasswordError,
    InvalidEmailError,
    IssuedSession,
    PasswordResetTokenInvalidError,
    Principal,
    WeakPasswordError,
    normalise_email,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

#: Cookie lifetime is quoted to the browser in seconds. Kept equal to the row's
#: absolute expiry: a browser holding a cookie the server has already forgotten
#: produces a 401 the user cannot explain, which is worse than a login screen.
_SECONDS_PER_HOUR: Final = 3600


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class RegisterIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)
    display_name: str = Field(default="", max_length=120)
    household_name: str = Field(default="", max_length=120)


class ChangePasswordIn(BaseModel):
    """The old password and the new one. Both :class:`~pydantic.SecretStr`.

    So that a model dumped into a log line, a traceback frame or a debugger prints
    a mask rather than the credential. ``api/errors.py`` already strips pydantic's
    ``input`` echo from a 422; this is the second lock, and the same one
    ``routers/export_targets.py`` puts on a third party's token.

    ``min_length`` is declared on the new password and **not** on the current one,
    for the reason ``LoginIn`` gives: a short value offered as the current password
    is a *wrong* password, and a 422 naming the policy would answer a question the
    endpoint refuses to. ``max_length`` stays on both, because that one is a
    denial-of-service bound (``infra/passwords.py``).
    """

    current_password: SecretStr = Field(max_length=1024)
    new_password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class RegistrationAcceptedOut(BaseModel):
    """What registration answers now, and it is the same object either way.

    No session, no cookie, no account identifier -- nothing that could differ
    between "the address was free" and "the address already has an account". The
    two fields are constants of the *instance*, not facts about the address: a
    caller comparing two responses byte for byte sees one response.

    ``email_available`` says whether this instance can send mail at all. It is
    read from the configuration and is identical for every request, so it
    discloses nothing about the submitted address -- but the interface needs it,
    because what it tells a person next depends on whether a message is on its
    way (``frontend/src/features/auth/AuthScreen.tsx``). It is deliberately **not**
    "a message was sent to you": that would be a per-address fact, and on the
    branch where the recipient's hourly budget is exhausted it would also be a
    lie.
    """

    status: Literal["accepted"] = "accepted"
    email_available: bool


class AuthCapabilitiesOut(BaseModel):
    """What this instance can do for somebody who is not signed in.

    One field today, and the pattern it follows is ``GET /v1/providers/capabilities``:
    the interface asks what exists rather than discovering it from a failure. The
    alternative -- rendering "forgot my password" unconditionally and letting the
    ``503`` explain -- puts the apology *after* the click, which is the arrangement
    the sign-in screen's old copy already rejected for the same reason.

    Unauthenticated on purpose, and it discloses nothing worth withholding:
    whether an operator configured a mail relay is a property of the deployment,
    visible to anyone who submits the form once, and useless to an attacker who
    cannot read anybody's mailbox.
    """

    password_reset: bool


class PasswordResetRequestIn(BaseModel):
    """An address, and nothing else. No ``Referer``, no "I am not a robot"."""

    email: str = Field(max_length=320)


class PasswordResetAcceptedOut(BaseModel):
    """The one answer a reset request has. Constant, by construction.

    A model with no field that could vary, rather than a bare ``202`` with an
    empty body, so that the shape is *declared*: the next person to touch this
    route has to add a field on purpose, and adding one is the mistake that
    reopens the oracle.
    """

    status: Literal["accepted"] = "accepted"


class PasswordResetIn(BaseModel):
    """The token from the link, and the password to set.

    Both :class:`~pydantic.SecretStr`, for the reason :class:`ChangePasswordIn`
    gives -- a model dumped into a traceback or a debugger prints a mask. It
    matters more here: the token is a *bearer credential for an account*, and
    unlike the password it arrived by mail and may still be live.

    ``min_length`` on the new password and none on the token, deliberately. A
    short password is a policy failure the caller can act on; a short token is a
    wrong token, and a ``422`` naming its expected length would tell a guesser how
    long to make theirs.
    """

    token: SecretStr = Field(max_length=512)
    new_password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=1024)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    # No `min_length` here, deliberately: a short password is a *wrong* password
    # on this endpoint, and a 422 naming the rule would tell a caller that the
    # policy exists without telling them anything useful. `max_length` stays,
    # because that one is a denial-of-service bound (`infra/passwords.py`).
    password: str = Field(max_length=1024)


class HouseholdOut(BaseModel):
    id: str
    name: str
    role: str


class SessionOut(BaseModel):
    """Everything the interface needs to render a signed-in state.

    ``csrf_token`` is here because it has to reach the client somehow, and a
    response body is the one channel a cross-origin attacker cannot read: the
    browser refuses to hand them the body of a CORS request their origin is not
    allowed to make. Putting it in a second cookie instead would work only while
    the interface and the API share a host, which is not a property worth
    depending on.
    """

    user_id: str
    email: str
    display_name: str
    csrf_token: str
    households: list[HouseholdOut]


def _session_out(principal: Principal) -> SessionOut:
    return SessionOut(
        user_id=str(principal.user_id),
        email=principal.email,
        display_name=principal.display_name,
        csrf_token=principal.csrf_token,
        households=[
            HouseholdOut(id=str(m.household_id), name=m.household_name, role=str(m.role))
            for m in principal.memberships
        ],
    )


# --------------------------------------------------------------------------- #
# The cookie
# --------------------------------------------------------------------------- #


def _set_session_cookie(response: Response, issued: IssuedSession, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=settings.session_absolute_ttl_hours * _SECONDS_PER_HOUR,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        # No `domain`: `__Host-` forbids it, and that prohibition is the whole
        # value of the prefix.
    )


def _clear_session_cookie(response: Response) -> None:
    # Same attributes as when it was set, or the browser deletes nothing: a
    # cookie is identified by name, domain and path together.
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")


# --------------------------------------------------------------------------- #
# Outbound mail
# --------------------------------------------------------------------------- #


def get_mailer(request: Request) -> Mailer | None:
    """The instance's mailer, or ``None`` when it has no relay configured.

    Read off ``app.state`` rather than declared in ``api/deps.py``, and the reason
    is scope: this is the only router that sends mail, so the wiring lives beside
    the routes that use it. ``getattr`` with a default because an application
    built by an older factory -- or by a test that assembles its own -- must not
    fail with ``AttributeError`` on a capability that is optional by design.

    Tests replace it by assigning ``app.state.mailer``; there is no dependency
    override to remember, and the double lives in ``infra/email/doubles.py``.
    """
    mailer: Mailer | None = getattr(request.app.state, "mailer", None)
    return mailer


MailerDep = Annotated[Mailer | None, Depends(get_mailer)]


async def _deliver(mailer: Mailer, message: OutboundMessage, kind: str) -> None:
    """Send one message, after the response has left, and never fail a request.

    Run as a Starlette background task. Two properties are the point.

    **It cannot raise into a response**, because there is no response left to
    raise into -- the client already has it. So a relay that is down changes
    nothing an unauthenticated caller can observe, which is precisely what stops
    "is the mail server slow for this address?" from becoming the oracle the
    ``202`` closed.

    **It logs the failure, and logs nothing else.** ``docs/security-model.md``
    names a silent send failure as the thing that turns "I did not receive the
    message" into an incident nobody can close, so a failure is a ``WARNING`` an
    operator can find. What that line carries is the message *kind* and the
    exception *type*: no address, because a recipient in a log is personal data
    retained past the request that produced it, and above all no token. The
    formatter would blank a 64-hex token anyway (``infra/redaction.py``,
    ``services/auth.py`` on why the token is hex) -- this is the first lock, not
    the second.
    """
    try:
        await mailer.send(message)
    except MailerError as exc:
        logger.warning("account_email_not_sent", extra={"kind": kind, "error": type(exc).__name__})


async def _may_email(throttles: Throttles, address: str) -> bool:
    """Whether this instance will put another message in *address*'s inbox this hour.

    The limiter that protects a **third party** rather than this instance
    (``api/throttling.py``). Without it the reset form is a mail bomb aimed at any
    address an attacker knows, sent from a relay the victim trusts.

    Refusal is not an error and never reaches the caller: the route answers its
    invariant ``202`` and simply sends nothing. Reporting it would be a per-address
    fact in a response whose whole design is to carry none.

    Charged **before** the branch that decides which message to send, and on the
    branch that sends none, so the bucket's own state cannot be read back as "this
    address has an account".
    """
    try:
        await throttles.account_emails.acquire(address)
    except AtCapacityError:
        logger.info("account_email_budget_exhausted")
        return False
    return True


def _client_key(request: Request) -> str:
    """Best available identity for a caller who has not signed in yet.

    Behind a reverse proxy this is the proxy, which collapses every caller into
    one bucket -- the same limitation ``routers/calendar.py`` documents. It still
    bounds a single misbehaving client on a direct deployment, and the per-account
    limiter below covers the case this one cannot see.
    """
    return request.client.host if request.client is not None else "unknown"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post(
    "/register",
    response_model=RegistrationAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for an account, and be told nothing about whether the address had one",
)
async def register(
    request: Request,
    payload: RegisterIn,
    auth: AuthServiceDep,
    throttles: ThrottlesDep,
    background: BackgroundTasks,
    mailer: MailerDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> RegistrationAcceptedOut:
    """Create the account and the household it owns -- or, silently, neither.

    **``202`` for both, and the same body for both.** This route used to answer
    ``201`` with a session for a free address and ``409 email-already-registered``
    for a taken one, which is an account enumerator anybody could run from a
    browser console (pentest finding O-10f). The disclosure now goes to the
    mailbox: one of two messages, differing in exactly the way the response no
    longer does (``services/account_email.py``).

    **It no longer signs the caller in, and that follows rather than being a
    separate decision.** A session can only be minted on the branch where the
    address was free; an endpoint that returned one would be announcing which
    branch it took. So registration ends with "now sign in", which costs one form
    submission with the password just chosen and is what every other application
    that closed this hole does.

    **Both branches pay one Argon2 hash**, because ``AuthService.register``
    computes the digest before the insert that may fail (``services/auth.py``).
    That is what keeps the two indistinguishable by a stopwatch as well as by a
    body -- and it is the same argument, in the same words, that
    ``AuthService.authenticate`` makes for sign-in.

    **What still differs, honestly.** The free branch writes four rows and the
    taken branch writes one; on a local socket that is tens of microseconds inside
    a request that has already spent tens of *milli*seconds in Argon2, and the
    per-address budget below caps a caller at three samples an hour for any one
    address. It is not zero, and it is written down here rather than claimed away.

    ``422`` is still possible, for a malformed address or a password under the
    policy. Neither is a fact about the address's account: both are decided from
    the submitted bytes alone, before anything is looked up.
    """
    try:
        await throttles.registrations.acquire(_client_key(request))
    except AtCapacityError as exc:
        raise rate_limited(
            detail="Too many accounts have been created from this address. Try again later.",
            retry_after=exc.retry_after,
        ) from None

    address = normalise_email(payload.email)
    already_registered = False
    created = None
    try:
        created = await auth.register(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            household_name=payload.household_name,
        )
    except EmailAlreadyRegisteredError:
        # Not an error the caller hears about. The savepoint in
        # ``AuthService.register`` is what leaves the transaction usable here, so
        # the reset link below can still be written.
        already_registered = True
    except WeakPasswordError as exc:
        raise ProblemError(
            slug="password-too-weak",
            title="Password too weak",
            status=422,
            detail=exc.detail,
        ) from None
    except InvalidEmailError as exc:
        raise ProblemError(
            slug="invalid-email", title="Invalid email address", status=422, detail=str(exc)
        ) from None

    if mailer is not None and await _may_email(throttles, address):
        message = await _registration_message(
            auth, settings, address=address, already_registered=already_registered, created=created
        )
        if message is not None:
            background.add_task(_deliver, mailer, message, "registration")

    return RegistrationAcceptedOut(email_available=mailer is not None)


async def _registration_message(
    auth: AuthService,
    settings: Settings,
    *,
    address: str,
    already_registered: bool,
    created: UserAccount | None,
) -> OutboundMessage | None:
    """Which of the two messages this registration attempt earns, if either.

    Split out of the handler so that the *only* place the two branches diverge is
    a function returning a message, rather than two paragraphs of a route that
    also builds a response. A future edit that adds a third outcome has to add it
    here, where the invariant is one line above it.

    ``None`` is returned when the address belongs to a **disabled** account:
    ``issue_password_reset`` refuses those (``services/auth.py``), and a message
    offering a reset link that will not work is worse than silence. Unobservable
    from outside, like everything else on this path.
    """
    if not already_registered and created is not None:
        return account_email.account_created(
            to=created.email, display_name=created.display_name, app_url=settings.app_url
        )

    grant = await auth.issue_password_reset(
        address, ttl=timedelta(minutes=settings.password_reset_ttl_minutes)
    )
    if grant is None:
        return None
    return account_email.address_already_registered(
        to=grant.address,
        app_url=settings.app_url,
        token=grant.token,
        ttl_minutes=settings.password_reset_ttl_minutes,
    )


@router.post("/login", response_model=SessionOut, summary="Start a session")
async def login(
    request: Request,
    response: Response,
    payload: LoginIn,
    auth: AuthServiceDep,
    throttles: ThrottlesDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """Verify the credentials and mint a **new** session.

    "New" is the anti-fixation property: whatever session identifier the browser
    arrived holding, the one it leaves with was generated here, after the
    password was checked (``AuthService.issue_session``). Any session the request
    presented is revoked on the way through, so a credential planted in the
    victim's browser before sign-in is dead rather than merely superseded.

    Two limiters, because neither sees what the other does. Per source address
    bounds one host spraying many accounts; per address bounds a botnet guessing
    at one account. The per-address counter is incremented for addresses that do
    not exist too -- otherwise its own behaviour would answer the question the
    endpoint refuses to.

    There is no CSRF token to check here (there is no session yet), and none is
    needed: the body is JSON, and a cross-site HTML form cannot send
    ``application/json``. A form-encoded body reaches the validation layer and is
    refused with ``422`` before any credential is read.
    """
    address = normalise_email(payload.email)
    for key, limiter in (
        (_client_key(request), throttles.login_attempts_by_ip),
        (address, throttles.login_attempts_by_account),
    ):
        try:
            await limiter.acquire(key)
        except AtCapacityError as exc:
            raise rate_limited(
                detail="Too many sign-in attempts. Wait before trying again.",
                retry_after=exc.retry_after,
            ) from None

    user = await auth.authenticate(email=payload.email, password=payload.password)
    if user is None:
        raise invalid_credentials()

    await _revoke_presented_session(request, auth)
    issued = await auth.issue_session(user)
    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


async def _revoke_presented_session(request: Request, auth: AuthService) -> None:
    """Kill whatever session the browser arrived with, before minting the next one."""
    existing = request.cookies.get(SESSION_COOKIE)
    if not existing:
        return
    principal = await auth.resolve(existing)
    if principal is not None:
        await auth.revoke(principal.session_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def logout(response: Response, principal: PrincipalDep, auth: AuthServiceDep) -> None:
    """Revoke the session server-side, then clear the cookie.

    In that order, and both halves matter. Clearing the cookie alone would leave
    a live row that anyone holding a copy of the token could keep using; revoking
    alone would leave the browser sending a dead credential on every request.

    Behind ``PrincipalDep``, so it also requires a valid ``X-CSRF-Token``: without
    that, any page on the internet could sign a user out of Chaudron.
    """
    await auth.revoke(principal.session_id)
    _clear_session_cookie(response)


@router.post(
    "/sessions/revoke-all",
    response_model=SessionOut,
    summary="End every session of this account, and start a fresh one here",
)
async def revoke_all_sessions(
    response: Response,
    principal: PrincipalDep,
    auth: AuthServiceDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """ "Sign me out everywhere." The lever for a cookie somebody thinks has leaked.

    ``AuthService.revoke_all`` has existed since sessions did, described as *the
    lever for a suspected compromise*, and was reachable from nothing: no route,
    no script, no test (audit AUD-028). A user who believed their cookie had been
    copied had **nothing** -- the only bound was the 30-day absolute expiry, and
    the only cure was an operator with a psql prompt.

    **Every row goes, this request's included, and a new one is minted before the
    response leaves.** That combination is the whole design and neither half is
    negotiable.

    Sparing the current row would be the obvious reading of "keep me signed in",
    and it is precisely wrong for the situation that brings someone here: a stolen
    cookie is not *another* session, it is a **copy of this one**. Sparing the row
    would spare the thief. So the row dies, and the browser that asked is handed a
    freshly generated credential in the same response -- which is session rotation,
    the same gesture ``POST /v1/auth/login`` performs, for the same reason.

    Behind :data:`PrincipalDep`, so a valid ``X-CSRF-Token`` is required and a
    machine token cannot reach it. Without the first, any page on the internet
    could sign a Chaudron user out of every device they own.

    What this does **not** touch: machine tokens. They are a separate credential
    with their own list and their own revocation (``routers/tokens.py``), and
    silently killing a household's integrations because one person's laptop was
    stolen would be a surprise nobody asked for. The interface says so next to the
    button.
    """
    await auth.revoke_all(principal.user_id)
    issued = await auth.issue_session_for(principal.user_id)
    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


@router.post(
    "/password",
    response_model=SessionOut,
    summary="Change this account's password, ending every other session",
)
async def change_password(
    response: Response,
    payload: ChangePasswordIn,
    principal: PrincipalDep,
    auth: AuthServiceDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionOut:
    """Replace the password, having proved the old one, and rotate the session.

    **The current password is required**, and it stayed required when the reset
    flow landed. The two endpoints are not alternatives: this one is reached with
    a live session and proves nothing about the person holding it, so skipping the
    check would turn a stolen cookie into a permanent takeover -- the exact failure
    the route above it exists to answer. ``POST /v1/auth/password/reset`` is the
    path for somebody who cannot supply the old password, and it demands a
    different proof entirely: control of the address, shown by consuming a
    single-use link that never travelled through the browser session.

    **Changing a password ends every session.** That is what makes it useful as a
    remedy rather than merely as hygiene: a password change that left the other
    browsers signed in would leave the intruder signed in. And, as above, the
    caller does not get logged out by their own remedy -- the response carries a
    new cookie and a new CSRF token, minted after the revocation.

    Not rate limited by a counter of its own, deliberately. Reaching it costs a
    live session **and** a matching CSRF token, so it is not a door a stranger can
    knock on; and each call pays one Argon2 verification, which is the same cost
    ``POST /v1/auth/login`` pays and is itself the bound. A limiter keyed on the
    account would additionally let anyone holding the cookie lock the owner out of
    signing back in, which is a worse trade.
    """
    try:
        user = await auth.change_password(
            user_id=principal.user_id,
            current_password=payload.current_password.get_secret_value(),
            new_password=payload.new_password.get_secret_value(),
        )
    except InvalidCurrentPasswordError:
        raise ProblemError(
            slug="current-password-invalid",
            title="Current password does not match",
            status=403,
            detail=(
                "The current password sent with this request does not match this "
                "account. The password was not changed."
            ),
        ) from None
    except WeakPasswordError as exc:
        raise ProblemError(
            slug="password-too-weak",
            title="Password too weak",
            status=422,
            detail=exc.detail,
        ) from None

    await auth.revoke_all(user.id)
    issued = await auth.issue_session(user)
    _set_session_cookie(response, issued, settings)
    return _session_out(issued.principal)


# --------------------------------------------------------------------------- #
# Recovering a forgotten password
# --------------------------------------------------------------------------- #
#
# The only unauthenticated path that changes an account, which is why every line
# of it is argued. The three routes below are, in order: what this instance can
# do, ask for a link, and use one.


@router.get(
    "/capabilities",
    response_model=AuthCapabilitiesOut,
    summary="What this instance can do for a caller who is not signed in",
)
async def auth_capabilities(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    mailer: MailerDep,
) -> AuthCapabilitiesOut:
    """Whether a password can be reset here at all.

    Both conditions, not one: the configuration must name a relay *and* the
    factory must have built a mailer from it. They agree today and asking for both
    means they cannot disagree tomorrow -- an application assembled by a test, or
    by a future factory that builds the mailer conditionally, answers what it can
    actually do rather than what its settings say.
    """
    return AuthCapabilitiesOut(password_reset=settings.email_enabled and mailer is not None)


def _mail_not_configured() -> ProblemError:
    """``503``: this instance has no relay, so it cannot start a recovery.

    Named, not silent. A route that accepted the request and did nothing would
    leave a person refreshing an inbox for a message that was never going to be
    written -- the failure ``docs/security-model.md`` singles out, and the reason
    an SMTP configuration has to be validated at startup rather than discovered at
    the first send.

    ``503`` rather than ``404``: the endpoint exists, and the thing that is
    missing is a server-side capability an operator can add. Same answer for every
    address, so it discloses nothing about any of them.
    """
    return ProblemError(
        slug="email-not-configured",
        title="This instance cannot send email",
        status=503,
        detail=(
            "No mail server is configured on this Chaudron instance, so a password "
            "cannot be reset by email. An owner of your household can restore your "
            "access, or the operator can configure CHAUDRON_SMTP_HOST."
        ),
    )


def _reset_token_invalid() -> ProblemError:
    """``400``: unknown, expired, already used, or superseded.

    One body for all four, and they are one branch in ``services/auth.py`` rather
    than four cases collapsed here -- so no future edit can pull them apart by
    adding an ``except``. Telling a caller that their link *had existed* confirms
    that a reset was requested for the address they hold, which is the enumeration
    answer the whole flow is arranged to withhold.
    """
    return ProblemError(
        slug="password-reset-token-invalid",
        title="Reset link not usable",
        status=400,
        detail=(
            "This password reset link is not usable. It may have expired, already "
            "been used, or been replaced by a more recent request. Ask for a new one."
        ),
    )


def _validated_address(raw: str) -> str:
    """Normalise, and refuse what could never be an address or a header.

    Format only. It decides nothing about whether an account exists, so a ``422``
    here is not an oracle -- and it has to happen before the address reaches a
    message, because that is where a bare CRLF would become headers of somebody
    else's choosing (``domain/email_ports.py``).
    """
    try:
        return normalise_email(validate_recipient(raw))
    except InvalidRecipientError as exc:
        raise ProblemError(
            slug="invalid-email", title="Invalid email address", status=422, detail=str(exc)
        ) from None


@router.post(
    "/password/reset-request",
    response_model=PasswordResetAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a password reset link, and be told nothing else",
)
async def request_password_reset(
    request: Request,
    payload: PasswordResetRequestIn,
    auth: AuthServiceDep,
    throttles: ThrottlesDep,
    background: BackgroundTasks,
    mailer: MailerDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> PasswordResetAcceptedOut:
    """Send a link to the address, if there is an account behind it. Answer the same either way.

    **The response cannot vary.** ``202`` with a body that has one field and one
    possible value (:class:`PasswordResetAcceptedOut`). Anything else -- a
    ``404``, a different message, a field saying whether a message went -- would
    move the enumeration oracle from ``/register`` to here rather than closing it,
    and this route would have been built as the replacement for the very thing it
    reintroduced.

    **A message goes either way too.** An address with no account receives one
    saying so (``services/account_email.py``), because otherwise a person who
    mistyped their address and a person whose relay is broken are left with the
    same evidence -- nothing -- and only one of the two has anything to fix.

    **No mail is sent from this handler.** The message is handed to a background
    task and the response leaves first. An SMTP dial-out here would take a second
    or two on the branch that has something to send and nothing on the branch that
    does not, which is the whole oracle again, measured with a stopwatch instead of
    read from a body.

    **Three limiters, guarding three different parties.** Per source address, so
    one host cannot walk a list; per *recipient* address, so this form cannot be
    used to fill somebody else's inbox from a server they trust -- charged before
    the branch, so its own state says nothing either; and, on the sibling route
    below, per attempt.

    ``503`` when the instance has no relay, and that is the one answer that is
    *not* uniform with the rest -- deliberately. It is a fact about the deployment,
    identical for every address, and the alternative is to accept a request that
    will never produce anything.
    """
    if mailer is None:
        raise _mail_not_configured()

    address = _validated_address(payload.email)
    try:
        await throttles.password_reset_requests.acquire(_client_key(request))
    except AtCapacityError as exc:
        raise rate_limited(
            detail="Too many password reset requests from this address. Try again later.",
            retry_after=exc.retry_after,
        ) from None

    if await _may_email(throttles, address):
        message = await _reset_request_message(auth, settings, address=address)
        background.add_task(_deliver, mailer, message, "password_reset")

    return PasswordResetAcceptedOut()


async def _reset_request_message(
    auth: AuthService, settings: Settings, *, address: str
) -> OutboundMessage:
    """The link, or the message saying there is nothing to link to.

    The single place the two branches of a reset request diverge, for the reason
    :func:`_registration_message` gives: one function, returning a message, next to
    the invariant it must not break.
    """
    grant = await auth.issue_password_reset(
        address, ttl=timedelta(minutes=settings.password_reset_ttl_minutes)
    )
    if grant is None:
        return account_email.password_reset_no_account(to=address, app_url=settings.app_url)
    return account_email.password_reset(
        to=grant.address,
        app_url=settings.app_url,
        token=grant.token,
        ttl_minutes=settings.password_reset_ttl_minutes,
    )


@router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set a new password from a reset link, ending every session",
)
async def complete_password_reset(
    request: Request,
    payload: PasswordResetIn,
    auth: AuthServiceDep,
    throttles: ThrottlesDep,
) -> None:
    """Consume the link, replace the password, and revoke **every** session.

    **``204``, not a session.** Following a link from an inbox proves control of a
    mailbox, and this application's answer to that proof is "you may set a
    password", not "you are signed in". Minting a session here would make the
    message itself a login link: anything that can read the mailbox -- a shared
    family account, a phone left unlocked, a backup of a mail client -- would
    become a way in without the password ever being known. So the person signs in
    afterwards, with the password they just chose, through the endpoint that has
    the rate limiters and the constant-time comparison on it.

    **Every session goes, including any the attacker holds.** Done in the service
    inside the same transaction as the password change (``services/auth.py``), not
    here: a reset that left an intruder's cookie alive would have accomplished
    nothing, and the two facts must not be able to disagree. This differs from
    ``POST /v1/auth/password``, where the revocation *is* an HTTP-layer decision
    because the caller holds a session worth replacing. Here there is none.

    **Every outstanding link goes too**, this one included. Single use is not a
    property of the row alone: a second link, minted before this one was followed,
    would still be sitting in the same inbox.

    **Refusals.** ``422`` for a password under the policy, decided *before* the
    token is looked at so that the answer does not depend on whether the link was
    good; ``400`` for the link itself, one body for unknown, expired, used and
    superseded alike. Not ``401``: there is no credential to re-present and no
    ``WWW-Authenticate`` that would mean anything.

    Rate limited per source address. Not because 256 bits are guessable -- they are
    not -- but because each call pays one Argon2 hash, and an endpoint that spends
    64 MiB and three passes per request for an anonymous caller is a denial of
    service with a friendly name.
    """
    try:
        await throttles.password_reset_attempts.acquire(_client_key(request))
    except AtCapacityError as exc:
        raise rate_limited(
            detail="Too many password reset attempts from this address. Try again later.",
            retry_after=exc.retry_after,
        ) from None

    try:
        await auth.reset_password(
            token=payload.token.get_secret_value(),
            new_password=payload.new_password.get_secret_value(),
        )
    except WeakPasswordError as exc:
        raise ProblemError(
            slug="password-too-weak",
            title="Password too weak",
            status=422,
            detail=exc.detail,
        ) from None
    except PasswordResetTokenInvalidError:
        raise _reset_token_invalid() from None


@router.get("/session", response_model=SessionOut, summary="The current session")
async def read_session(principal: PrincipalDep) -> SessionOut:
    """Who is signed in, which households they may open, and the CSRF token.

    Called by the interface on every load: the cookie survives a refresh, the
    in-memory CSRF token does not, and this is where it is fetched back. A ``401``
    here is the signal to show the sign-in screen.
    """
    return _session_out(principal)


#: The header the interface must echo. Exported so ``api/main.py`` can add it to
#: the CORS allow-list without repeating the string.
__all__ = ["CSRF_HEADER", "router"]
