"""make an unconsented third-party provider unrepresentable, not merely unusable

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-08 00:00:00.000000+00:00

The half of revision ``0016`` that was deferred, and the reason it was deferred no
longer holds. That revision said so in as many words:

    The mode-tied CHECK -- "``byok`` and ``instance_owner`` rows must carry a
    consent", the constraint that would make the wrong state unrepresentable rather
    than merely unusable -- is deliberately **not** added here. Per the
    expand/migrate/contract rule, it belongs with the registration route that can
    guarantee the invariant on insert, and that route does not exist yet.

That route exists now (``api/routers/providers.py``, ``POST /v1/providers``), so the
invariant has a place where it can be established rather than merely hoped for, and
the penetration test of 2026-08-04 records the same conclusion under finding O-02.

What this adds
--------------

``ck_llm_provider_config_consent_required_by_mode``::

    mode = 'ollama' OR consented_at IS NOT NULL

Read as: **a row that transmits to a third party carries an agreement, or it does
not exist.** ``byok`` sends under the household's own key and ``instance_owner``
under the operator's, which changes who pays and not who receives -- and either way
the prompt carries a child's age band, a member's free-text health note, or an
entire receipt photograph (art. 9(2)(a), and ``docs/security-model.md`` §8.3).

The gate in ``services/providers.py`` already refuses such a row at every read. This
is the difference between a rule the application applies and a rule the database
holds: a future code path that forgets the gate, a repair script, a hand-written
``INSERT`` by an operator switching the feature on -- all of them now fail loudly
instead of producing a configuration that quietly transmits.

Why ``ollama`` is still spelled by mode here, when the gate no longer does
--------------------------------------------------------------------------

The application's consent exemption was moved off the enum in this same change: it
now keys on whether ``base_url`` resolves to a loopback or otherwise non-routable
address, because ``mode = 'ollama'`` names a wire protocol and not a location, and
an operator who allowlists a *hosted* Ollama produced a row that transmitted to a
third party with the gate switched off (pentest, design note under O-02).

This constraint cannot make that distinction and must not pretend to. Classifying an
endpoint means parsing a URL and, for the documented co-located topology
``http://ollama:11434``, resolving a name -- neither of which belongs in a CHECK,
and the second of which is not immutable, so PostgreSQL would refuse it outright.
So the database holds the half it can hold *totally* -- the two modes that transmit
unconditionally -- and the application holds the half that depends on where the
endpoint is. Splitting it that way means neither layer states more than it enforces,
which is the failure this schema has met before.

``NOT VALID``, and why that is the whole point rather than a compromise
-----------------------------------------------------------------------

Revision ``0016`` refused to backfill ``consented_at`` on rows that predate it,
because a fabricated date "would not merely mislabel a record, it would manufacture
the legal basis for a transfer that nobody agreed to". That reasoning is untouched
here, and it has a consequence: a deployment may hold exactly the rows this
constraint forbids.

A validating ``ADD CONSTRAINT`` would abort the migration on those deployments, and
the only ways to make it succeed would be to invent the consent (refused above) or
to delete the household's configuration during an upgrade (worse). ``NOT VALID`` is
neither: PostgreSQL enforces the constraint on **every INSERT and every UPDATE** from
this revision onward, and simply does not re-examine rows already present. New
configurations are therefore correct by construction, which is the guarantee O-02
asked for, while the legacy rows keep failing closed at the gate -- with the reason
and the remedy on the banner the household already sees -- until somebody re-grants
through the route this revision was waiting for, or archives the configuration.

Validating it later is a one-line follow-up (``ALTER TABLE ... VALIDATE
CONSTRAINT``) that takes only a ``SHARE UPDATE EXCLUSIVE`` lock and can be run when
an operator has confirmed there is nothing left to trip over. It is deliberately not
run here, where nobody could confirm that.

Why the revision number is out of order
----------------------------------------

``0021`` was left free for this work while ``0022`` and ``0023`` landed alongside it,
and ``0022`` already names ``0020`` as its parent. Chaining this revision to ``0020``
as well would give Alembic two heads and break every ``upgrade head``; re-pointing
``0022`` would mean editing another change's migration. So the identifier keeps the
number that was reserved and the *graph* stays linear, which is the only one of the
two that anything executes. Alembic orders by the graph and never by the name.
"""

from __future__ import annotations

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None

#: Spelled in full rather than as the ``constraint_name`` component, because this
#: is raw DDL: ``op.create_check_constraint`` would prepend the metadata template's
#: ``ck_llm_provider_config_`` prefix, and ``op.execute`` does not. The rendered form
#: has to match what ``Base.metadata`` produces for the twin declaration on
#: :class:`chaudron.domain.models.LlmProviderConfig`, or
#: ``tests/test_schema_naming_guard.py`` fails -- which is exactly what that file
#: exists to catch (revision ``0010`` was eighteen constraints named wrongly).
_CONSTRAINT = "ck_llm_provider_config_consent_required_by_mode"

#: ``consent_revoked_at`` is deliberately absent. A withdrawal is a *state* of a row
#: that exists and is refused at the gate, not a row that should stop existing --
#: forbidding it would delete the record art. 7(3) requires be kept, and would make
#: withdrawal impossible to perform at all.
_PREDICATE = "mode = 'ollama' OR consented_at IS NOT NULL"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE llm_provider_config"
        f" ADD CONSTRAINT {_CONSTRAINT} CHECK ({_PREDICATE}) NOT VALID"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE llm_provider_config DROP CONSTRAINT {_CONSTRAINT}")
