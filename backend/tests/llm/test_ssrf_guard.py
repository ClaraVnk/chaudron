"""The Ollama URL is user-supplied and server-dialled: these are its guard rails.

ADR-0007 calls this out as a textbook SSRF primitive whose usual mitigation --
rejecting private ranges -- does not apply, because a co-located Ollama's address
*is* private. What replaces it is asserted here.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator

import httpx
import pytest

from chaudron.domain.llm_ports import ProviderContext, ProviderNotConfigured
from chaudron.infra.llm.http import GuardedHttpClient, HttpFailure, validate_ollama_base_url
from chaudron.infra.llm.ollama_provider import build_guarded_client
from chaudron.infra.llm.settings import (
    ALLOWED_HOSTS_ENV_VAR,
    DEFAULT_OLLAMA_PORT,
    LlmSettings,
    load_settings,
)

_ALLOWED = LlmSettings(ollama_allowed_hosts=frozenset({"ollama", "ollama.internal"}))
_CONTEXT = ProviderContext(provider="ollama", model="llama3.2")


def test_allowlisted_host_is_accepted() -> None:
    url = validate_ollama_base_url("http://ollama:11434", _ALLOWED)
    assert url.host == "ollama"
    assert url.port == 11434


@pytest.mark.parametrize(
    ("raw_url", "expected_fragment"),
    [
        # The whole point: a private address is legitimate *only* if allowlisted.
        ("http://169.254.169.254/latest/meta-data", "this instance permits"),
        ("http://localhost:11434", "this instance permits"),
        ("http://ollama.evil.example", "this instance permits"),
        # Scheme confusion is the other half of the classic SSRF toolbox.
        ("file:///etc/passwd", "http or https"),
        ("gopher://ollama:11434", "http or https"),
        # Credentials in a URL end up in logs and proxy traces.
        ("http://user:secret@ollama:11434", "credentials"),
        ("", "empty"),
    ],
)
def test_rejected_urls(raw_url: str, expected_fragment: str) -> None:
    with pytest.raises(ProviderNotConfigured) as raised:
        validate_ollama_base_url(raw_url, _ALLOWED)
    assert expected_fragment in str(raised.value)


def test_case_is_not_a_bypass() -> None:
    assert validate_ollama_base_url("http://OLLAMA:11434", _ALLOWED).host == "ollama"


def test_empty_allowlist_disables_the_mode_and_names_the_variable() -> None:
    """A closed default, with the remedy in the message rather than in a wiki page."""
    with pytest.raises(ProviderNotConfigured) as raised:
        validate_ollama_base_url("http://ollama:11434", LlmSettings())
    assert ALLOWED_HOSTS_ENV_VAR in str(raised.value)


async def test_dns_rebinding_is_refused_before_the_call() -> None:
    """A name allowlisted at save time must not be dialled once it moves.

    Without the second resolution, a host that answers ``10.89.0.7`` while the
    household is saving the form and ``169.254.169.254`` a second later would pass
    every check and reach the cloud metadata endpoint.
    """

    async def rebound_resolver(host: str, port: int) -> frozenset[str]:
        return frozenset({"169.254.169.254"})

    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        resolver=rebound_resolver,
        pinned_addresses=frozenset({"10.89.0.7"}),
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert "rebinding" in str(raised.value)


async def test_redirects_are_not_followed() -> None:
    """A permitted host must not be able to bounce the server onto another one."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(handler),
    )
    result = await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert not isinstance(result, dict)
    assert result.status == 302
    assert seen == ["http://ollama:11434/api/chat"]


async def test_oversized_response_is_abandoned() -> None:
    """A hostile or broken endpoint must not be able to exhaust memory."""
    from chaudron.domain.llm_ports import ProviderResponseInvalid

    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama"}), max_response_bytes=64)
    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 5000)),
    )
    with pytest.raises(ProviderResponseInvalid) as raised:
        await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert "ceiling" in str(raised.value)


# --------------------------------------------------------------------------- #
# AUD-005: the allowlist constrains the endpoint, not just the host
# --------------------------------------------------------------------------- #


def test_a_listed_host_on_an_unlisted_port_is_refused() -> None:
    """The finding, in one line.

    ``CHAUDRON_OLLAMA_ALLOWED_HOSTS`` used to be compared against ``url.host``,
    which never carries a port, so permitting ``ollama`` permitted ``ollama:22``
    and ``ollama:5432`` as well. On the co-located topology of ADR-0007 the allowed
    host is a Podman service name sitting on the same network as everything else.
    """
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama:11434"}))
    assert validate_ollama_base_url("http://ollama:11434", settings).port == 11434
    for port in (22, 80, 5432, 6379, 11435):
        with pytest.raises(ProviderNotConfigured):
            validate_ollama_base_url(f"http://ollama:{port}", settings)


def test_a_bare_entry_means_ollamas_port_and_only_it() -> None:
    """A permissive reading of an unqualified entry is what created the hole.

    ``ollama`` is what an operator writes when they mean their Ollama, so it means
    port 11434 -- never "any port", and not the scheme's default port either, which
    would refuse every legitimate URL while showing an allowlist that contains the
    host.
    """
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama"}))
    assert settings.allows_endpoint("ollama", DEFAULT_OLLAMA_PORT)
    assert not settings.allows_endpoint("ollama", 80)
    assert not settings.allows_endpoint("ollama", 443)
    assert not settings.allows_endpoint("ollama", 22)


