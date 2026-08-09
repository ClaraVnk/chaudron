"""Mailers for tests: one that records, one that always fails.

In ``src/`` rather than in ``tests/`` for the reason ``infra/llm/doubles.py``
gives: a double that lives beside the adapter it replaces is a double that gets
updated when the port changes, and one that lives in the test tree is a second
implementation nobody type-checks against the first. Both classes below satisfy
:class:`~chaudron.domain.email_ports.Mailer` structurally, and ``mypy --strict``
is what proves it.

Neither ships in a request path. They are constructed by tests and assigned to
``app.state.mailer``.
"""

from __future__ import annotations

from chaudron.domain.email_ports import (
    Mailer,
    OutboundMessage,
    SendFailedError,
    validate_recipient,
)

__all__ = ["FailingMailer", "RecordingMailer"]


class RecordingMailer:
    """Keeps every message instead of sending it.

    :func:`~chaudron.domain.email_ports.validate_recipient` is still applied, so a
    test cannot pass an address the real adapter would have refused and conclude
    that the flow works.
    """

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        validate_recipient(message.to)
        self.sent.append(message)

    def to(self, address: str) -> list[OutboundMessage]:
        """Every message addressed to *address*, in the order they were sent."""
        return [message for message in self.sent if message.to == address]


class FailingMailer:
    """Raises on every send, and counts the attempts.

    For the property that matters most on the request path: a mail server that is
    down must change nothing an unauthenticated caller can observe.
    """

    def __init__(self) -> None:
        self.attempts: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.attempts.append(message)
        raise SendFailedError("the double refuses every message")


def _conformance() -> tuple[Mailer, Mailer]:
    """Structural conformance, checked by ``mypy --strict`` rather than at runtime.

    Never called. It exists so that a change to the protocol which one of these
    two stops satisfying fails the type check here, in the file that has to be
    fixed, rather than at whichever test happens to assign one to ``app.state``.
    """
    return RecordingMailer(), FailingMailer()
