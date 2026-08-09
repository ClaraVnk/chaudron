"""one household, one live provider configuration -- and no purpose binding left to make

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-08 12:00:00.000000+00:00

A product decision, made structural. A household has **one** live
``llm_provider_config`` or none, and the schema is where that is settled rather
than a rule the service tries to remember.

What changes
------------

*``ix_llm_provider_config_household_active`` becomes
``uq_llm_provider_config_household_active``.* Same table, same column, same
partial predicate ``archived_at IS NULL`` -- now ``UNIQUE``, and renamed to the
prefix the naming convention on ``Base.metadata`` gives a unique index, exactly as
``uq_product_gtin_global`` is declared. PostgreSQL cannot turn an existing index
unique in place, so it is dropped and rebuilt.

The predicate is what keeps the rule liveable. Archived rows are excluded, so a
household can retire the configuration it has and register a different one; the
record of what was authorised, to whom and until when survives in the archive,
which is what an art. 15 request asks for. What the index forbids is two usable at
the same moment.

*``llm_purpose_binding`` is dropped.* Its entire job was to name *which of several*
configurations served recipe generation and which served receipt parsing. With one
configuration there is nothing to choose between, so no meaningful row can exist in
it -- and a table nothing can populate is debt with a foreign key attached.
``ProviderService._load`` no longer consults it; the two "several configurations and
no binding" refusals it justified are deleted with it, being unreachable.

The ``llm_purpose`` PostgreSQL enum goes with the table, its last user. The Python
:class:`chaudron.domain.models.LlmPurpose` **stays**: receipt parsing needs vision
and recipe generation does not, which is still a real distinction and still decides
whether a configuration can serve a request -- it simply no longer selects between
rows.

What a household loses, said plainly
------------------------------------

Two configurations made one arrangement possible that one does not: a local,
vision-less Ollama for recipes *and* a hosted multimodal provider for photographed
receipts. A household that wants both must now pick one.

Nothing breaks on the path it gives up. ``ProviderService.for_receipts`` refuses a
model without vision through ADR-0005's ``unavailable`` case, with the reason and
the remedy on the banner the household already sees (``_DEGRADED_REASONS``), and
importing a PDF order recap needs no model at all. The feature degrades honestly;
it is one fewer arrangement, not a silent failure. ``docs/data-model.md`` §4.13
records the same trade-off next to the index that causes it.

Upgrading a database that already holds two
-------------------------------------------

``CREATE UNIQUE INDEX`` aborts on a household with two live rows, and the message
PostgreSQL gives names one duplicated key rather than the situation. So the
duplicates are counted first and the upgrade refuses with the query an operator can
act on.

It refuses rather than repairing, and that is the same decision the create route
makes: archiving one of two working configurations means retiring a third party's
API key, which is not a choice a migration may take on a household's behalf.

Rollback
--------

``downgrade`` puts back the non-unique index under its old name, and recreates
``llm_purpose_binding`` as revision ``0001`` built it -- both foreign keys including
the composite ``(household_id, llm_provider_config_id)`` one that makes assigning
another household's configuration impossible at the database level, the composite
primary key, and the row-level security revision ``0004`` enables on it (dropping a
table drops its policy, so the policy has to be recreated, not merely re-enabled).
The rows themselves are gone: they named a choice that no longer exists, and a
database rolled back to ``0024`` has a purpose binding table with nothing in it,
which is what one that never had the feature configured also has.
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE: Final = "llm_provider_config"

#: The index this revision retires, and the one it installs. Both are partial on the
#: same predicate; only the name and the uniqueness differ.
_OLD_INDEX: Final = "ix_llm_provider_config_household_active"
_NEW_INDEX: Final = "uq_llm_provider_config_household_active"
_LIVE_ROW: Final = "archived_at IS NULL"

_BINDING_TABLE: Final = "llm_purpose_binding"
_BINDING_ENUM: Final = "llm_purpose"

#: Kept verbatim from revision ``0004``: the policy is recreated by ``downgrade``,
#: and a predicate that drifted would leave the restored table isolated by a
#: different rule than every other tenant table.
_TENANT_FUNCTION: Final = "chaudron_current_household"
_BINDING_POLICY: Final = f"{_BINDING_TABLE}_household_isolation"
_OWN_ROW: Final = f"household_id = {_TENANT_FUNCTION}()"

#: Names a household and how many live configurations it has, for the refusal below.
_DUPLICATES: Final = sa.text(
    f"""
    SELECT household_id, count(*) AS live
    FROM {_TABLE}
    WHERE {_LIVE_ROW}
    GROUP BY household_id
    HAVING count(*) > 1
    ORDER BY count(*) DESC
    """
)


def _refuse_existing_duplicates() -> None:
    """Fail before the ``CREATE UNIQUE INDEX`` does, with something actionable.

    PostgreSQL would say ``Key (household_id)=(...) is duplicated`` and name one
    household. This names all of them and says what to do, which is the difference
    between an operator who can finish the upgrade and one who has to reverse-engineer
    the intent from an index name.
    """
    offenders = op.get_bind().execute(_DUPLICATES).all()
    if not offenders:
        return
    listed = ", ".join(f"{household_id} ({live} live)" for household_id, live in offenders)
    raise RuntimeError(
        f"revision 0025 makes one live {_TABLE} per household the rule, and "
        f"{len(offenders)} household(s) hold more than one: {listed}. "
        "This revision will not choose which one to keep -- archiving a configuration "
        "retires a third party's API key. Archive the extras first "
        f"(UPDATE {_TABLE} SET archived_at = now(), status = 'disabled' WHERE id = ...), "
        "then run the upgrade again."
    )


def upgrade() -> None:
    _refuse_existing_duplicates()

    op.drop_index(_OLD_INDEX, table_name=_TABLE, postgresql_where=sa.text(_LIVE_ROW))
    op.create_index(
        _NEW_INDEX,
        _TABLE,
        ["household_id"],
        unique=True,
        postgresql_where=sa.text(_LIVE_ROW),
    )

    # The policy revision `0004` created on this table goes with the table; the enum
    # goes with its last column.
    op.drop_table(_BINDING_TABLE)
    op.execute(f"DROP TYPE {_BINDING_ENUM}")


def downgrade() -> None:
    op.drop_index(_NEW_INDEX, table_name=_TABLE, postgresql_where=sa.text(_LIVE_ROW))
    op.create_index(
        _OLD_INDEX,
        _TABLE,
        ["household_id"],
        unique=False,
        postgresql_where=sa.text(_LIVE_ROW),
    )

    # Copied from revision `0001` rather than rewritten, down to the `op.f()` calls
    # that render the constraint names: `tests/test_schema_naming_guard.py` compares
    # the catalogue to the model by name, and an approximation here would restore a
    # table that is only nearly the one that was dropped. The `sa.Enum` recreates the
    # `llm_purpose` type, which `upgrade` dropped.
    op.create_table(
        _BINDING_TABLE,
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum("recipe_generation", "receipt_parsing", name=_BINDING_ENUM),
            nullable=False,
        ),
        sa.Column("llm_provider_config_id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Not hygiene, a security control: without it a guessed identifier would be
        # enough to spend another household's API credit.
        sa.ForeignKeyConstraint(
            ["household_id", "llm_provider_config_id"],
            [f"{_TABLE}.household_id", f"{_TABLE}.id"],
            name=op.f("fk_llm_purpose_binding_household_id_llm_provider_config_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_llm_purpose_binding_household_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("household_id", "purpose", name=op.f("pk_llm_purpose_binding")),
    )

    # Revision `0004` protects this table, and `DROP TABLE` took the policy with it.
    # Re-enabling row-level security without recreating the policy would give the
    # opposite of the intended result -- a table with RLS on and no policy returns no
    # rows to anyone but its owner -- so both statements belong together.
    op.execute(f"ALTER TABLE {_BINDING_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_BINDING_POLICY} ON {_BINDING_TABLE}
        FOR ALL
        USING ({_OWN_ROW})
        WITH CHECK ({_OWN_ROW})
        """
    )
