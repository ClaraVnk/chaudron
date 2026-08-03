"""What the Anthropic adapter actually puts on the wire.

Two of these assertions are the difference between using a paid feature and merely
believing you are:

* the cache breakpoint has to sit at the end of the **stable** prefix, with the
  household's stock after it. Caching is a prefix match, so an inventory placed
  before the cut point invalidates the cache on every single call -- silently, with
  no error and no signal other than the bill;
* the schema has to reach ``output_config.format``, or the model is merely *asked*
  to comply and the emulated path's failure rate has been imported for nothing.
"""

from __future__ import annotations

import json
from typing import Any

from chaudron.domain.llm_ports import InventoryItem, ProviderCapabilities, RecipeRequest
from chaudron.infra.llm.anthropic_provider import (
    AnthropicClientLike,
    AnthropicTransport,
    anthropic_capabilities,
)
from chaudron.infra.llm.base import ModelReceiptParser, ModelRecipeGenerator
from chaudron.infra.llm.doubles import valid_receipt_payload, valid_recipe_payload


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Message:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]
        self.stop_reason = "end_turn"


class _CapturingMessages:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _Message(self.answer)


class _CapturingClient:
    def __init__(self, answer: str) -> None:
        self._messages = _CapturingMessages(answer)

    @property
    def messages(self) -> _CapturingMessages:
        return self._messages


def _request() -> RecipeRequest:
    return RecipeRequest(
        inventory=(InventoryItem(name="Courgettes", quantity="600", unit="g"),),
        max_suggestions=2,
    )


async def _call_recipes(
    capabilities: ProviderCapabilities,
) -> tuple[dict[str, Any], _CapturingClient]:
    client = _CapturingClient(valid_recipe_payload())
    transport = AnthropicTransport(client, capabilities, api_key="sk-ant-test")
    await ModelRecipeGenerator(transport).suggest(_request())
    return client.messages.calls[0], client


async def test_the_cache_breakpoint_sits_at_the_end_of_the_stable_prefix() -> None:
    payload, _ = await _call_recipes(anthropic_capabilities("claude-opus-5"))

    system = payload["system"]
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}

    # The volatile half is in the user turn, i.e. *after* the breakpoint.
    stable = system[0]["text"]
    assert "Courgettes" not in stable
    user_text = payload["messages"][0]["content"][-1]["text"]
    assert "Courgettes" in user_text


async def test_no_cache_directive_when_the_model_cannot_cache() -> None:
    capabilities = ProviderCapabilities(
        provider="anthropic",
        model="claude-opus-5",
        context_window=1_000_000,
        supports_structured_output=True,
        supports_vision=True,
        supports_prompt_caching=False,
    )
    payload, _ = await _call_recipes(capabilities)
    assert "cache_control" not in payload["system"][0]


async def test_the_schema_is_handed_to_constrained_decoding() -> None:
    payload, _ = await _call_recipes(anthropic_capabilities("claude-opus-5"))
    output_format = payload["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert "suggestions" in output_format["schema"]["properties"]
    # ...and therefore the prompt does not also beg for JSON.
    assert "JSON Schema" not in payload["system"][0]["text"]


async def test_the_stable_prefix_is_identical_across_households() -> None:
    """Any per-household byte in the prefix would make the cache per-household."""
    first, _ = await _call_recipes(anthropic_capabilities("claude-opus-5"))

    client = _CapturingClient(valid_recipe_payload())
    transport = AnthropicTransport(
        client, anthropic_capabilities("claude-opus-5"), api_key="sk-ant-other"
    )
    await ModelRecipeGenerator(transport).suggest(
        RecipeRequest(inventory=(InventoryItem(name="Pommes"),), notes="vegan")
    )
    second = client.messages.calls[0]

    assert first["system"] == second["system"]


async def test_the_receipt_image_travels_as_base64_before_the_instruction() -> None:
    client = _CapturingClient(valid_receipt_payload())
    transport = AnthropicTransport(
        client, anthropic_capabilities("claude-opus-5"), api_key="sk-ant-test"
    )
    receipt = await ModelReceiptParser(transport).parse(b"\x89PNG fake", "image/png")
    assert receipt.merchant == "Coop"
    assert receipt.lines[0].label == "COURGETTES BIO"

    content = client.messages.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[-1]["type"] == "text"


async def test_the_key_is_never_part_of_the_payload() -> None:
    """It belongs in the client's headers, never in anything we serialise or log."""
    payload, _ = await _call_recipes(anthropic_capabilities("claude-opus-5"))
    assert "sk-ant-test" not in json.dumps(payload)


def test_the_adapter_satisfies_both_ports_structurally() -> None:
    from chaudron.domain.llm_ports import ReceiptParser, RecipeGenerator

    client: AnthropicClientLike = _CapturingClient("")
    transport = AnthropicTransport(client, anthropic_capabilities("claude-opus-5"), api_key="k")
    generator: RecipeGenerator = ModelRecipeGenerator(transport)
    parser: ReceiptParser = ModelReceiptParser(transport)
    assert isinstance(generator, RecipeGenerator)
    assert isinstance(parser, ReceiptParser)
