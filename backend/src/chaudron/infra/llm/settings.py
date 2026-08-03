"""Instance-level settings the LLM layer needs, read from the environment.

Two of these are security controls rather than tuning knobs, and both come from
ADR-0007:

* ``CHAUDRON_OLLAMA_ALLOWED_HOSTS`` -- the SSRF allowlist. The usual defence
  (reject private ranges) is inoperative here, because the legitimate address of a
  co-located Ollama *is* private. An explicit allowlist is the replacement, and its
  operational friction -- adding a host means editing the instance environment and
  restarting -- is the point, not an oversight.
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
    "INSTANCE_OWNER_ENV_VAR",
    "INSTANCE_OWNER_KEY_ENV_VAR",
    "LlmSettings",
    "load_settings",
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

    #: Lower-cased host names an Ollama base URL may point at. Empty means the
    #: ``ollama`` mode is switched off for this instance, which is the default.
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

    def allows_host(self, host: str) -> bool:
        return host.lower() in self.ollama_allowed_hosts


def _parse_hosts(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


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
