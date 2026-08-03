"""make every llm_provider row agree with the adapter that serves it

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03 23:10:00.000000+00:00

Revision ``0002`` seeded three ``default_model`` values that no adapter in
``chaudron.infra.llm`` recognises: ``gpt-5`` for OpenAI, ``gemini-2.5-pro`` for
Gemini and ``mistral-large-latest`` for Mistral AI. The capability tables are
hand-maintained and refuse an unknown model rather than assuming it capable
(ADR-0005), so a household that accepted any of those defaults had its configuration
rejected as "unknown model" -- the recommended default was unusable by construction.

**The data is corrected, not the tables.** Adding those three names to the adapter
tables would mean asserting four capability facts about each (context window, vision,
structured output, prompt caching) that this instance cannot verify, and a table that
promises vision a model does not have produces a fabricated shopping list rather than
an error. The adapters are the source of truth for what the running code supports;
the seed is data, and data is what bends.

Each replacement was chosen to match the capability columns ``0002`` already declared,
so the row stays internally consistent. Two deliberate exceptions, both of which
correct the *capabilities* rather than the model:

* Mistral AI moves to ``pixtral-large-latest`` and gains ``default_supports_vision``.
  The alternative, ``mistral-small-latest``, is the conformance suite's reference
  *degraded* configuration (no vision, 32k context), which would leave receipt import
  unavailable on the one provider that keeps a household's data under EU jurisdiction.
* Anthropic keeps ``claude-opus-5`` -- the model ADR-0005 names, and one the adapter
  does know -- but its declared context window was 200k where the model has 1M.
  Understating it makes the recipe prompt trim an inventory that would have fitted.

``0002`` is already applied on running databases, so this is an additive revision
rather than an edit to it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# One row per provider whose seeded description contradicted its adapter, as
# (code, model, supports_vision, max_context_tokens) before and after.
type _Row = tuple[str, bool, int]

_CORRECTIONS: tuple[tuple[str, _Row, _Row], ...] = (
    # The model was right; only the context window was understated.
    ("anthropic", ("claude-opus-5", True, 200_000), ("claude-opus-5", True, 1_000_000)),
    # gpt-4o: 128k, vision, structured output -- exactly what 0002 declared.
    ("openai", ("gpt-5", True, 128_000), ("gpt-4o", True, 128_000)),
    # `-flash` rather than `-pro`: it matches the 1M context 0002 declared, and it is
    # the cheaper of the two for a household paying its own bill (ADR-0007).
    # `gemini-1.5-pro` stays selectable; only the pre-filled default changes.
    ("gemini", ("gemini-2.5-pro", True, 1_000_000), ("gemini-1.5-flash", True, 1_000_000)),
    ("mistral", ("mistral-large-latest", False, 128_000), ("pixtral-large-latest", True, 128_000)),
)

_UPDATE = sa.text(
    "UPDATE llm_provider"
    " SET default_model = :model,"
    " default_supports_vision = :vision,"
    " default_max_context_tokens = :context"
    " WHERE code = :code"
)


def _apply(index: int) -> None:
    for correction in _CORRECTIONS:
        code = correction[0]
        model, vision, context = correction[index]
        op.execute(_UPDATE.bindparams(code=code, model=model, vision=vision, context=context))


def upgrade() -> None:
    _apply(2)


def downgrade() -> None:
    """Put back exactly what ``0002`` inserted.

    Restoring values the adapters reject is the correct rollback: ``down`` returns the
    database to the state the previous revision defines, and the instance running that
    revision is the one whose code produced them.
    """
    _apply(1)
