"""password reset tokens: the one unauthenticated way back into an account

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-08 00:00:00.000000+00:00

Until this revision the application had no outbound mail, and
``api/routers/auth.py`` said so in as many words: *there is no password reset. A
forgotten password is a forgotten account, and the honest answer is to say so
rather than to build an unauthenticated recovery path.* That was correct while
there was no channel only the owner of an address could read. It stops being
correct the moment somebody installs this for a family member -- the documented
recovery path, "an owner re-invites the person", is not a path when the owner
*is* the person.

``docs/security-model.md`` section 9 listed the preconditions. This table is one
of them: *single-use short-lived tokens stored hashed exactly like sessions*.

One table
---------

``password_reset_token``. One row per outstanding link, and the shape is copied
from :class:`chaudron.domain.models.UserSession` and ``machine_token`` rather than
invented:

* ``token_hash`` is a SHA-256 of the value that went into the message, lowercase
  hex, unique. The plaintext exists in one place -- the body of one email -- and
  is never written here. A dump of this table opens no account. SHA-256 rather
  than Argon2, for the reason revision ``0011`` gives: the value is 256 bits from
  ``secrets``, there is no dictionary to slow down, and a memory-hard function on
  a lookup would be a denial of service we perform on ourselves.
* ``expires_at`` is absolute and never moves. There is no idle deadline, because
  a reset link is used once or not at all; sliding one forward would only extend
  the life of a key sitting in an inbox.
* ``consumed_at`` is the single-use column, and it is set by **four** events, not
  one: following the link, asking for another, changing the password voluntarily,
  and completing a reset. All four mean the same thing -- whatever links are in
  flight are no longer the current way in -- and folding them into one column is
  what stops "the password changed" and "the outstanding links died" from being
  two facts that can disagree.

Unknown, expired, consumed and superseded are therefore **one ``WHERE`` clause**
in ``services/auth.py``, one exception, and one body at the API. Telling a caller
that their link had existed and has expired confirms that a reset was requested
for the address they hold, which is the enumeration answer this whole feature is
arranged to withhold.

No tenant column, and it must not have one
------------------------------------------

The same exemption ``user_session`` carries (revision ``0009``) and for the same
two reasons. A reset belongs to an **account**, and an account may open a family
home and a flatshare; a tenant here would mean one recovery per household, which
is the modelling mistake ``user_account`` exists to avoid. And it could not work
even if it were wanted: the row is written for a stranger who has posted no
tenant and, at that moment, has proved nothing at all -- a policy keyed on the
tenant would have to be satisfied before the tenant is known.

It is declared in the tenancy guard's ``GLOBAL_TABLES`` with that reason, which
is how this schema records such an exemption rather than leaving it to be
rediscovered.

Nothing here records **who asked**
----------------------------------

No source address, no user agent, no ``requested_by``. Two arguments, and the
second is the one that settles it.

The forensic value is small: the row lives an hour, and the address it would hold
is the proxy's on most deployments (``api/routers/auth.py`` documents that
limitation for the rate limiters). The privacy cost is not: it would be personal
data about an *unauthenticated* person, retained past the request that produced
it, on a table an article 17 erasure reaches only through the account -- and the
requester need not have one. So the column is absent rather than nullable.

``ON DELETE CASCADE``
---------------------

An erased account takes its outstanding links with it. The alternative --
``SET NULL`` or ``RESTRICT`` -- would leave a row that authorises a password
change on an account that no longer exists, which is either an orphan or a bug
waiting for one.

Reversible
----------

``downgrade`` drops the table, and with it every outstanding link. That is the
correct behaviour for removing the reset store: an instance rolled back to ``0022``
has no reset endpoint either, so a surviving row would be a credential the schema
can no longer validate and nobody can revoke. Nothing else in the schema
references it, so the drop is unconditional and complete.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "password_reset_token",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            comment=(
                "SHA-256 of the value that went out in the message, lowercase hex. The "
                "plaintext is never stored: a dump of this table opens no account."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when the link is followed, and also when it is superseded by a "
                "later request, a password change or a completed reset. A used token "
                "and an expired one are one answer at the API."
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            # Rendered by ``NAMING_CONVENTION``'s ``fk_%(table_name)s_%(column_0_N_name)s``
            # template. Spelled out here because the migration has no metadata to
            # render it from, and ``tests/test_schema_naming_guard.py`` diffs the two.
            name="fk_password_reset_token_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_password_reset_token"),
    )
    # The lookup key of a bearer credential: global by necessity, because the
    # digest is what *decides* the account (same argument as
    # ``uq_machine_token_token_hash``, revision ``0011``).
    op.create_index(
        "uq_password_reset_token_hash", "password_reset_token", ["token_hash"], unique=True
    )
    # "Kill every outstanding link for this account", which runs on every password
    # change and every completed reset.
    op.create_index("ix_password_reset_token_user_id", "password_reset_token", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_password_reset_token_user_id", table_name="password_reset_token")
    op.drop_index("uq_password_reset_token_hash", table_name="password_reset_token")
    op.drop_table("password_reset_token")