def test_an_omitted_port_in_the_url_is_the_schemes_default_not_a_wildcard() -> None:
    """``http://ollama`` dials port 80, and is judged as port 80."""
    on_eighty = LlmSettings(ollama_allowed_hosts=frozenset({"ollama:80"}))
    assert validate_ollama_base_url("http://ollama", on_eighty).host == "ollama"
    assert validate_ollama_base_url("http://ollama:80", on_eighty).host == "ollama"
    with pytest.raises(ProviderNotConfigured):
        validate_ollama_base_url("https://ollama", on_eighty)

    on_https = LlmSettings(ollama_allowed_hosts=frozenset({"ollama:443"}))
    assert validate_ollama_base_url("https://ollama", on_https).host == "ollama"


def test_bracketed_ipv6_entries_are_understood() -> None:
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"[::1]:11434"}))
    assert validate_ollama_base_url("http://[::1]:11434", settings).host == "::1"
    with pytest.raises(ProviderNotConfigured):
        validate_ollama_base_url("http://[::1]:22", settings)


def test_the_refusal_does_not_read_the_allowlist_back_to_the_household() -> None:
    """Listing it would hand out a map of the instance's private network (A10)."""
    settings = LlmSettings(ollama_allowed_hosts=frozenset({"ollama-internal:11434"}))
    with pytest.raises(ProviderNotConfigured) as raised:
        validate_ollama_base_url("http://ollama:11434", settings)
    message = str(raised.value)
    assert "ollama-internal" not in message
    assert ALLOWED_HOSTS_ENV_VAR in message


@pytest.mark.parametrize(
    "raw",
    ["ollama:notaport", "ollama:0", "ollama:70000", "::1", "[::1", "ollama:11434:9"],
)
def test_a_malformed_allowlist_entry_fails_the_instance_at_startup(raw: str) -> None:
    """Fail fast and name the variable, rather than drop the line and refuse later."""
    with pytest.raises(ValueError, match=ALLOWED_HOSTS_ENV_VAR):
        load_settings({ALLOWED_HOSTS_ENV_VAR: raw})


# --------------------------------------------------------------------------- #
# AUD-005, second half: the scanning oracle
# --------------------------------------------------------------------------- #


def _closed_port() -> int:
    """A port on the loopback that nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def _listener(accepted: list[str], label: str, *, speak_http: bool) -> AsyncGenerator[int]:
    """A real server on a real loopback port, recording every connection it gets."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(label)
        if speak_http:
            await reader.read(1)
            writer.write(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


async def test_a_port_scan_of_an_allowed_host_is_refused_without_opening_a_socket() -> None:
    """The proven attack, replayed against real sockets rather than a mock.

    The audit showed three distinguishable answers -- a listening HTTP server, a
    listening non-HTTP service, and a closed port -- reachable on any port of an
    allowed host, which is a port scanner with an oracle. Here all three exist for
    real on the loopback, and the assertion is twofold: nothing connects to them,
    and the caller cannot tell them apart from what it gets back.
    """
    accepted: list[str] = []
    silent = _listener(accepted, "non-http", speak_http=False)
    talking = _listener(accepted, "http", speak_http=True)
    silent_port = await anext(silent)
    talking_port = await anext(talking)
    closed_port = _closed_port()
    try:
        settings = LlmSettings(ollama_allowed_hosts=frozenset({"127.0.0.1:11434"}))
        outcomes: list[str] = []
        for port in (closed_port, silent_port, talking_port):
            with pytest.raises(ProviderNotConfigured) as raised:
                client = build_guarded_client(f"http://127.0.0.1:{port}", settings)
                await client.get_json("/api/version", context=_CONTEXT, provider_label="Ollama")
            outcomes.append(str(raised.value).replace(str(port), "<port>"))

        assert accepted == [], "the guard dialled a port the operator never declared"
        assert len(set(outcomes)) == 1, f"the answers still tell the ports apart: {outcomes}"
        assert "11434" not in outcomes[0]
    finally:
        await silent.aclose()
        await talking.aclose()


async def test_the_declared_endpoint_is_still_reached_for_real() -> None:
    """The control of the test above: without this, "nothing connected" proves nothing."""
    accepted: list[str] = []
    talking = _listener(accepted, "http", speak_http=True)
    port = await anext(talking)
    try:
        settings = LlmSettings(ollama_allowed_hosts=frozenset({f"127.0.0.1:{port}"}))
        client = build_guarded_client(f"http://127.0.0.1:{port}", settings)
        result = await client.get_json("/api/version", context=_CONTEXT, provider_label="Ollama")
    finally:
        await talking.aclose()

    assert accepted == ["http"]
    assert isinstance(result, HttpFailure)
    assert result.status == 404


# --------------------------------------------------------------------------- #
# AUD-028: the pinning check compares sets, and how
# --------------------------------------------------------------------------- #


async def test_an_answer_that_merely_overlaps_the_pinned_set_is_refused() -> None:
    """A hostile resolver answering ``[allowed, hostile]`` used to pass.

    The check required a non-empty intersection, so adding an address was enough to
    satisfy it; the connection could then leave for the added one. Requiring the
    current answer to be contained in the pinned set removes that.
    """

    async def widening_resolver(host: str, port: int) -> frozenset[str]:
        return frozenset({"10.89.0.7", "169.254.169.254"})

    client = GuardedHttpClient(
        httpx.URL("http://ollama:11434"),
        _ALLOWED,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        resolver=widening_resolver,
        pinned_addresses=frozenset({"10.89.0.7"}),
    )
    with pytest.raises(ProviderNotConfigured) as raised:
        await client.post_json("/api/chat", {}, context=_CONTEXT, provider_label="Ollama")
    assert "rebinding" in str(raised.value)
