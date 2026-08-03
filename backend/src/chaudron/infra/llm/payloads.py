"""The wire shapes the models are asked for, and the strict readers that accept them.

One schema per feature, shared by every adapter. Providers that support constrained
decoding are handed it verbatim; providers that do not are asked for it in prose and
answer into the very same reader (ADR-0005, "emulation with a documented loss").
That symmetry is the point: the emulated path is *less reliable*, never *differently
shaped*, so a malformed answer fails in one place with one error type.

Validation here is hand-written rather than delegated to a JSON-Schema library. The
schemas are small, fixed and reviewed; a dependency whose failure messages we would
have to sanitise anyway (they quote the offending payload) is not worth its cost.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from chaudron.domain.llm_ports import (
    ParsedReceipt,
    ProviderContext,
    ProviderResponseInvalid,
    ReceiptLine,
    RecipeIngredient,
    RecipeSuggestion,
)
from chaudron.infra.llm.redaction import snippet

__all__ = [
    "RECEIPT_SCHEMA",
    "RECIPE_SCHEMA",
    "read_receipt",
    "read_recipes",
]

#: ``additionalProperties: false`` and an explicit ``required`` everywhere: that is
#: what makes a schema usable for strict constrained decoding on the providers that
#: offer it, and it costs nothing on the ones that do not.
RECIPE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "ingredients", "steps"],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": ["string", "null"]},
                    "duration_minutes": {"type": ["integer", "null"]},
                    "servings": {"type": ["integer", "null"]},
                    "uses_expiring_soon": {"type": "boolean"},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "amount": {"type": ["string", "null"]},
                                "unit": {"type": ["string", "null"]},
                                "in_stock": {"type": "boolean"},
                            },
                        },
                    },
                    "steps": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}

RECEIPT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lines"],
    "properties": {
        "merchant": {"type": ["string", "null"]},
        "purchased_on": {"type": ["string", "null"], "description": "ISO-8601 date"},
        "currency": {"type": ["string", "null"], "description": "ISO-4217 code"},
        "total": {"type": ["string", "null"], "description": "decimal as a string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label"],
                "properties": {
                    "label": {"type": "string"},
                    "quantity": {"type": ["string", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "unit_price": {"type": ["string", "null"]},
                    "total_price": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def _invalid(reason: str, context: ProviderContext | None) -> ProviderResponseInvalid:
    return ProviderResponseInvalid(reason, context=context)


def _load(raw: str, context: ProviderContext | None) -> dict[str, Any]:
    """Parse the outermost JSON object, tolerating the fences models like to add.

    A model told to answer in JSON very often answers with a ```json block around
    it. Rejecting that would fail a response that is otherwise perfectly good, so
    the outermost braces are located rather than the string trusted whole. Anything
    beyond that -- prose instead of JSON, a truncated object -- is a real failure.
    """
    text = raw.strip()
    if not text:
        raise _invalid("provider returned an empty response", context)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise _invalid(
            f"provider response is not JSON: {snippet(text)}",
            context,
        )
    try:
        parsed: object = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise _invalid(
            f"provider response is not valid JSON ({exc.msg} at position {exc.pos})",
            context,
        ) from None
    if not isinstance(parsed, dict):
        raise _invalid("provider response is not a JSON object", context)
    return parsed


def _require_list(payload: dict[str, Any], key: str, context: ProviderContext | None) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise _invalid(f"provider response is missing the {key!r} array", context)
    return value


def _optional_str(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _optional_decimal(value: object) -> Decimal | None:
    """Money and quantities, read leniently but never wrongly.

    Anything that is not unambiguously a number becomes ``None`` rather than a
    guess: a receipt line whose price we could not read is a line the user will be
    asked to confirm, whereas an invented price silently corrupts a budget.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        # A French-locale model writes thin or non-breaking spaces as
        # thousands separators and a comma as the decimal point.
        text = "".join(c for c in value.strip() if not c.isspace())
        text = text.replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _optional_date(value: object) -> dt.date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def read_recipes(raw: str, *, context: ProviderContext | None = None) -> list[RecipeSuggestion]:
    """Turn a model answer into domain objects, or raise :class:`ProviderResponseInvalid`."""
    payload = _load(raw, context)
    entries = _require_list(payload, "suggestions", context)
    suggestions: list[RecipeSuggestion] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _invalid(f"suggestion #{index} is not an object", context)
        title = _optional_str(entry.get("title"))
        if title is None:
            raise _invalid(f"suggestion #{index} has no title", context)
        ingredients: list[RecipeIngredient] = []
        for item in entry.get("ingredients") or []:
            if not isinstance(item, dict):
                continue
            name = _optional_str(item.get("name"))
            if name is None:
                continue
            ingredients.append(
                RecipeIngredient(
                    name=name,
                    amount=_optional_str(item.get("amount")),
                    unit=_optional_str(item.get("unit")),
                    in_stock=bool(item.get("in_stock", False)),
                )
            )
        steps = tuple(
            step.strip()
            for step in entry.get("steps") or []
            if isinstance(step, str) and step.strip()
        )
        if not steps:
            raise _invalid(f"suggestion {title!r} has no steps", context)
        suggestions.append(
            RecipeSuggestion(
                title=title,
                summary=_optional_str(entry.get("summary")),
                duration_minutes=_optional_int(entry.get("duration_minutes")),
                servings=_optional_int(entry.get("servings")),
                ingredients=tuple(ingredients),
                steps=steps,
                uses_expiring_soon=bool(entry.get("uses_expiring_soon", False)),
            )
        )
    if not suggestions:
        raise _invalid("provider returned no usable suggestion", context)
    return suggestions


def read_receipt(raw: str, *, context: ProviderContext | None = None) -> ParsedReceipt:
    """Turn a model answer into a :class:`ParsedReceipt`, or raise.

    An empty ``lines`` array is accepted: an unreadable photograph is a legitimate
    outcome the user must be shown, and inventing a line to avoid an empty screen
    would be far worse than an honest "nothing readable here".
    """
    payload = _load(raw, context)
    entries = _require_list(payload, "lines", context)
    lines: list[ReceiptLine] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _invalid(f"receipt line #{index} is not an object", context)
        label = _optional_str(entry.get("label"))
        if label is None:
            raise _invalid(f"receipt line #{index} has no label", context)
        lines.append(
            ReceiptLine(
                label=label,
                quantity=_optional_decimal(entry.get("quantity")),
                unit=_optional_str(entry.get("unit")),
                unit_price=_optional_decimal(entry.get("unit_price")),
                total_price=_optional_decimal(entry.get("total_price")),
            )
        )
    currency = _optional_str(payload.get("currency"))
    return ParsedReceipt(
        lines=tuple(lines),
        merchant=_optional_str(payload.get("merchant")),
        purchased_on=_optional_date(payload.get("purchased_on")),
        currency=currency.upper()[:3] if currency else None,
        total=_optional_decimal(payload.get("total")),
    )
