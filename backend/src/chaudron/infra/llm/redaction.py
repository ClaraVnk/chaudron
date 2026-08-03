"""Keep API keys out of everything that leaves the adapter.

Finding SEC-003 of the security review: a provider SDK can embed the credential it
was called with in its own exception message, and `str(exc)` in a log line, a
traceback or an error response is enough to leak a third party's billable secret.
The rule the adapters follow is therefore *translate, never quote*: a domain error
is built from our own fields.

This module is the second line of defence, for the one place where quoting is
genuinely useful -- a snippet of a malformed model response, which helps diagnose a
bad prompt. That snippet is untrusted text, so it is scrubbed here before it can
reach a message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

__all__ = ["REDACTED", "redact", "snippet"]

REDACTED: Final = "[redacted]"

#: Shapes that are secrets by construction. Deliberately broad: a false positive
#: costs a slightly less readable diagnostic, a false negative costs a rotation.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Anthropic, OpenAI and most of their imitators.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    # Google AI Studio.
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}"),
    # AWS access key ids, which show up in misconfigured gateways.
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    # Bearer tokens and JWTs.
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{4,}"),
    # Credentials embedded in a URL.
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # PEM blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

#: Anything shorter than this is not a credential; blanking it would only mangle
#: readable text (and an empty configured key must never blank the whole string).
_MIN_LITERAL_SECRET_LENGTH: Final = 8


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with known and probable credentials replaced.

    ``secrets`` are the values we actually hold -- the household's decrypted key,
    typically. They are removed by literal match, which catches a key that no
    pattern would recognise (a self-hosted gateway's opaque token, for instance).
    """
    for secret in secrets:
        if len(secret) >= _MIN_LITERAL_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def snippet(text: str, *, limit: int = 200, secrets: Iterable[str] = ()) -> str:
    """A short, scrubbed, single-line excerpt safe to put in a domain error."""
    cleaned = redact(" ".join(text.split()), secrets=secrets)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "..."
