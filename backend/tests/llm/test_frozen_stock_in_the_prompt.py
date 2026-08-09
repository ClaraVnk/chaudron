"""What the model is told about the freezer, and what it is never asked to enforce.

Point (c) of the case: *frozen meat is not the same thing*. A frozen ingredient is
real stock and stays in the document -- a household with a full freezer and an
empty fridge that is told it owns nothing has been lied to just as surely as one
whose chicken is called fresh. What it is not is available tonight, and the
difference between those two facts is an evening.

The division this file pins down matters more than the wording it checks. The
model is told what makes a suggestion *useful*: plan the thaw, do not plan to
refreeze. The two rules that can hurt somebody are not delegated to it at all --
refusing the freezer to a thawed lot is a service refusal, and the three days it
then has are computed in SQL and applied by every filter in the application. This
is the same split ADR-0009 draws between allergens, which are a filter, and
preferences, which are prompt text.
"""

from __future__ import annotations

import json
from typing import Any

from chaudron.domain.llm_ports import InventoryItem, RecipeRequest
from chaudron.infra.llm.prompts import (
    DATA_BLOCK_CLOSE,
    DATA_BLOCK_OPEN,
    PROMPT_VERSION,
    RECIPE_SYSTEM_PROMPT,
    recipe_user_prompt,
)


def _document(inventory: tuple[InventoryItem, ...]) -> dict[str, Any]:
    prompt = recipe_user_prompt(RecipeRequest(inventory=inventory), inventory)
    lines = prompt.split("\n")
    opened = lines.index(DATA_BLOCK_OPEN)
    assert lines.index(DATA_BLOCK_CLOSE) == opened + 2
    parsed: dict[str, Any] = json.loads(lines[opened + 1])
    return parsed


def test_a_frozen_item_is_marked_as_frozen() -> None:
    frozen = InventoryItem(name="Blanc de poulet", quantity="1", unit="kg", frozen=True)

    document = _document((frozen,))

    assert document["inventory"] == [
        {"name": "Blanc de poulet", "quantity": "1", "unit": "kg", "frozen": True}
    ]


def test_a_frozen_item_is_still_in_the_inventory() -> None:
    """Hiding it would be a different lie from calling it fresh, not a safer one."""
    frozen = InventoryItem(name="Blanc de poulet", frozen=True, expires_in_days=90)
    fresh = InventoryItem(name="Courgettes", expires_in_days=2)

    names = [entry["name"] for entry in _document((frozen, fresh))["inventory"]]

    assert names == ["Blanc de poulet", "Courgettes"]


def test_stock_that_is_not_frozen_carries_no_flag() -> None:
    """Three hundred ``"frozen": false`` is three hundred lines of tokens nobody reads."""
    document = _document((InventoryItem(name="Courgettes", expires_in_days=2),))

    assert document["inventory"] == [{"name": "Courgettes", "expires_in_days": 2}]


def test_the_system_prompt_states_what_thawing_costs() -> None:
    """The rule sits in the cached prefix, so it costs nothing per call.

    Asserted on substance rather than on wording: a suggestion that treats a
    frozen breast as twenty minutes away is the failure, and the instruction has
    to say the three things that prevent it -- thaw first, refrigerated, and never
    put it back.
    """
    assert "frozen" in RECIPE_SYSTEM_PROMPT
    assert "thaw" in RECIPE_SYSTEM_PROMPT
    assert "refrigerator" in RECIPE_SYSTEM_PROMPT
    assert "never refrozen" in RECIPE_SYSTEM_PROMPT
    assert "cannot be cooked tonight" in RECIPE_SYSTEM_PROMPT


def test_the_instruction_is_in_the_stable_prefix_and_not_in_the_user_turn() -> None:
    """A rule interpolated per call is a rule paid for per call, and evictable.

    The same argument as the untrusted-data paragraph above it: instructions live
    in the cached, byte-stable half so a large inventory cannot push them out of
    the window.
    """
    frozen = InventoryItem(name="Blanc de poulet", frozen=True)

    assert "thaw" not in recipe_user_prompt(RecipeRequest(inventory=(frozen,)), (frozen,))


def test_the_prompt_version_records_the_change() -> None:
    """ "The suggestions got worse last week" is unanswerable without this."""
    assert PROMPT_VERSION == "recipes-4"
