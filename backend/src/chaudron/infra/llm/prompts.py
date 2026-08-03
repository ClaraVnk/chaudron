"""Prompt construction, shared by every adapter.

The split enforced here is the one prompt caching depends on: a **stable** part
(instructions and output contract, byte-identical across every household and every
call) followed by a **volatile** part (this household's stock, right now). Caching
is a prefix match, so anything volatile placed before the cut point silently
invalidates the cache for everything after it -- with no error, only a bill.

Nothing below is provider-specific. Where a provider can be told to obey the schema
natively it is; where it cannot, :func:`emulation_clause` is appended and the answer
is validated server-side instead.
"""

from __future__ import annotations

import json
from typing import Final

from chaudron.domain.llm_ports import InventoryItem, RecipeRequest
from chaudron.infra.llm.payloads import RECEIPT_SCHEMA, RECIPE_SCHEMA

__all__ = [
    "PROMPT_VERSION",
    "RECEIPT_SYSTEM_PROMPT",
    "RECIPE_SYSTEM_PROMPT",
    "emulation_clause",
    "receipt_user_prompt",
    "recipe_user_prompt",
    "trim_inventory",
]

#: Stored alongside every generated suggestion. Without it, "the suggestions got
#: worse last week" is unanswerable.
PROMPT_VERSION: Final = "recipes-1"

#: Stable prefix. Never interpolate anything here -- not a date, not a household
#: name, not a model id. Every byte of it is the cache key.
RECIPE_SYSTEM_PROMPT: Final = (
    "You plan home meals from what a household already owns.\n"
    "\n"
    "Rules:\n"
    "- Prefer recipes that use items closest to their expiry date.\n"
    "- Only mark an ingredient as in stock when it appears in the inventory given.\n"
    "- You may add up to three common pantry staples (salt, pepper, oil, water) "
    "that are not in the inventory; mark them as not in stock.\n"
    "- Never invent a quantity the household does not have.\n"
    "- Steps are short, imperative and ordered.\n"
    "- Answer in the language requested by the user turn.\n"
)

RECEIPT_SYSTEM_PROMPT: Final = (
    "You transcribe photographed till receipts into structured purchase lines.\n"
    "\n"
    "Rules:\n"
    "- Transcribe only what is legible. Never infer a product or a price.\n"
    "- Leave a field null when the receipt does not show it or you cannot read it.\n"
    "- Keep the merchant's own wording for each line label, abbreviations included.\n"
    "- Amounts are decimal strings with a dot separator and no currency symbol.\n"
    "- Ignore loyalty points, discounts applied to the whole basket, and the "
    "payment summary; keep only purchased items.\n"
)


def emulation_clause(schema: dict[str, object]) -> str:
    """The instructions that stand in for native constrained decoding.

    Documented loss (ADR-0005): the model is *asked* rather than *constrained*, so a
    share of answers come back malformed. The server validates and retries; the
    interface tells the user their configuration is on this path.
    """
    return (
        "\n"
        "Answer with a single JSON object and nothing else: no prose before it, no "
        "prose after it, no markdown fence. It must validate against this JSON "
        "Schema:\n" + json.dumps(schema, separators=(",", ":"), sort_keys=True) + "\n"
    )


def _format_item(item: InventoryItem) -> str:
    parts = [item.name]
    if item.quantity:
        parts.append(f"{item.quantity}{item.unit or ''}".strip())
    elif item.unit:
        parts.append(item.unit)
    if item.expires_in_days is not None:
        if item.expires_in_days < 0:
            parts.append(f"expired {abs(item.expires_in_days)}d ago")
        else:
            parts.append(f"expires in {item.expires_in_days}d")
    return " | ".join(parts)


def trim_inventory(
    inventory: tuple[InventoryItem, ...], *, keep: int
) -> tuple[tuple[InventoryItem, ...], bool]:
    """Keep the ``keep`` most useful items, most urgent first.

    Used only on the ``degraded`` path of the taxonomy, when the configured model's
    context is too small for the whole stock. The ordering is what makes the
    reduction defensible rather than arbitrary: what is about to spoil is exactly
    what the household most needs a recipe for.
    """
    if keep <= 0 or len(inventory) <= keep:
        return inventory, False
    ordered = sorted(
        inventory,
        key=lambda item: (
            item.expires_in_days is None,
            item.expires_in_days if item.expires_in_days is not None else 0,
        ),
    )
    return tuple(ordered[:keep]), True


def recipe_user_prompt(request: RecipeRequest, inventory: tuple[InventoryItem, ...]) -> str:
    """The volatile half: this household's stock and constraints, this minute.

    Rendered *after* the cache breakpoint. ``inventory`` is passed separately from
    ``request`` because the degraded path sends a subset of it.
    """
    lines = [
        f"Language: {request.language}",
        f"Suggestions requested: {request.max_suggestions}",
    ]
    if request.servings:
        lines.append(f"Servings: {request.servings}")
    if request.notes:
        lines.append(f"Constraints: {request.notes}")
    lines.append("")
    if inventory:
        lines.append("Inventory (name | quantity | expiry):")
        lines.extend(f"- {_format_item(item)}" for item in inventory)
    else:
        lines.append("Inventory: empty.")
    return "\n".join(lines)


def receipt_user_prompt() -> str:
    return (
        "Transcribe this till receipt. Return every purchased line in the order it "
        "appears on the ticket."
    )


#: Convenience re-exports so an adapter needs one import for a whole feature.
RECIPE_JSON_SCHEMA: Final = RECIPE_SCHEMA
RECEIPT_JSON_SCHEMA: Final = RECEIPT_SCHEMA
