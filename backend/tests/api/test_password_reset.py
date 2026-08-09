"""Recovery: the oracle that closed, the token that works once, and the log that stays clean.

Four properties are load-bearing, and the file is organised around them.

**The two responses are identical.** Registering an address that already has an
account and one that does not must produce the same status, the same headers and
the same body -- and asking for a reset must do the same. That is pentest finding
O-10f, and it is asserted by comparison rather than by reading each answer
separately: two responses are diffed, so a field added to one of them in a year's
time fails here.

**The difference goes to the mailbox.** Same file, next test: the *messages* are
what differ, and only they.

**A token works once, briefly, and never in the clear.** Single use, superseded
by anything that moves the password, indistinguishable from a wrong one once
spent, and stored as a digest.

**And it is never written to a log.** Asserted on the **rendered** output of
``JsonFormatter`` rather than on the ``extra=`` mapping a call site passed: the
mapping is what the caller meant, the rendered line is what lands on disk, and
the two are only the same while nobody logs a message string.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from chaudron.api.deps import CSRF_HEADER, SESSION_COOKIE
from chaudron.domain.email_ports import OutboundMessage
from chaudron.domain.models import PasswordResetToken, UserAccount, UserSession
from chaudron.infra.email.doubles import FailingMailer, RecordingMailer
from chaudron.infra.logging import JsonFormatter
from chaudron.services.auth import hash_token
from tests.conftest import MakeUser, SignedIn

PASSWORD: Final = "un-mot-de-passe-assez-long"
NEW_PASSWORD: Final = "un-autre-mot-de-passe-bien-long"

#: What a reset link looks like in the body of a message. 64 lowercase hex
#: characters, which is ``secrets.token_hex(32)`` and is also -- deliberately --
#: exactly the shape ``infra/redaction.py`` blanks on sight (``services/auth.py``).
_LINK: Final = re.compile(r"[?&]reset=([0-9a-f]{64})")

#: Headers that legitimately differ between two otherwise identical responses.
#: ``x-request-id`` is generated per request on purpose (audit AUD-014) and
#: ``date`` is a clock; everything else must match, including ``content-length``.
_VOLATILE_HEADERS: Final = frozenset({"date", "x-request-id"})


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def mailer(api_app: FastAPI) -> RecordingMailer:
    """Give the application a mail relay it can reach.

    Assigned to ``app.state`` rather than through ``dependency_overrides``,
    because that is how the real one is wired (``api/main.py``) and a test that
    replaced a different seam would not exercise ``get_mailer``.

    The default test settings name no SMTP host, so an application built without
    this fixture has ``mailer is None`` -- which is the state the "this instance
    cannot send mail" tests want, and they simply do not ask for it.
    """
    recorder = RecordingMailer()
    api_app.state.mailer = recorder
    return recorder


@pytest.fixture
def rendered_logs() -> Iterator[StringIO]:
    """Capture what the JSON formatter actually writes, for the whole test.

    Not ``caplog``. ``caplog`` keeps :class:`logging.LogRecord` objects, so an
    assertion against it checks the *arguments* a call site passed and never the
    text that reaches disk -- and redaction happens in the formatter
    (``infra/logging.py``). A token could therefore be absent from every
    ``record.__dict__`` and present in every line of the file.

    Installed on the root logger at ``DEBUG`` so nothing is filtered out by level,
    and removed afterwards.
    """
    buffer = StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _comparable(response: httpx.Response) -> tuple[int, dict[str, str], str]:
    """A response reduced to what two of them must agree on, byte for byte."""
    body = json.loads(response.text)
    body.pop("request_id", None)
    return (
        response.status_code,
        {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in _VOLATILE_HEADERS
        },
        json.dumps(body, sort_keys=True),
    )


def _token_in(message: OutboundMessage) -> str:
    found = _LINK.search(message.body)
    assert found is not None, f"no reset link in the message body: {message.body!r}"
    return found.group(1)


async def _register(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(
        "/v1/auth/register",
        json={"email": email, "password": PASSWORD, "household_name": "Chez test"},
    )


async def _ask_for_reset(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post("/v1/auth/password/reset-request", json={"email": email})


# --------------------------------------------------------------------------- #
# The oracle
# --------------------------------------------------------------------------- #


async def test_registration_answers_identically_for_a_taken_and_a_free_address(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer, make_user: MakeUser
) -> None:
    """Pentest finding O-10f, closed and pinned.

    The two answers are compared to each other rather than each checked against a
    literal, so this fails the day somebody adds a field to one branch -- which is
    how the oracle would come back.

    The existing account is seeded through the fixture rather than through a first
    call to the endpoint, and that is a constraint of the harness rather than a
    preference: the whole test runs in one transaction, and
    ``set_transaction_household`` refuses to re-scope one (``infra/db.py``), so
    creating two households in a single test is not possible. Seeding one account
    leaves exactly one household to be created, by the call under test.
    """
    taken = (await make_user(email="oracle-taken@example.test")).email
    free = "oracle-free@example.test"

    again = await _register(anonymous_client, taken)
    fresh = await _register(anonymous_client, free)

    assert again.status_code == 202
    assert _comparable(again) == _comparable(fresh), (
        "the answer for an address that already has an account must be "
        "indistinguishable from the answer for one that does not"
    )
    assert SESSION_COOKIE not in again.cookies
    assert SESSION_COOKIE not in fresh.cookies


async def test_a_reset_request_answers_identically_for_a_known_and_an_unknown_address(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """The oracle must not simply move from ``/register`` to here."""
    known = "reset-known@example.test"
    assert (await _register(anonymous_client, known)).status_code == 202

    for_known = await _ask_for_reset(anonymous_client, known)
    for_unknown = await _ask_for_reset(anonymous_client, "reset-unknown@example.test")

    assert for_known.status_code == 202
    assert _comparable(for_known) == _comparable(for_unknown)


async def test_the_difference_travels_by_mail_and_only_by_mail(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """One message either way, and the two say different things.

    This is the other half of the two tests above: closing the oracle is only
    acceptable because the person entitled to the answer still gets it.
    """
    address = "mailbox@example.test"
    await _register(anonymous_client, address)
    await _register(anonymous_client, address)

    messages = mailer.to(address)
    assert len(messages) == 2, "one message per attempt, to the address itself"

    welcome, collision = messages
    assert "compte est prêt" in welcome.subject
    assert _LINK.search(welcome.body) is None, "a welcome message carries no reset link"

    assert "inscription a été tentée" in collision.subject
    assert _token_in(collision), "the person who owns the address gets a way back in"


async def test_an_unknown_address_is_told_so_rather_than_left_waiting(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """ "Nothing arrived" must never be the answer somebody is left with.

    ``docs/security-model.md`` names a silent non-send as the failure a mail
    feature must not produce. A mistyped address and a broken relay would
    otherwise leave identical evidence, and only one of the two is the user's to
    fix.
    """
    address = "nobody-here@example.test"
    assert (await _ask_for_reset(anonymous_client, address)).status_code == 202

    messages = mailer.to(address)
    assert len(messages) == 1
    assert "aucune réinitialisation" in messages[0].subject.lower()
    assert _LINK.search(messages[0].body) is None, "there is no account, so there is no link"


# --------------------------------------------------------------------------- #
# The token
# --------------------------------------------------------------------------- #


async def _issue_link(client: httpx.AsyncClient, mailer: RecordingMailer, address: str) -> str:
    assert (await _ask_for_reset(client, address)).status_code == 202
    return _token_in(mailer.to(address)[-1])


async def test_a_reset_link_sets_the_password_once_and_only_once(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    address = "single-use@example.test"
    await _register(anonymous_client, address)
    token = await _issue_link(anonymous_client, mailer, address)

    first = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert first.status_code == 204, first.text

    # The new password works...
    signed_in = await anonymous_client.post(
        "/v1/auth/login", json={"email": address, "password": NEW_PASSWORD}
    )
    assert signed_in.status_code == 200

    # ...and the link does not, a second time.
    replay = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": token, "new_password": "encore-un-autre-long"}
    )
    assert replay.status_code == 400
    assert replay.json()["type"].endswith("/password-reset-token-invalid")

    # And the second password was not set either.
    refused = await anonymous_client.post(
        "/v1/auth/login", json={"email": address, "password": "encore-un-autre-long"}
    )
    assert refused.status_code == 401


async def test_a_spent_token_is_indistinguishable_from_a_wrong_one(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """Used, expired, superseded and never-existed are one answer.

    Telling them apart would confirm that a reset had been asked for on the
    address the holder of the link already knows -- the enumeration answer the
    whole flow is arranged to withhold.
    """
    address = "spent@example.test"
    await _register(anonymous_client, address)
    token = await _issue_link(anonymous_client, mailer, address)
    assert (
        await anonymous_client.post(
            "/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )
    ).status_code == 204

    spent = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": token, "new_password": "quelque-chose-de-long"}
    )
    invented = await anonymous_client.post(
        "/v1/auth/password/reset",
        json={"token": "0" * 64, "new_password": "quelque-chose-de-long"},
    )
    assert _comparable(spent) == _comparable(invented)


async def test_an_expired_token_is_refused(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer, db_session: AsyncSession
) -> None:
    address = "expired@example.test"
    await _register(anonymous_client, address)
    token = await _issue_link(anonymous_client, mailer, address)

    record = await db_session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
    )
    assert record is not None
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    refused = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert refused.status_code == 400


async def test_asking_again_supersedes_the_previous_link(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """Two live keys in one inbox is one key too many."""
    address = "superseded@example.test"
    await _register(anonymous_client, address)
    stale = await _issue_link(anonymous_client, mailer, address)
    current = await _issue_link(anonymous_client, mailer, address)
    assert stale != current

    refused = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": stale, "new_password": NEW_PASSWORD}
    )
    assert refused.status_code == 400

    accepted = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": current, "new_password": NEW_PASSWORD}
    )
    assert accepted.status_code == 204


async def test_the_token_is_stored_as_a_digest(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer, db_session: AsyncSession
) -> None:
    """A dump of ``password_reset_token`` must open no account."""
    address = "digest-reset@example.test"
    await _register(anonymous_client, address)
    token = await _issue_link(anonymous_client, mailer, address)

    rows = list(await db_session.scalars(select(PasswordResetToken)))
    assert rows, "the token row was not written"
    assert all(row.token_hash != token for row in rows), "the plaintext reached the database"
    assert any(row.token_hash == hash_token(token) for row in rows)


async def test_a_weak_password_is_refused_before_the_token_is_looked_at(
    anonymous_client: httpx.AsyncClient,
) -> None:
    """Otherwise the *choice* of error message is a probe for token validity.

    A bogus token with a short password answers ``422 password-too-weak``, exactly
    as a good token with a short password would. The pydantic layer refuses it
    first, which is the same ordering the service enforces underneath.
    """
    refused = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": "0" * 64, "new_password": "court"}
    )
    assert refused.status_code == 422


# --------------------------------------------------------------------------- #
# What a reset does to everything else
# --------------------------------------------------------------------------- #


async def test_a_completed_reset_revokes_every_session(
    api_app: FastAPI,
    anonymous_client: httpx.AsyncClient,
    mailer: RecordingMailer,
    signed_in: SignedIn,
    db_session: AsyncSession,
) -> None:
    """A reset that leaves an intruder's cookie alive has done nothing.

    The account here is the suite's own signed-in user, which already holds a live
    ``user_session`` row -- so this asserts against a session that existed *before*
    the reset was asked for, which is the shape of the attack.
    """
    address = signed_in.user.email
    live = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
        headers={CSRF_HEADER: signed_in.csrf_token},
    )
    async with live:
        assert (await live.get("/v1/auth/session")).status_code == 200

        token = await _issue_link(anonymous_client, mailer, address)
        assert (
            await anonymous_client.post(
                "/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
            )
        ).status_code == 204

        assert (await live.get("/v1/auth/session")).status_code == 401, (
            "the session that existed before the reset must be dead after it"
        )

    rows = list(
        await db_session.scalars(
            select(UserSession).where(UserSession.user_id == signed_in.user.id)
        )
    )
    assert rows and all(row.revoked_at is not None for row in rows)


async def test_a_reset_does_not_hand_back_a_session(
    anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """Following a link from an inbox proves control of a mailbox, not of a password.

    Minting a session here would make the message itself a login link: anything
    that can read the mailbox would become a way in.
    """
    address = "no-session@example.test"
    await _register(anonymous_client, address)
    token = await _issue_link(anonymous_client, mailer, address)

    done = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert done.status_code == 204
    assert SESSION_COOKIE not in done.cookies
    assert not done.content


async def test_changing_a_password_kills_the_outstanding_reset_links(
    api_app: FastAPI,
    anonymous_client: httpx.AsyncClient,
    mailer: RecordingMailer,
    signed_in: SignedIn,
) -> None:
    """A link mailed before a voluntary password change must not still work after it.

    The likely story is an attacker who requested a reset and a user who noticed
    and changed their password. If the link survived, noticing would have
    accomplished nothing.
    """
    token = await _issue_link(anonymous_client, mailer, signed_in.user.email)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="https://testserver",
        cookies={SESSION_COOKIE: signed_in.token},
        headers={CSRF_HEADER: signed_in.csrf_token},
    ) as live:
        changed = await live.post(
            "/v1/auth/password",
            json={"current_password": "correct-horse-battery-staple", "new_password": NEW_PASSWORD},
        )
        assert changed.status_code == 200, changed.text

    refused = await anonymous_client.post(
        "/v1/auth/password/reset", json={"token": token, "new_password": "encore-autre-chose-long"}
    )
    assert refused.status_code == 400


# --------------------------------------------------------------------------- #
# The capability, and its absence
# --------------------------------------------------------------------------- #


async def test_capabilities_reports_no_reset_when_the_instance_has_no_relay(
    anonymous_client: httpx.AsyncClient,
) -> None:
    """No ``mailer`` fixture here: the default test settings name no SMTP host."""
    answer = await anonymous_client.get("/v1/auth/capabilities")
    assert answer.status_code == 200
    assert answer.json() == {"password_reset": False}


async def test_a_reset_request_is_refused_out_loud_when_there_is_no_relay(
    anonymous_client: httpx.AsyncClient,
) -> None:
    """Accepting and dropping is the one behaviour that is not on offer."""
    refused = await _ask_for_reset(anonymous_client, "nowhere@example.test")
    assert refused.status_code == 503
    assert refused.json()["type"].endswith("/email-not-configured")


async def test_registration_still_works_without_a_relay_and_still_says_nothing(
    anonymous_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The oracle stays closed on an instance that cannot send mail.

    The person who forgot they already had an account gets no explanation, which
    is a real cost and the reason ``email_available`` is in the body: the
    interface says so. It is still strictly better than the ``409`` it replaced,
    because the only remaining feedback is sign-in, which already answers
    identically for a wrong password and an unknown address.
    """
    address = "relayless@example.test"
    first = await _register(anonymous_client, address)
    again = await _register(anonymous_client, address)

    assert first.json() == {"status": "accepted", "email_available": False}
    assert _comparable(first) == _comparable(again)

    accounts = list(
        await db_session.scalars(select(UserAccount).where(UserAccount.email == address))
    )
    assert len(accounts) == 1


