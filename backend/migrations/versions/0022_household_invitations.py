"""household invitations, and a way to resolve one before the tenant is known

Revision ID: 0022
Revises: 0020
Create Date: 2026-08-08 00:00:00.000000+00:00

This revision follows ``0020``: there is no ``0021``. Several revisions were
written in parallel and the numbers were handed out before the order settled, so
the sequence has a gap where one of them was renumbered on its way in. Alembic
reads ``down_revision`` and nothing else -- the identifiers are labels, never an
ordering -- and the chain from here is ``0020 -> 0022 -> 0023 -> 0024``.

Until this revision a household was permanently one person. ``household_member``
has carried three roles since revision ``0001``, and the application wrote that
table in exactly one place -- registration, as ``owner``. There was no invite
path, no join path and no removal path, so ``member`` and ``viewer`` were values
the schema could hold and nothing could produce, and every role guard in
``api/deps.py`` was defending a state that could not be reached. The application
is named for households and could not have two people in one.

Two database objects, and the second is the awkward part -- for the third time in
this schema's history, and for the same reason.

*``household_invitation``.* One row per issued invitation, scoped to **one
household** and carrying the role the redeemer will receive. ``token_hash`` is a
SHA-256 of a 256-bit value handed out once at creation, for the reason argued on
:class:`chaudron.domain.models.UserSession` and repeated by revision ``0011``:
there is no dictionary to slow down, and an Argon2 verification on a redemption
would buy nothing. A dump of this table yields nothing redeemable.

The table carries ``household_id``, so it is protected exactly like the tables of
revision ``0004``: ``ENABLE ROW LEVEL SECURITY``, the same
``household_id = chaudron_current_household()`` predicate, and deliberately **no**
``FORCE`` -- revision ``0004`` argues that at length and the argument has not
changed.

Three column-level decisions are worth reading as decisions rather than as
defaults:

* ``expires_at`` is ``NOT NULL``, unlike ``machine_token.expires_at``. A token is
  an integration that must keep running; an invitation is a doorbell. One that
  never expires is a permanent way into a household, sitting in a chat log.
* ``ck_household_invitation_role_not_owner`` refuses ``owner`` in the database.
  Handing somebody ownership cannot be undone by the person who did it -- two
  owners may each remove the other -- so it must not be reachable by pasting a
  code into a form. Promotion to owner is a separate gesture, and this revision
  deliberately does not invent it.
* there is no recipient address. Chaudron has no outbound email, and a column
  holding an address nobody verifies would be a control in appearance only. The
  value is handed over out of band; adding SMTP later changes the delivery and
  not this table.

*``chaudron_resolve_household_invitation(text)``.* The same inversion revisions
``0009`` and ``0011`` had to solve, arriving from a third side. Somebody
redeeming an invitation is **by definition not yet a member**, so no tenant can
have been posted -- the invitation is what decides it -- and a direct read of
``household_invitation`` would be filtered by the very policy the lookup exists to
arm.

``SECURITY DEFINER`` again, and narrowed the same three ways:

* it is keyed on the **digest of a 256-bit secret**, so it enumerates nothing: a
  caller who can supply the argument already holds the invitation;
* it returns four columns, not the table -- ``prefix``, ``last4`` and the
  timestamps stay behind the policy, where the household's own session reads them;
* ``search_path`` is pinned to ``pg_catalog, public``, without which a caller able
  to create a schema substitutes their own ``household_invitation``.

And, following revision ``0014``, ``EXECUTE`` is revoked from ``PUBLIC`` in the
same breath as the function is created rather than left at PostgreSQL's default.
The matching ``GRANT`` is in ``scripts/provision_app_role.py``; the two are one
change, and a database migrated without re-running that script answers every
redemption with a refusal.

**Every refusal is one branch.** Unknown, expired, revoked, already redeemed,
issued by an account since disabled, issued by somebody who is no longer an owner
of the household, and belonging to an archived household all produce zero rows
from the same ``WHERE``. The API therefore answers them identically *and takes
the same time to do it*.

That last clause -- the join back to ``household_member`` with ``role = 'owner'``
-- is what keeps an invitation from outliving the authority that issued it. An
owner who is demoted or removed cannot leave a working way in behind them, and
nobody has to remember to go and revoke it.

Reversible. ``downgrade`` drops the function, the policy, the indexes and the
table, and destroys every pending invitation -- which is the correct behaviour for
removing the invitation store: nobody is left holding a code the schema can no
longer validate. Memberships already created by a redemption are ordinary
``household_member`` rows and are **not** touched: undoing the mechanism must not
evict the people who came in through it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE: str = "household_invitation"
RESOLVE_FUNCTION: str = "chaudron_resolve_household_invitation"

#: Same helper and same predicate as revision ``0004``. Repeated rather than
#: imported so this file reads on its own, which is how a migration is read.
_TENANT_FUNCTION = "chaudron_current_household"
_OWN_ROW = f"household_id = {_TENANT_FUNCTION}()"
_POLICY = f"{TABLE}_household_isolation"

#: Declared by revision ``0001`` and reused, never re-created: an invitation
#: grants a ``household_member.role`` and must be spelled in the same vocabulary.
_ROLE_ENUM = "membership_role"


def _role_type() -> postgresql.ENUM:
    return postgresql.ENUM(name=_ROLE_ENUM, create_type=False)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("household_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            _role_type(),
            nullable=False,
            comment=(
                "The role the redeemer receives. Never 'owner': see the check "
                "constraint, and revision 0022."
            ),
        ),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of the presented value, lowercase hex. The plaintext is never "
                "stored: a dump of this table yields no redeemable invitation."
            ),
        ),
        sa.Column(
            "prefix",
            sa.String(length=16),
            nullable=False,
            comment=(
                "The fixed scheme marker the value carries, stored per row so a list "
                "still renders correctly if the format ever changes."
            ),
        ),
        sa.Column(
            "last4",
            sa.String(length=4),
            nullable=False,
            comment="The last four characters, to tell two pending invitations apart.",
        ),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            comment=(
                "The owner who issued it. CASCADE rather than SET NULL: an invitation "
                "outliving the account that vouched for it is a credential nobody owns."
            ),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "redeemed_by_user_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment=(
                "Who used it. SET NULL here, unlike the issuer: the membership this "
                "produced is the record that matters, and it is deleted by its own "
                "cascade when the account goes."
            ),
        ),
        # Both `name=` values below are *components*: `op.create_table` renders
        # them through the metadata's `ck_%(table_name)s_%(constraint_name)s`
        # template, so writing the prefix here would deploy
        # `ck_household_invitation_ck_household_invitation_...`. That is the
        # mistake revision `0010` had to rename eighteen constraints to undo, and
        # `tests/test_schema_naming_guard.py` fails on it.
        sa.CheckConstraint("role <> 'owner'", name="role_not_owner"),
        sa.CheckConstraint(
            "(redeemed_at IS NULL) = (redeemed_by_user_id IS NULL)",
            name="redemption_recorded_whole",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name="fk_household_invitation_household_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user_account.id"],
            name="fk_household_invitation_created_by_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by_user_id"],
            ["user_account.id"],
            name="fk_household_invitation_redeemed_by_user_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_household_invitation"),
    )
    op.create_index("uq_household_invitation_token_hash", TABLE, ["token_hash"], unique=True)
    op.create_index("ix_household_invitation_household_id", TABLE, ["household_id"], unique=False)

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON {TABLE}
        FOR ALL
        USING ({_OWN_ROW})
        WITH CHECK ({_OWN_ROW})
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {RESOLVE_FUNCTION}(p_token_hash text)
        RETURNS TABLE (
            invitation_id uuid,
            household_id uuid,
            role {_ROLE_ENUM},
            created_by_user_id uuid
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT i.id, i.household_id, i.role, i.created_by_user_id
            FROM {TABLE} AS i
            JOIN household_member AS m
              ON m.household_id = i.household_id
             AND m.user_id = i.created_by_user_id
             AND m.role = 'owner'
            JOIN user_account AS u ON u.id = i.created_by_user_id
            JOIN household AS h ON h.id = i.household_id
            WHERE i.token_hash = p_token_hash
              AND i.revoked_at IS NULL
              AND i.redeemed_at IS NULL
              AND i.expires_at > now()
              AND u.disabled_at IS NULL
              AND h.archived_at IS NULL
        $$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {RESOLVE_FUNCTION}(text) IS "
        "'Resolve a household invitation from the SHA-256 of its value, past the "
        "row-level security on household_invitation. SECURITY DEFINER because the "
        "redeemer is by definition not yet a member, so no tenant can have been "
        "posted -- the invitation is what decides it; see revision 0022. Keyed on "
        "the digest of a 256-bit secret, so it enumerates nothing. Unknown, expired, "
        "revoked, spent, disabled-issuer, demoted-issuer and archived-household are "
        "one branch, so the API can answer them identically and in the same time. "
        "search_path is pinned.'"
    )
    # Revision `0014`'s rule, applied at birth rather than retrofitted: PostgreSQL
    # defaults a new function to EXECUTE by PUBLIC, and a SECURITY DEFINER exempt
    # from every policy must not be reachable by the next role somebody adds. The
    # matching GRANT is in scripts/provision_app_role.py and the two are one change.
    op.execute(f"REVOKE EXECUTE ON FUNCTION {RESOLVE_FUNCTION}(text) FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {RESOLVE_FUNCTION}(text)")
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {TABLE}")
    op.drop_index("ix_household_invitation_household_id", table_name=TABLE)
    op.drop_index("uq_household_invitation_token_hash", table_name=TABLE)
    op.drop_table(TABLE)
