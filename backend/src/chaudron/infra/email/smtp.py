"""The SMTP adapter, and the reasons it is built from the standard library.

**No new dependency.** ``smtplib`` and ``email.message`` have shipped with Python
since before this application existed, they speak the whole of the protocol this
uses -- connect, ``STARTTLS``, ``AUTH``, one message -- and an async client would
have added a package to the runtime image for a call this process makes a handful
of times a day. ``pyproject.toml``'s dependency policy asks that question first;
here the answer is that the standard library already covers it.

**It is blocking, so it does not run on the event loop.** ``smtplib`` performs
synchronous socket I/O with a timeout measured in seconds, and a mail server that
accepts a connection and then falls silent would freeze every other request in
the process -- the exact failure ``infra/documents/sandbox.py`` documents for the
PDF parser, measured there at 48 seconds. The send therefore runs in a worker
thread through ``run_in_executor``, which is the shape this codebase already uses
for blocking work.

**And it is not called from a request handler at all.** ``api/routers/auth.py``
hands the message to a Starlette background task, so the response has already
left before the socket is opened. That is a security property rather than a
latency one: an SMTP dial-out inside the handler makes "did this address have an
account?" answerable with a stopwatch, and the whole point of the endpoints that
call this is that the answer is unobservable.

**Concurrency is bounded.** One semaphore, small, per mailer. The rate limiters
in front of the two callers already bound how many messages an attacker can ask
for; this bounds how many threads and sockets a burst of *legitimate* traffic can
occupy, because the executor's default pool is shared with everything else that
ever runs off the loop.

**Nothing the far end says is quoted.** An SMTP server's rejection routinely
echoes the ``AUTH`` username, and some gateways echo more; a message built from
its text would carry that into a log line. Every failure here becomes
:class:`SendFailedError` with our own words, and the operator's diagnosis comes
from a structured log line naming the exception *type* and the configured host.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING, Final, Literal

from chaudron.domain.email_ports import (
    Mailer,
    OutboundMessage,
    SendFailedError,
    validate_recipient,
)

if TYPE_CHECKING:
    from chaudron.config import Settings

__all__ = ["SmtpMailer", "SmtpSettings", "build_mailer"]

logger = logging.getLogger(__name__)

#: How the connection is protected. ``starttls`` upgrades a plain connection on
#: the submission port (587) and is what every hosted provider expects;
#: ``implicit-tls`` opens the socket already wrapped (465); ``none`` is neither,
#: and ``config.py`` refuses to pair it with credentials except on the loopback.
SmtpSecurity = Literal["starttls", "implicit-tls", "none"]

#: Messages this process will have in flight at once. Three is generous for what
#: this sends -- a password reset and a registration notice -- and the number is
#: fixed here rather than configured because it bounds *threads*, which is a
#: property of this process and not a decision an operator has any basis to make.
_MAX_CONCURRENT_SENDS: Final = 3


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Everything needed to hand a message to one mail server.

    A value object rather than a reference to :class:`~chaudron.config.Settings`,
    so the adapter can be built in a test without a whole configuration, and so
    the password is held in one place that is easy to grep for.
    """

    host: str
    port: int
    sender: str
    security: SmtpSecurity = "starttls"
    username: str | None = None
    password: str | None = None
    timeout_seconds: float = 15.0


class SmtpMailer:
    """A :class:`~chaudron.domain.email_ports.Mailer` speaking SMTP.

    One connection per message, opened and closed inside the send. A pooled
    connection would save a TCP and TLS handshake on a call this application makes
    a few times a day, and would buy that with a socket held open across an idle
    period long enough for every intermediary to close it from the other end --
    which is discovered as an intermittent failure of the one feature nobody can
    afford to have fail intermittently.
    """

    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings
        self._in_flight = asyncio.Semaphore(_MAX_CONCURRENT_SENDS)

    async def send(self, message: OutboundMessage) -> None:
        """Hand *message* to the configured server, or raise :class:`SendFailedError`.

        The recipient is validated before anything is built:
        :func:`~chaudron.domain.email_ports.validate_recipient` is what stops an
        address carrying a bare CRLF from appending headers of somebody else's
        choosing, and it raises :class:`InvalidRecipientError`, which is *not* a
        send failure and must not be reported as one.
        """
        recipient = validate_recipient(message.to)
        envelope = self._compose(recipient, message)
        loop = asyncio.get_running_loop()
        async with self._in_flight:
            try:
                await loop.run_in_executor(None, self._deliver, envelope)
            except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                # Type only. An SMTP rejection commonly quotes the AUTH username
                # back, and the exception text is what would reach the log.
                logger.warning(
                    "smtp_send_failed",
                    extra={"error": type(exc).__name__, "smtp_host": self._settings.host},
                )
                raise SendFailedError(
                    "the message could not be handed to the mail server"
                ) from None

    def _compose(self, recipient: str, message: OutboundMessage) -> EmailMessage:
        envelope = EmailMessage()
        envelope["From"] = self._settings.sender
        envelope["To"] = recipient
        envelope["Subject"] = message.subject
        # Plain text, UTF-8, and nothing else. No HTML alternative, no attachment,
        # no `Reply-To` pointing at an address nobody reads.
        envelope.set_content(message.body, subtype="plain", charset="utf-8")
        return envelope

    def _deliver(self, envelope: EmailMessage) -> None:
        """The blocking half. Runs in a worker thread, never on the event loop."""
        settings = self._settings
        if settings.security == "implicit-tls":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.host,
                settings.port,
                timeout=settings.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)

        with client:
            if settings.security == "starttls":
                # `starttls` raises `SMTPNotSupportedError` when the server does not
                # advertise the extension, and that refusal is the control: silently
                # continuing in clear would send the credentials -- and the reset
                # link -- across the wire on a connection the operator asked to have
                # encrypted. A downgrade must fail, not degrade.
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings.username is not None and settings.password is not None:
                client.login(settings.username, settings.password)
            client.send_message(envelope)


def build_mailer(settings: Settings) -> Mailer | None:
    """The mailer this instance owns, or ``None`` when it has no mail server.

    ``None`` is a first-class answer and the reason the port is a protocol rather
    than a base class. Many self-hosted installs have no SMTP relay at all; the
    application must therefore be able to *say* the capability is absent
    (``GET /v1/auth/capabilities``) rather than accept a request and drop it,
    which is the failure mode that turns "I never received the message" into an
    incident nobody can close.

    ``config.py`` has already refused to start on a half-filled configuration -- a
    host without a sender address, credentials without transport security -- so
    everything read here is known good by the time this runs.
    """
    if settings.smtp_host is None:
        return None
    return SmtpMailer(
        SmtpSettings(
            host=settings.smtp_host,
            port=settings.smtp_port,
            # Non-null whenever `smtp_host` is, enforced at startup.
            sender=settings.smtp_from or "",
            security=settings.smtp_security,
            username=settings.smtp_username,
            password=(
                None
                if settings.smtp_password is None
                else settings.smtp_password.get_secret_value()
            ),
            timeout_seconds=settings.smtp_timeout_seconds,
        )
    )
