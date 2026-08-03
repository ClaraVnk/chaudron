"""The Ollama URL is user-supplied and server-dialled: these are its guard rails.

ADR-0007 calls this out as a textbook SSRF primitive whose usual mitigation --
rejecting private ranges -- does not apply, because a co-located Ollama's address
*is* private. What replaces it is asserted here.
"""

from __future__ import annotations

import httpx
import pytest

from chaudron.domain.llm_ports import ProviderContext, ProviderNotConfigured
from chaudron.infra.llm.http import GuardedHttpClient, validate_ollama_base_url
from chaudron.infra.llm.settings import ALLOWED_HOSTS_ENV_VAR, LlmSettings

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
        ("http://169.254.169.254/latest/meta-data", "allowlist"),
        ("http://localhost:11434", "allowlist"),
        ("http://ollama.evil.example", "allowlist"),
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
