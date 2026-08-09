"""let an account see, past row-level security, what its own erasure would orphan

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-09 00:00:00.000000+00:00

``DELETE /v1/account`` has to answer one question before it deletes anything:
**would removing this account's memberships leave any household with no owner?**
That is the question ``services/memberships.py`` already refuses to create the
answer to -- ``LastOwnerError``, "what becomes of a household whose last member
leaves is a product question, and answering it in a ``DELETE`` handler would be
inventing it in the worst possible place". This revision does not answer it
either. It supplies the only thing the refusal needs: the fact.

**Why a function rather than a query.** Revision ``0004`` put row-level security
on ``household_member`` with the predicate
``household_id = chaudron_current_household()``, and revision ``0009`` explains
why authentication cannot satisfy it. Account erasure cannot either, and for a
second reason on top of the first: it spans *every* household the account
belongs to, while ``infra/db.py``'s rule is that **one transaction serves one
household**. There is no tenant to post that would make the read legal, because
there is no single tenant. With none posted the policy shows zero rows, and a
direct query would report that the account belongs to nothing and is therefore
safe to erase -- the most dangerous wrong answer available.

``SECURITY DEFINER``, then, exactly as ``chaudron_user_memberships`` (``0009``),
``chaudron_resolve_machine_token`` (``0011``) and
``chaudron_resolve_household_invitation`` (``0022``). The same three narrowings
apply, and the second one is doing more work here than in any of the three:

* it takes one argument and returns rows for **that account's** memberships only,
  so it cannot be used to enumerate somebody else's households;
* it returns **counts, never identities**. The caller learns that a household has
  two other owners; it does not learn who they are, what their addresses are, or
  that they exist at all beyond a number. Handing back the other members' rows
  would have been the obvious shape and would have turned an erasure
  pre-condition into a cross-household directory;
* ``search_path`` is pinned to ``pg_catalog, public``, without which a caller able
  to create a schema substitutes their own ``household_member``.

**``household.name`` is returned, and that is a deliberate disclosure.** A refusal
that says "one of your households would be left without an owner" and does not say
*which* is a refusal nobody can act on. The name belongs to a household the
account is a member of, and every member can already read it.

Reversible: ``downgrade`` drops the function. Nothing depends on it in the
database -- no policy calls it, no view selects from it -- so dropping it removes
the account-erasure route's ability to run and touches nothing else. The matching
``GRANT`` lives in ``scripts/provision_app_role.py``; without it the API refuses
every account erasure, which is the correct direction to fail.
"""

from __future__ import annotations

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None

#: Named here and imported by ``services/privacy.py`` rather than spelled twice.
SURVEY_FUNCTION: str = "chaudron_account_erasure_survey"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SURVEY_FUNCTION}(p_user_id uuid)
        RETURNS TABLE (
            household_id uuid,
            household_name varchar(120),
            role membership_role,
            other_owner_count bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT
                h.id,
                h.name,
                mine.role,
                (
                    SELECT count(*)
                    FROM household_member AS others
                    WHERE others.household_id = h.id
                      AND others.user_id <> p_user_id
                      AND others.role = 'owner'
                )
            FROM household_member AS mine
            JOIN household AS h ON h.id = mine.household_id
            WHERE mine.user_id = p_user_id
            ORDER BY h.name, h.id
        $$
        """
    )
    op.execute(
        f"COMMENT ON FUNCTION {SURVEY_FUNCTION}(uuid) IS "
        "'One row per household this account belongs to, with its own role and a "
        "count -- never a list -- of the other owners. Answers the single question "
        "DELETE /v1/account must settle before it deletes anything: would this "
        "account''s departure leave a household with nobody who can administer it? "
        "SECURITY DEFINER because the read crosses every household the account "
        "belongs to, and infra/db.py''s rule is that one transaction serves one "
        "household -- so there is no tenant to post that would make it legal, and an "
        "unscoped query would report that the account belongs to nothing. Returns no "
        "other member''s identity. search_path is pinned; see revision 0026.'"
    )
    # Revision `0014`'s rule: PostgreSQL defaults a new function to EXECUTE by
    # PUBLIC, and a SECURITY DEFINER exempt from every policy must not be reachable
    # by the next role somebody adds. The matching GRANT is in
    # `scripts/provision_app_role.py`, and the two are one change.
    op.execute(f"REVOKE EXECUTE ON FUNCTION {SURVEY_FUNCTION}(uuid) FROM PUBLIC")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SURVEY_FUNCTION}(uuid)")
