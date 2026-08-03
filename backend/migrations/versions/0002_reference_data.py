"""seed the global reference tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03 20:40:00.000000+00:00

``unit`` and ``llm_provider`` are global reference tables, not tenant data. They
are seeded by a migration rather than by an application bootstrap so the content
is versioned, reproducible and identical in CI, in development and in
production: an inventory whose conversion factors depend on which environment
first started the process is an inventory nobody can reconcile.

``unit`` in particular is load-bearing. ``factor_to_canonical`` is frozen into
every lot at write time (``docs/data-model.md`` section 6.3): correcting a wrong
factor later requires a data migration, not an ``UPDATE`` of this table.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# code, dimension, factor to canonical, symbol, canonical?, sort order
_UNITS: tuple[tuple[str, str, str, str, bool, int], ...] = (
    ("g", "mass", "1", "g", True, 10),
    ("kg", "mass", "1000", "kg", False, 20),
    ("mg", "mass", "0.001", "mg", False, 30),
    ("ml", "volume", "1", "ml", True, 10),
    ("cl", "volume", "10", "cl", False, 20),
    ("dl", "volume", "100", "dl", False, 30),
    ("l", "volume", "1000", "L", False, 40),
    # Cooking measures, French convention: 15 ml and 5 ml. Approximate by nature,
    # and that is fine -- they exist so "a tablespoon of oil" can be recorded at
    # all rather than forcing a guess in millilitres at entry time.
    ("tbsp", "volume", "15", "c. à s.", False, 50),
    ("tsp", "volume", "5", "c. à c.", False, 60),
    ("piece", "count", "1", "pc", True, 10),
)

# code, display name, needs key, needs base url, default model, vision, structured, context
_PROVIDERS: tuple[tuple[str, str, bool, bool, str | None, bool, bool, int | None, int], ...] = (
    ("anthropic", "Anthropic", True, False, "claude-opus-5", True, True, 200_000, 10),
    ("openai", "OpenAI", True, False, "gpt-5", True, True, 128_000, 20),
    ("gemini", "Google Gemini", True, False, "gemini-2.5-pro", True, True, 1_000_000, 30),
    ("mistral", "Mistral", True, False, "mistral-large-latest", False, True, 128_000, 40),
    # Capabilities depend on the pulled model, not on the endpoint: these
    # defaults are pessimistic, and the household's configuration corrects them
    # from a probe (ADR-0005).
    ("ollama", "Ollama (self-hosted)", False, True, None, False, False, None, 50),
)


def upgrade() -> None:
    unit = sa.table(
        "unit",
        sa.column("code", sa.String),
        sa.column("dimension", sa.Enum(name="quantity_dimension")),
        sa.column("factor_to_canonical", sa.Numeric),
        sa.column("symbol", sa.String),
        sa.column("is_canonical", sa.Boolean),
        sa.column("sort_order", sa.SmallInteger),
    )
    op.bulk_insert(
        unit,
        [
            {
                "code": code,
                "dimension": dimension,
                "factor_to_canonical": Decimal(factor),
                "symbol": symbol,
                "is_canonical": is_canonical,
                "sort_order": sort_order,
            }
            for code, dimension, factor, symbol, is_canonical, sort_order in _UNITS
        ],
    )

    provider = sa.table(
        "llm_provider",
        sa.column("code", sa.String),
        sa.column("display_name", sa.String),
        sa.column("requires_api_key", sa.Boolean),
        sa.column("requires_base_url", sa.Boolean),
        sa.column("default_model", sa.String),
        sa.column("default_supports_vision", sa.Boolean),
        sa.column("default_supports_structured_output", sa.Boolean),
        sa.column("default_max_context_tokens", sa.Integer),
        sa.column("is_enabled", sa.Boolean),
        sa.column("sort_order", sa.SmallInteger),
    )
    op.bulk_insert(
        provider,
        [
            {
                "code": code,
                "display_name": display_name,
                "requires_api_key": requires_api_key,
                "requires_base_url": requires_base_url,
                "default_model": default_model,
                "default_supports_vision": vision,
                "default_supports_structured_output": structured,
                "default_max_context_tokens": context,
                "is_enabled": True,
                "sort_order": sort_order,
            }
            for (
                code,
                display_name,
                requires_api_key,
                requires_base_url,
                default_model,
                vision,
                structured,
                context,
                sort_order,
            ) in _PROVIDERS
        ],
    )


def downgrade() -> None:
    """Remove only the rows this revision inserted.

    A blanket ``DELETE`` would take units added by a later revision with it, and
    the foreign keys onto ``unit`` would then refuse the deletion in a way that
    looks like a bug in the rollback rather than in this function.
    """
    op.execute(
        sa.text("DELETE FROM llm_provider WHERE code IN :codes").bindparams(
            sa.bindparam("codes", tuple(row[0] for row in _PROVIDERS), expanding=True)
        )
    )
    op.execute(
        sa.text("DELETE FROM unit WHERE code IN :codes").bindparams(
            sa.bindparam("codes", tuple(row[0] for row in _UNITS), expanding=True)
        )
    )
