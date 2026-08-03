"""Reduce text a third party wrote to something that cannot forge structure.

The product catalogue is shared by every household -- ADR-0008 stores catalogue
rows with ``household_id IS NULL`` so a barcode is resolved once for the whole
instance -- and it is filled from Open Food Facts, a wiki anyone may edit. A
hostile contributor therefore writes into a table that every household scanning
that barcode reads, and that value reaches an LLM prompt (security audit,
AUD-006). It is not the household attacking itself: it is a stranger attacking
people they will never meet.

So catalogue text is untrusted input, and this is where it stops being handled as
if it were ours. What happens here is narrow on purpose:

* control and format characters are dropped -- C0/C1 controls, bidi overrides,
  private-use and, importantly, the Unicode tag block (U+E0000..U+E007F), which is
  the usual way to smuggle text a human reviewer cannot see past a field a model
  reads perfectly well;
* every run of whitespace collapses to one space, so a value can no longer contain
  a line break and therefore can no longer *look like* a new section of a prompt;
* the result is length-bounded, so one field cannot outweigh the instructions
  around it or the rest of the inventory.

There is no list of forbidden phrases, and there deliberately never will be:
"ignore previous instructions" has unbounded spellings and blocking a sample of
them buys a false sense of completion. What this module buys is narrower and
actually true: an untrusted value is afterwards **one bounded, single-line run of
text**. That is a structural property, and
:mod:`chaudron.infra.llm.prompts` is built on it.
"""

from __future__ import annotations

import unicodedata
from typing import Final

__all__ = ["ELLIPSIS", "sanitize", "sanitize_optional"]

#: Marks a value the bound cut, so a truncated label reads as truncated rather
#: than as a different product.
ELLIPSIS: Final = "…"

#: Unicode general categories removed outright. ``Cf`` is the load-bearing one: it
#: covers bidi overrides and the tag block used for invisible instruction
#: smuggling. ``Cn`` (unassigned) is deliberately *not* here -- it would delete
#: characters that are simply newer than this interpreter.
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs"})


def sanitize(value: str, *, limit: int) -> str:
    """Return ``value`` as a single bounded line, with invisible characters gone.

    ``limit`` is the ceiling on the returned length, ellipsis included, so the
    caller can reason about the size of what it is about to embed.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    # NFC, not NFKC: canonical composition settles the "e + combining grave" versus
    # "e-grave" question, which is a real duplicate-key problem, without rewriting
    # the characters an author actually chose.
    normalised = unicodedata.normalize("NFC", value)
    # Whitespace first, and this order is load-bearing. A newline is a ``Cc``
    # character: dropping controls before collapsing would *delete* it, welding
    # "Tomates\nIGNORE ALL..." into one word instead of separating two. Collapsing
    # first turns every break into a space. ``str.split()`` covers Unicode
    # whitespace, so U+2028 LINE SEPARATOR and U+0085 NEL -- the two a naive
    # ``replace("\n", " ")`` misses and a renderer still treats as breaks -- go too.
    collapsed = " ".join(normalised.split())
    # What is left is the invisible remainder: NUL, ESC, zero-width spaces, bidi
    # overrides, tag characters. None of them separate words, so removing them
    # outright is right -- and then collapse again, since removing a character
    # between two spaces leaves a double one.
    stripped = "".join(
        char for char in collapsed if unicodedata.category(char) not in _DROPPED_CATEGORIES
    )
    cleaned = " ".join(stripped.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + ELLIPSIS


def sanitize_optional(value: str | None, *, limit: int) -> str | None:
    """:func:`sanitize`, mapping "nothing left once cleaned" back to ``None``."""
    if value is None:
        return None
    cleaned = sanitize(value, limit=limit)
    return cleaned or None
