"""HTTP plumbing shared by the adapters that speak plain REST, plus the SSRF guard.

The guard is the reason this module exists. In the co-located Ollama topology of
ADR-0007 the base URL is **supplied by the user and dialled by the server**: a
textbook SSRF primitive. The usual mitigation -- reject private ranges -- is
inoperative, because the legitimate address of a co-located Ollama *is* private
(``http://ollama:11434`` on a Podman network). The replacement is an explicit
allowlist of hosts from the instance environment, plus:

* schemes restricted to ``http`` and ``https``;
* no credentials in the URL;
* DNS resolved at validation **and** again immediately before the call, with the two
  answers required to agree -- otherwise a name that resolves to an allowed host at
  save time and to a metadata endpoint at call time (DNS rebinding) would pass;
* redirects disabled, so a permitted host cannot bounce us onto a forbidden one;
* a bounded timeout and a bounded response size, so a hostile or broken endpoint
  cannot hold a connection or exhaust memory.

Everything here raises domain errors. No ``httpx`` exception crosses the boundary.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from chaudron.domain.llm_ports import (
    ProviderContext,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from chaudron.infra.llm.redaction import snippet
from chaudron.infra.llm.settings import ALLOWED_HOSTS_ENV_VAR, LlmSettings

__all__ = [
    "GuardedHttpClient",
    "HttpFailure",
    "Resolver",
    "system_resolver",
    "translate_http_status",
    "translate_transport_error",
    "validate_ollama_base_url",
]

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

#: Resolve a (host, port) pair to the set of addresses it currently points at.
Resolver = Callable[[str, int], Awaitable[frozenset[str]]]


async def system_resolver(host: str, port: int) -> frozenset[str]:
    """The real resolver: the operating system's, off the event loop thread."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ProviderUnavailable(f"host {host!r} could not be resolved") from exc
    return frozenset(str(info[4][0]) for info in infos)


def validate_ollama_base_url(raw_url: str, settings: LlmSettings) -> httpx.URL:
    """Structural half of the guard, run at configuration time and before every call.

    Rejecting here, at registration, is what ADR-0007 asks for: a household should
    learn that its URL is not permitted while it is looking at the form, not on the
    first recipe it tries to generate.
    """
    text = (raw_url or "").strip()
    if not text:
        raise ProviderNotConfigured("the Ollama base URL is empty")
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL, ValueError:
        raise ProviderNotConfigured(f"the Ollama base URL is malformed: {snippet(text)}") from None

    if url.scheme not in _ALLOWED_SCHEMES:
        raise ProviderNotConfigured(
            f"the Ollama base URL must use http or https, not {url.scheme!r}"
        )
    if url.userinfo:
        raise ProviderNotConfigured("the Ollama base URL must not embed credentials")
    host = url.host
    if not host:
        raise ProviderNotConfigured("the Ollama base URL has no host")
    if not settings.ollama_allowed_hosts:
        raise ProviderNotConfigured(
            "no Ollama host is allowed on this instance; set "
            f"{ALLOWED_HOSTS_ENV_VAR} to the container or service name of your "
            "Ollama and restart the instance"
        )
    if not settings.allows_host(host):
        allowed = ", ".join(sorted(settings.ollama_allowed_hosts))
        raise ProviderNotConfigured(
            f"host {host!r} is not in this instance's Ollama allowlist ({allowed}); "
            f"add it to {ALLOWED_HOSTS_ENV_VAR} and restart the instance"
        )
    return url


