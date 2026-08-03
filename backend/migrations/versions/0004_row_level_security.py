"""enable row-level security on every tenant-scoped table

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-04 00:00:00.000000+00:00

Closes the engine half of SEC-001 / AUD-002. Until this revision the isolation
between households rested entirely on the application remembering to write
``WHERE household_id = :current`` -- a property that degrades silently, because
the one query that forgets it fails no test and raises no error. It simply
returns another family's pantry.

Three things happen here, and the order matters.

*One function, thirteen policies.* ``chaudron_current_household()`` reads the
transaction-local GUC ``chaudron.household_id`` posted by
``chaudron.infra.db.set_transaction_household``. It is ``STABLE`` so the planner
inlines it and evaluates it once per query, which keeps the policy predicate
index-friendly: the rewritten query is ``household_id = <constant>``, not a
function call per row.

*``NULLIF`` is not decoration.* PostgreSQL resets a ``SET LOCAL`` at ``COMMIT``
by restoring the *session* value of the parameter, which for a custom GUC that
was never set at session level is the empty string -- **not** NULL. Verified on
PostgreSQL 16 with a pooled connection: the second transaction reads ``''``.
Without the ``NULLIF``, every query on a recycled connection would die on
``invalid input syntax for type uuid: ""`` instead of returning nothing.

*No tenant means no rows, everywhere, including the public catalogue.* The
predicate is written so that an unscoped transaction sees zero rows on all
thirteen tables rather than "zero private rows plus the whole shared
catalogue". A uniform rule is one an operator can verify in a single query and
a test can assert table by table; an exception on ``product`` would be the one
line nobody re-reads.

**``FORCE ROW LEVEL SECURITY`` is deliberately not enabled.** The table owner is
the migration and maintenance identity -- Alembic, ``scripts/seed.py``, a DBA
holding a psql prompt -- and forcing policies on it would mean every one of them
has to post a tenant before it can insert a row. The security property comes
from the *application* connecting as a role that owns nothing:
``scripts/provision_app_role.py`` creates it, checks it is neither the owner nor
``BYPASSRLS``, and ``ops/README.md`` section 3.6 documents the switch. An
instance that keeps pointing the application at the owner DSN gets no protection
from this revision and has to be told so, which is what the check in that script
and ``Database.check_row_level_security`` exist for.

Roles are cluster-wide objects, so nothing here creates or grants to one: a
migration that needed ``CREATEROLE`` would fail on every managed PostgreSQL that
does not hand it out. The provisioning script sets ``ALTER DEFAULT PRIVILEGES``
so tables added by later revisions are granted automatically.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The transaction-local parameter carrying the current household. Kept in sync
#: with ``chaudron.infra.db.TENANT_SETTING`` -- a test asserts the two agree.
TENANT_SETTING: str = "chaudron.household_id"

#: The SQL helper every policy calls.
TENANT_FUNCTION: str = "chaudron_current_household"

#: Tables whose ``household_id`` is ``NOT NULL``: one household, one owner, no
#: shared rows. Ordered as ``Base.metadata.sorted_tables`` returns them so a diff
#: against the model is readable.
UNIFORM_TENANT_TABLES: tuple[str, ...] = (
    "household_member",
    "inventory_lot",
    "llm_provider_config",
    "llm_purpose_binding",
    "receipt",
    "receipt_line",
    "recipe_suggestion",
    "recipe_suggestion_ingredient",
    "shopping_list",
    "shopping_list_item",
    "stock_movement",
    "storage_location",
)

#: ``product`` is the exception: ``household_id IS NULL`` is the mutualised
#: public catalogue (ADR-0008), non-NULL a household-private entry.
PRODUCT_TABLE: str = "product"

#: Every table this revision protects.
PROTECTED_TABLES: tuple[str, ...] = (*UNIFORM_TENANT_TABLES, PRODUCT_TABLE)

#: Read as: "a tenant is posted, and the row belongs to it".
_OWN_ROW = f"household_id = {TENANT_FUNCTION}()"

#: Read as: "a tenant is posted, and the row is either the household's own or a
#: public catalogue entry". The left conjunct is what makes an unscoped
#: transaction see nothing at all rather than the whole catalogue.
_OWN_OR_PUBLIC_ROW = f"{TENANT_FUNCTION}() IS NOT NULL AND (household_id IS NULL OR {_OWN_ROW})"


def _policy_name(table: str) -> str:
    return f"{table}_household_isolation"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {TENANT_FUNCTION}() RETURNS uuid
        LANGUAGE sql
        STABLE
        PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid
        $$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {TENANT_FUNCTION}() IS "
        "'Current household for row-level security, posted per transaction by "
        "chaudron.infra.db.set_transaction_household. NULL when unscoped, which "
        "every policy reads as: no rows.'"
    )

    for table in UNIFORM_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {_policy_name(table)} ON {table}
            FOR ALL
            USING ({_OWN_ROW})
            WITH CHECK ({_OWN_ROW})
            """
        )

    # `product` needs four policies rather than one, because reading and writing
    # do not have the same rule. The public catalogue is readable and writable by
    # any scoped household -- it is a shared cache of Open Food Facts answers, and
    # scoping it per household would multiply the outbound calls by the number of
    # tenants for byte-identical content (ADR-0008, repositories/products.py).
    # Deleting from it is not part of that bargain: `upsert_public` archives, it
    # never removes, so a DELETE reaching a public row is a bug and the policy
    # says so.
    op.execute(f"ALTER TABLE {PRODUCT_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_policy_name(PRODUCT_TABLE)}_select ON {PRODUCT_TABLE}
        FOR SELECT USING ({_OWN_OR_PUBLIC_ROW})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {_policy_name(PRODUCT_TABLE)}_insert ON {PRODUCT_TABLE}
        FOR INSERT WITH CHECK ({_OWN_OR_PUBLIC_ROW})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {_policy_name(PRODUCT_TABLE)}_update ON {PRODUCT_TABLE}
        FOR UPDATE USING ({_OWN_OR_PUBLIC_ROW}) WITH CHECK ({_OWN_OR_PUBLIC_ROW})
        """
    )
    op.execute(
        f"""
        CREATE POLICY {_policy_name(PRODUCT_TABLE)}_delete ON {PRODUCT_TABLE}
        FOR DELETE USING ({_OWN_ROW})
        """
    )


def downgrade() -> None:
    for table in UNIFORM_TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_policy_name(table)} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for action in ("select", "insert", "update", "delete"):
        op.execute(
            f"DROP POLICY IF EXISTS {_policy_name(PRODUCT_TABLE)}_{action} ON {PRODUCT_TABLE}"
        )
    op.execute(f"ALTER TABLE {PRODUCT_TABLE} DISABLE ROW LEVEL SECURITY")

    # After the policies: a function still referenced by one cannot be dropped.
    op.execute(f"DROP FUNCTION IF EXISTS {TENANT_FUNCTION}()")
