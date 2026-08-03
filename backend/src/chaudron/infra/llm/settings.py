"""Instance-level settings the LLM layer needs, read from the environment.

Two of these are security controls rather than tuning knobs, and both come from
ADR-0007:

* ``CHAUDRON_OLLAMA_ALLOWED_HOSTS`` -- the SSRF allowlist. The usual defence
  (reject private ranges) is inoperative here, because the legitimate address of a
  co-located Ollama *is* private. An explicit allowlist is the replacement, and its
  operational friction -- adding a host means editing the instance environment and
  restarting -- is the point, not an oversight. Entries name an **endpoint**, host
  *and* port: an allowlist that only pinned the host let a household dial every
  other port of that host and read the answers apart, which is a port scanner
  (security audit, AUD-005).
* ``CHAUDRON_INSTANCE_OWNER_HOUSEHOLD_ID`` -- the single household allowed to spend
  the operator's own API credit. Unset means nobody can, which is the safe default:
  the failure mode of getting this wrong is a stranger's bill.

.. note::

   These are read here rather than from a shared ``chaudron.config`` module because
   that module does not exist yet. When it lands, this file becomes a thin adapter
   over it; the *validation* below (fail fast, never guess) should move with it.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "ALLOWED_HOSTS_ENV_VAR",
    "DEFAULT_OLLAMA_PORT",
    "INSTANCE_OWNER_ENV_VAR",
    "INSTANCE_OWNER_KEY_ENV_VAR",
    "LlmSettings",
    "load_settings",
    "parse_allowed_endpoint",
]

ALLOWED_HOSTS_ENV_VAR: Final = "CHAUDRON_OLLAMA_ALLOWED_HOSTS"
INSTANCE_OWNER_ENV_VAR: Final = "CHAUDRON_INSTANCE_OWNER_HOUSEHOLD_ID"
INSTANCE_OWNER_KEY_ENV_VAR: Final = "CHAUDRON_INSTANCE_OWNER_API_KEY"
_TIMEOUT_ENV_VAR: Final = "CHAUDRON_LLM_TIMEOUT_SECONDS"
_MAX_RESPONSE_ENV_VAR: Final = "CHAUDRON_LLM_MAX_RESPONSE_BYTES"

#: A local model can be slow, but an unbounded wait is a held connection and a user
#: staring at a spinner. Bounded, and overridable per instance.
_DEFAULT_TIMEOUT_SECONDS: Final = 60.0
#: A parsed receipt or three recipes is a few kilobytes. Anything past this is a
#: misconfigured endpoint or a hostile one, and reading it all is the bug.
_DEFAULT_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024

#: What an allowlist entry means when the operator wrote a bare host. Ollama's own
#: port, and *only* it -- never "every port of that host", which is the permissive
#: default AUD-005 turned into a working port scanner. This is also the reading
#: ``docs/security-model.md`` already prescribed; the code simply never honoured it.
#:
#: Choosing 11434 over "the scheme's default port" is deliberate: an operator who
#: writes ``ollama`` means their Ollama, and silently narrowing that to port 80
#: would refuse every legitimate URL with an error about an allowlist that visibly
#: contains the host -- closed, but undebuggable.
DEFAULT_OLLAMA_PORT: Final = 11434

_MAX_NUM_CTX_ENV_VAR: Final = "CHAUDRON_OLLAMA_MAX_NUM_CTX"
#: Ceiling on the context window requested from Ollama, regardless of what the
#: model could take.
#:
#: Measured, not guessed. Asking qwen2.5:0.5b for its full 32768-token window on
#: a 7.5 GB host with ~1 GB free took **over ten minutes** for one suggestion and
#: OOM-killed llama-server outright on the 3B variant; the same request capped at
#: 4096 answered in **five seconds**. Ollama allocates the KV cache for the whole
#: declared window up front, so the cost is paid even when the prompt is short.
#:
#: 8192 holds a household inventory comfortably while staying within reach of the
#: small self-hosted machines this mode exists for. Raise it on a host with room.
_DEFAULT_MAX_NUM_CTX: Final = 8192


@dataclass(frozen=True, slots=True)
class LlmSettings:
    """Instance-wide settings for the provider layer."""

    #: Endpoints an Ollama base URL may point at, as the operator wrote them:
    #: ``host:port``, or a bare ``host`` meaning :data:`DEFAULT_OLLAMA_PORT`. Empty
    #: means the ``ollama`` mode is switched off for this instance, the default.
    #:
    #: The field keeps its historical name and its ``frozenset[str]`` type because
    #: it is the shape the application configuration hands over; the *decision* is
    #: taken on :attr:`allowed_endpoints`, which is derived from it below.
    ollama_allowed_hosts: frozenset[str] = frozenset()
    #: The one household allowed to use ``instance_owner``. ``None`` means none.
    instance_owner_household_id: uuid.UUID | None = None
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    #: Upper bound on ``num_ctx`` sent to Ollama. See :data:`_DEFAULT_MAX_NUM_CTX`
    #: for why a ceiling exists at all.
    ollama_max_num_ctx: int = _DEFAULT_MAX_NUM_CTX
    #: The operator's own key, read from the instance environment and never from the
    #: database. ``repr=False`` so that dumping the settings -- in a log line, a
    #: traceback, a debugger -- cannot print it (security review, SEC-003).
    instance_owner_api_key: str | None = field(default=None, repr=False)
    #: :attr:`ollama_allowed_hosts`, normalised into the ``(host, port)`` pairs the
    #: guard compares against. Derived, never passed in.
    allowed_endpoints: frozenset[tuple[str, int]] = field(
        init=False, default=frozenset(), compare=False, repr=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_endpoints",
            frozenset(parse_allowed_endpoint(entry) for entry in self.ollama_allowed_hosts),
        )

    def allows_endpoint(self, host: str, port: int) -> bool:
        """Whether this instance permits dialling exactly ``host:port``.

        Both halves, not just the host. The pair is what makes the control a
        control: with the host alone, a household that was allowed to reach
        ``ollama:11434`` was also allowed to reach ``ollama:22`` and
        ``ollama:5432``, and the three shapes of answer told it which of them were
        listening (security audit, AUD-005, proven against a live instance).
        """
        return (host.lower(), port) in self.allowed_endpoints


def parse_allowed_endpoint(entry: str) -> tuple[str, int]:
    """Normalise one allowlist entry into ``(host, port)``.

    Accepts ``host``, ``host:port``, ``[v6]`` and ``[v6]:port``. Raises
    :class:`ValueError` on anything else rather than guessing: an entry the
    operator mistyped must be a startup failure that names the variable, not a
    silently dropped line that turns into an unexplainable refusal months later.
    """
    text = entry.strip().lower()
    if not text:
        raise ValueError(f"{ALLOWED_HOSTS_ENV_VAR} contains an empty entry")
    host, port_text = _split_endpoint(text)
    if not host:
        raise ValueError(f"{ALLOWED_HOSTS_ENV_VAR} entry {entry.strip()!r} has no host")
    if port_text is None:
        return host, DEFAULT_OLLAMA_PORT
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(
            f"{ALLOWED_HOSTS_ENV_VAR} entry {entry.strip()!r} has a non-numeric port; "
            "the form is host:port"
        ) from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{ALLOWED_HOSTS_ENV_VAR} entry {entry.strip()!r} has a port out of range")
    return host, port


def _split_endpoint(text: str) -> tuple[str, str | None]:
    """Separate host from port, keeping bracketed IPv6 literals in one piece."""
    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            raise ValueError(
                f"{ALLOWED_HOSTS_ENV_VAR} entry {text!r} opens an IPv6 bracket it never closes"
            )
        host = text[1:closing]
        rest = text[closing + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise ValueError(f"{ALLOWED_HOSTS_ENV_VAR} entry {text!r} is not host:port")
        return host, rest[1:]
    # An unbracketed IPv6 literal is ambiguous with host:port and is rejected on
    # that ground alone -- `::1` would otherwise parse as host `` port `:1`.
    if text.count(":") > 1:
        raise ValueError(
            f"{ALLOWED_HOSTS_ENV_VAR} entry {text!r} looks like an IPv6 address; "
            "write it bracketed, as [::1]:11434"
        )
    host, _, port_text = text.partition(":")
    return host, port_text or None


def _parse_hosts(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    entries = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    # Parse eagerly so a malformed entry fails the process at startup, where the
    # operator is looking, rather than the first recipe of the first household.
    for entry in entries:
        parse_allowed_endpoint(entry)
    return entries


def _parse_household_id(raw: str | None) -> uuid.UUID | None:
    if not raw or not raw.strip():
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError as exc:
        # Fail fast: a typo here would silently lock the owner out of their own
        # instance, or -- far worse if the comparison were loosened -- let it in.
        raise ValueError(f"{INSTANCE_OWNER_ENV_VAR} is not a valid UUID") from exc


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    if not raw or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _parse_positive_int(raw: str | None, default: int, name: str) -> int:
    if not raw or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def load_settings(environ: dict[str, str] | None = None) -> LlmSettings:
    """Build the settings, raising rather than guessing on malformed input."""
    env = os.environ if environ is None else environ
    return LlmSettings(
        ollama_allowed_hosts=_parse_hosts(env.get(ALLOWED_HOSTS_ENV_VAR)),
        instance_owner_household_id=_parse_household_id(env.get(INSTANCE_OWNER_ENV_VAR)),
        timeout_seconds=_parse_positive_float(
            env.get(_TIMEOUT_ENV_VAR), _DEFAULT_TIMEOUT_SECONDS, _TIMEOUT_ENV_VAR
        ),
        max_response_bytes=_parse_positive_int(
            env.get(_MAX_RESPONSE_ENV_VAR), _DEFAULT_MAX_RESPONSE_BYTES, _MAX_RESPONSE_ENV_VAR
        ),
        ollama_max_num_ctx=_parse_positive_int(
            env.get(_MAX_NUM_CTX_ENV_VAR), _DEFAULT_MAX_NUM_CTX, _MAX_NUM_CTX_ENV_VAR
        ),
        instance_owner_api_key=(env.get(INSTANCE_OWNER_KEY_ENV_VAR) or "").strip() or None,
    )