def translate_transport_error(
    exc: Exception, context: ProviderContext, *, provider_label: str
) -> Exception:
    """Turn an ``httpx`` transport failure into the matching domain error."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderUnavailable(
            f"{provider_label} did not answer within the configured timeout",
            context=ProviderContext(context.provider, context.model, "timeout"),
        )
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailable(
            f"{provider_label} could not be reached",
            context=ProviderContext(context.provider, context.model, "connection_refused"),
        )
    return ProviderUnavailable(
        f"{provider_label} call failed before a response was received",
        context=ProviderContext(context.provider, context.model, "client_error"),
    )


@dataclass(frozen=True, slots=True)
class HttpFailure:
    """A non-2xx answer, reduced to what is safe to reason about."""

    status: int
    body: str


class GuardedHttpClient:
    """A tiny POST-JSON client with every bound ADR-0007 asks for already applied."""

    def __init__(
        self,
        base_url: httpx.URL,
        settings: LlmSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        pinned_addresses: frozenset[str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._settings = settings
        self._transport = transport
        self._resolver = resolver
        self._pinned = pinned_addresses
        self._headers = dict(headers or {})

    @property
    def base_url(self) -> httpx.URL:
        return self._base_url

    async def assert_stable_resolution(self) -> None:
        """Re-resolve and compare, so a rebound name is caught before the call.

        Skipped when no resolver is configured -- which is the case for the
        first-party providers, whose hostnames are ours to trust and are not
        user-supplied. Only the Ollama path, where the URL comes from a household,
        pins its resolution.
        """
        if self._resolver is None or self._pinned is None:
            return
        port = self._base_url.port or (443 if self._base_url.scheme == "https" else 80)
        host = self._base_url.host
        current = await self._resolver(host, port)
        if not current & self._pinned:
            raise ProviderNotConfigured(
                f"host {host!r} now resolves elsewhere than when it was registered; "
                "the request was refused (possible DNS rebinding)"
            )

    async def get_json(
        self,
        path: str,
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> dict[str, Any] | HttpFailure:
        """GET ``path``; return the decoded object or the failure to translate.

        Exists because some provider endpoints only answer GET. Ollama's
        ``/api/version`` is the case that forced it: a POST there returns 405, and
        the adapter used to send one on the assumption that a single verb kept every
        outbound call under the same guards. The guards belong to
        :meth:`_send_json`, not to the verb, so both share them.
        """
        return await self._send_json(
            "GET", path, None, context=context, provider_label=provider_label
        )

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> dict[str, Any] | HttpFailure:
        """POST ``payload``; return the decoded object or the failure to translate."""
        return await self._send_json(
            "POST", path, payload, context=context, provider_label=provider_label
        )

    async def _send_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        context: ProviderContext,
        provider_label: str,
    ) -> dict[str, Any] | HttpFailure:
        await self.assert_stable_resolution()
        url = self._base_url.join(path)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._settings.timeout_seconds,
            # A permitted host must not be able to bounce us onto a forbidden one.
            follow_redirects=False,
            headers=self._headers,
        ) as client:
            try:
                request = (
                    client.build_request(method, url)
                    if payload is None
                    else client.build_request(method, url, json=dict(payload))
                )
                response = await client.send(request, stream=True)
                try:
                    body = await self._read_bounded(response, context)
                finally:
                    await response.aclose()
            except httpx.HTTPError as exc:
                raise translate_transport_error(
                    exc, context, provider_label=provider_label
                ) from None

        # 3xx counts as a failure, not a success: redirects are disabled on purpose,
        # so a redirect is a permitted host trying to send us somewhere else.
        if response.status_code >= 300:
            return HttpFailure(status=response.status_code, body=body)
        try:
            decoded: object = json.loads(body)
        except json.JSONDecodeError:
            raise ProviderResponseInvalid(
                f"{provider_label} returned a body that is not JSON: {snippet(body)}",
                context=ProviderContext(context.provider, context.model, "malformed_payload"),
            ) from None
        if not isinstance(decoded, dict):
            raise ProviderResponseInvalid(
                f"{provider_label} returned a JSON value that is not an object",
                context=ProviderContext(context.provider, context.model, "malformed_payload"),
            )
        return decoded

    async def _read_bounded(self, response: httpx.Response, context: ProviderContext) -> str:
        limit = self._settings.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise ProviderResponseInvalid(
                    f"response exceeded the {limit} byte ceiling and was abandoned",
                    context=ProviderContext(context.provider, context.model, "response_too_large"),
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")


def translate_http_status(
    failure: HttpFailure,
    context: ProviderContext,
    *,
    provider_label: str,
    not_found_hint: str | None = None,
    credentials_hint: str | None = None,
) -> Exception:
    """Map a non-2xx status onto the domain, without echoing the provider's body."""
    status = failure.status
    if status in (401, 403):
        message = f"{provider_label} rejected the credentials ({status})"
        if credentials_hint:
            message += f"; {credentials_hint}"
        return ProviderNotConfigured(
            message, context=ProviderContext(context.provider, context.model, "invalid_credentials")
        )
    if status == 404:
        message = f"{provider_label} has no such endpoint or model ({status})"
        if not_found_hint:
            message += f"; {not_found_hint}"
        return ProviderNotConfigured(
            message, context=ProviderContext(context.provider, context.model, "not_found")
        )
    if status == 429:
        return ProviderQuotaExceeded(
            f"{provider_label} rate limit reached",
            context=ProviderContext(context.provider, context.model, "rate_limited"),
        )
    if status in (402, 413) or (status == 400 and _looks_like_quota(failure.body)):
        if status == 413:
            return ProviderUnavailable(
                f"{provider_label} refused the request as too large",
                context=ProviderContext(context.provider, context.model, "request_too_large"),
            )
        return ProviderQuotaExceeded(
            f"{provider_label} reports the account is out of credit or quota",
            context=ProviderContext(context.provider, context.model, "quota_exhausted"),
        )
    if status >= 500:
        return ProviderUnavailable(
            f"{provider_label} returned a server error ({status})",
            context=ProviderContext(context.provider, context.model, "server_error"),
        )
    return ProviderUnavailable(
        f"{provider_label} returned an unexpected status ({status})",
        context=ProviderContext(context.provider, context.model, "unexpected_status"),
    )


_QUOTA_MARKERS: Final = ("quota", "credit", "billing", "insufficient", "exceeded")


def _looks_like_quota(body: str) -> bool:
    """Route on the body's wording; never re-emit it.

    Several providers report an exhausted balance as a 400 rather than a 402, and
    telling a household "you are out of credit" instead of "something went wrong" is
    the difference between a two-minute fix and a support ticket.
    """
    lowered = body.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)
