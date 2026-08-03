"""The probe is what makes Ollama's capability declaration a fact rather than a guess.

ADR-0005 calls the probed declaration "a source of bugs of its own": it depends on a
third-party instance being reachable at configuration time, the household can change
model afterwards without telling us, and the answer ages. All three consequences show
up here -- what a probe reads, what it stamps, and what it does when the instance
will not answer.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from chaudron.domain.llm_ports import (
    CapabilitySource,
    ProviderNotConfigured,
    ProviderResponseInvalid,
    ProviderUnavailable,
)
from chaudron.infra.llm.ollama_provider import build_guarded_client, probe_capabilities
from chaudron.infra.llm.settings import LlmSettings

_SETTINGS = LlmSettings(ollama_allowed_hosts=frozenset({"ollama"}))


def _instance(
    *,
    version: str = "0.5.4",
    capabilities: list[str] | None = None,
    context_length: int | None = 8192,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": version})
        if request.url.path == "/api/show":
            info = {"general.architecture": "llama"}
            if context_length is not None:
                info["llama.context_length"] = context_length  # type: ignore[assignment]
            return httpx.Response(
                200,
                json={
                    "capabilities": capabilities if capabilities is not None else ["completion"],
                    "model_info": info,
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> object:
    return build_guarded_client("http://ollama:11434", _SETTINGS, transport=transport)


async def test_probe_reads_vision_and_context_from_the_instance() -> None:
    probed = await probe_capabilities(
        _client(_instance(capabilities=["completion", "vision"], context_length=131072)),  # type: ignore[arg-type]
        "llava",
        now=dt.datetime(2026, 3, 3, tzinfo=dt.UTC),
    )
    assert probed.supports_vision is True
    assert probed.context_window == 131072
    assert probed.supports_long_context is True
    assert probed.source is CapabilitySource.PROBED
    assert probed.probed_at == dt.datetime(2026, 3, 3, tzinfo=dt.UTC)


async def test_a_text_only_model_is_declared_without_vision() -> None:
    probed = await probe_capabilities(_client(_instance()), "llama3.2")  # type: ignore[arg-type]
    assert probed.supports_vision is False
    assert probed.supports_long_context is False


async def test_structured_output_follows_the_server_version_not_the_model() -> None:
    """``format`` is enforced by the runtime, so the capability is the server's."""
    old = await probe_capabilities(_client(_instance(version="0.3.14")), "llama3.2")  # type: ignore[arg-type]
    new = await probe_capabilities(_client(_instance(version="0.5.4")), "llama3.2")  # type: ignore[arg-type]
    assert old.supports_structured_output is False
    assert new.supports_structured_output is True


async def test_prompt_caching_is_never_claimed_for_ollama() -> None:
    probed = await probe_capabilities(_client(_instance()), "llama3.2")  # type: ignore[arg-type]
    assert probed.supports_prompt_caching is False


async def test_an_unreported_context_length_falls_back_low_not_high() -> None:
    """Assuming a window we were not told about is how degraded mode stops applying."""
    probed = await probe_capabilities(_client(_instance(context_length=None)), "llama3.2")  # type: ignore[arg-type]
    assert probed.context_window == 4096


async def test_an_instance_too_old_to_answer_fails_the_save() -> None:
    with pytest.raises(ProviderResponseInvalid) as raised:
        await probe_capabilities(_client(_instance(capabilities=[])), "llama3.2")  # type: ignore[arg-type]
    assert "too old" in str(raised.value)


async def test_a_missing_model_names_the_command_that_fixes_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.4"})
        return httpx.Response(404, json={"error": "model 'llava' not found"})

    with pytest.raises(ProviderNotConfigured) as raised:
        await probe_capabilities(_client(httpx.MockTransport(handler)), "llava")  # type: ignore[arg-type]
    assert "ollama pull llava" in str(raised.value)


async def test_an_unreachable_instance_fails_rather_than_defaulting() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderUnavailable):
        await probe_capabilities(_client(httpx.MockTransport(handler)), "llama3.2")  # type: ignore[arg-type]