async def test_capabilities_reports_a_reset_when_a_relay_is_present(
    api_app: FastAPI, anonymous_client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    """Both halves must agree: the settings *and* the object the factory built."""
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"smtp_host": "relay.invalid", "smtp_from": "chaudron@example.test"}
    )
    answer = await anonymous_client.get("/v1/auth/capabilities")
    assert answer.json() == {"password_reset": True}


async def test_a_relay_that_is_down_changes_nothing_the_caller_can_see(
    api_app: FastAPI, anonymous_client: httpx.AsyncClient
) -> None:
    """The send happens after the response, so it cannot fail one.

    This is what makes "do not send mail from the request" a security property
    rather than a latency one: a relay that hangs or refuses must not be
    measurable, and it is not, because the client already has its answer.
    """
    api_app.state.mailer = FailingMailer()
    address = "relay-down@example.test"

    first = await _register(anonymous_client, address)
    assert first.status_code == 202
    assert (await _ask_for_reset(anonymous_client, address)).status_code == 202


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #


async def test_the_reset_token_never_reaches_a_rendered_log_line(
    api_app: FastAPI,
    anonymous_client: httpx.AsyncClient,
    mailer: RecordingMailer,
    rendered_logs: StringIO,
) -> None:
    """Asserted on what the formatter writes, not on what a call site passed.

    The whole flow runs inside the capture -- registration, the collision notice,
    the reset request, the failed relay, the consumption -- so every line this
    feature can emit is in the buffer.
    """
    address = "quiet@example.test"
    await _register(anonymous_client, address)
    await _register(anonymous_client, address)
    token = await _issue_link(anonymous_client, mailer, address)
    api_app.state.mailer = FailingMailer()
    await _ask_for_reset(anonymous_client, "another-quiet@example.test")
    assert (
        await anonymous_client.post(
            "/v1/auth/password/reset", json={"token": token, "new_password": NEW_PASSWORD}
        )
    ).status_code == 204

    written = rendered_logs.getvalue()
    assert written, "nothing was logged at all; the capture is not wired up"
    assert token not in written, "the reset token reached a log line"
    assert hash_token(token) not in written, "the digest is not a secret, and is still noise"


def test_a_token_logged_by_accident_is_blanked_by_the_formatter(
    rendered_logs: StringIO,
) -> None:
    """The second lock, and the reason the token is hexadecimal.

    Nothing logs a reset token today, and the test above proves it. This proves
    the property that catches the line somebody adds in a hurry two years from
    now: 64 unbroken hex characters is exactly the shape ``infra/redaction.py``
    blanks on length alone. A URL-safe base64 token would have carried ``-`` and
    ``_``, each of which breaks that run, and would have survived intact.
    """
    from chaudron.services.auth import new_reset_token

    token = new_reset_token()
    assert len(token) == 64
    logging.getLogger("chaudron.test").warning("careless %s", token)
    logging.getLogger("chaudron.test").warning("careless", extra={"token": token})

    written = rendered_logs.getvalue()
    assert token not in written
    assert written.count("[redacted]") >= 2
