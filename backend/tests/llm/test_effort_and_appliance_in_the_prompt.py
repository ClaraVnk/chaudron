"""The two newest preferences must reach the prompt — and only when asked.

Both are *preferences* in the sense contract 4ter gives the word: composed by
the server, transmitted to the model, never applied as a filter. Nothing in any
catalogue says how long a recipe takes or which machine it suits, so there is no
predicate to write and nothing to verify afterwards.

What can be verified is the half that is ours: that asking produces the
instruction, and that not asking produces silence. A preference that leaks into
every prompt is worse than one that never arrives — it quietly rewrites requests
nobody made.
"""

from __future__ import annotations

import pytest

from chaudron.domain.llm_ports import InventoryItem, RecipeRequest
from chaudron.infra.llm.prompts import recipe_user_prompt

INVENTORY = (InventoryItem(name="Pommes"),)


def _prompt(**kwargs: str) -> str:
    return recipe_user_prompt(RecipeRequest(inventory=INVENTORY, **kwargs), INVENTORY)


def test_defaults_say_nothing_about_effort_or_appliance() -> None:
    """Silence is the default, and it is the assertion that matters most.

    `any` and `none` must add no line at all: a household that did not choose
    should get the prompt it got before these options existed.
    """
    prompt = _prompt()
    assert "least possible work" not in prompt
    for machine in ("Thermomix", "Monsieur Cuisine", "Cookeo", "Instant Pot"):
        assert machine not in prompt


def test_quick_carries_all_three_budgets() -> None:
    """Not just "be quick".

    A dish with eleven ingredients in four pans is not quick however fast each
    step is: the work being avoided is the chopping and the washing-up as much
    as the clock. If a later edit collapses this into a single duration, this
    test is what says so.
    """
    prompt = _prompt(effort="quick")
    assert "30 minutes" in prompt
    assert "6 ingredients" in prompt
    assert "5 steps" in prompt


@pytest.mark.parametrize(
    ("appliance", "expected"),
    [
        ("thermomix", "Thermomix"),
        ("monsieur_cuisine", "Monsieur Cuisine"),
        ("cookeo", "Cookeo"),
        ("instant_pot", "Instant Pot"),
    ],
)
def test_each_appliance_is_named_and_alone(appliance: str, expected: str) -> None:
    """Named specifically, and never two at once.

    "Use their robot" would let the model invent an interface: a Thermomix step
    is a speed, a time and a temperature; a Cookeo step is a pressure programme
    with different timings. And a prompt naming two machines asks for a recipe
    for neither.
    """
    prompt = _prompt(appliance=appliance)
    assert expected in prompt
    others = {"Thermomix", "Monsieur Cuisine", "Cookeo", "Instant Pot"} - {expected}
    # "Thermomix" appears inside the Monsieur Cuisine instruction on purpose
    # (it warns against assuming Thermomix-only programmes), so that one pair is
    # allowed to overlap.
    if appliance != "monsieur_cuisine":
        for other in others:
            assert other not in prompt


def test_the_appliance_never_changes_what_is_cooked() -> None:
    """The instruction has to say so itself.

    Without that sentence, "write the steps for a Cookeo" reads as "propose
    Cookeo recipes", and the machine silently becomes a filter on a catalogue
    that has no such column.
    """
    prompt = _prompt(appliance="cookeo")
    assert "does not change" in prompt


def test_both_at_once() -> None:
    prompt = _prompt(effort="quick", appliance="thermomix")
    assert "least possible work" in prompt
    assert "Thermomix" in prompt
