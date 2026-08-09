"""The one thing the application asks of a mail server, and the message it hands over.

Outbound mail arrived for exactly one reason: an account whose password is
forgotten had no way back, and closing that hole needs a channel only the owner
of an address can read. Everything here is shaped by that single use, and
deliberately not by the general problem of "sending email":

* **one message at a time, no templates, no HTML, no attachments.** A reset
  message is four lines of text and a link. An HTML alternative would add a
  renderer, a sanitiser and a class of injection, and would buy a bold word.
* **no delivery receipt, no bounce handling, no queue.** ``send`` either handed
  the message to a server or raised. What the far end then does with it is
  outside anything this application can observe, and pretending otherwise --
  a ``delivered`` column, say -- would be a field nobody could ever set truthfully.
* **the port is a protocol, so "this instance cannot send mail" is ``None``.**
  Self-hosting is the normal case here and many installs have no mail server at
  all. The absence is therefore a *value* the wiring carries, not an exception
  the call site discovers; see ``api/routers/auth.py`` for what the API answers
  when it holds one.

The address is the only untrusted input that reaches a mail server, and it is the
one an attacker chooses: :func:`validate_recipient` is what stands between
``victim@example.test\\r\\nBcc: everyone`` and a header the SMTP layer would emit
verbatim. It runs in this module rather than in the adapter so that every future
adapter inherits it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "MAX_RECIPIENT_LENGTH",
    "InvalidRecipientError",
    "Mailer",
    "MailerError",
    "OutboundMessage",
    "SendFailedError",
    "validate_recipient",
]

#: RFC 5321's maximum, and the width of ``user_account.email``. An address longer
#: than the column can hold cannot belong to an account, so refusing it here costs
#: nothing and bounds what reaches a header.
MAX_RECIPIENT_LENGTH: Final = 320

#: Every character a header must never contain. CR and LF are the injection
#: itself; NUL and the remaining C0 controls are refused with them because an
#: address containing one is malformed by any reading and letting it through
#: would leave the guard arguing about which control characters are "safe".
_FORBIDDEN_IN_HEADER: Final = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


class MailerError(Exception):
    """Base class for the failures a mailer reports."""


class InvalidRecipientError(MailerError):
    """The address cannot be put in a header.

    Empty, oversized, missing an ``@``, or carrying a control character. The last
    is the one that matters: an address is the only part of an outbound message an
    attacker chooses, and a bare CRLF in it appends headers of their choosing to a
    message this application signs its name to.
    """


class SendFailedError(MailerError):
    """The message was not handed to a server.

    Deliberately opaque about *why*. The reason belongs in a log line the operator
    reads, and the caller's only decision is the same for a refused connection, a
    rejected credential and a timeout: the message did not go.
    """


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One plain-text message, addressed to one recipient.

    Frozen, so a message cannot be edited between being built and being sent --
    the window in which a second recipient would be added.
    """

    to: str
    subject: str
    body: str


def validate_recipient(address: str) -> str:
    """Return *address* stripped, or raise :class:`InvalidRecipientError`.

    Not an RFC 5322 parser and not trying to be: a validator strict enough to be
    correct rejects addresses that work, and one loose enough to accept them all
    proves nothing. What is enforced is exactly what a header needs -- no control
    characters, a bounded length, and one ``@`` with something either side -- and
    whether the mailbox exists is answered by the mail server, which is the only
    thing that can answer it.
    """
    trimmed = address.strip()
    if not trimmed:
        raise InvalidRecipientError("an email address is required")
    if len(trimmed) > MAX_RECIPIENT_LENGTH:
        raise InvalidRecipientError(
            f"an email address may not exceed {MAX_RECIPIENT_LENGTH} characters"
        )
    if _FORBIDDEN_IN_HEADER.intersection(trimmed):
        # The message deliberately does not quote the offending value: it would be
        # echoed into whatever log or error the caller builds, carrying the very
        # CRLF this refusal exists to contain.
        raise InvalidRecipientError("an email address may not contain control characters")
    local, separator, domain = trimmed.partition("@")
    if not separator or not local or not domain:
        raise InvalidRecipientError("an email address must contain a local part and a domain")
    return trimmed


@runtime_checkable
class Mailer(Protocol):
    """Hand one message to a mail server, or raise.

    A protocol rather than a base class, so the test double is a small class in
    ``infra/email/doubles.py`` that inherits nothing, and so "no mail server
    configured" is expressible as ``Mailer | None`` at the wiring.
    """

    async def send(self, message: OutboundMessage) -> None:
        """Deliver *message*, raising :class:`MailerError` if it could not be handed over."""
        ...
