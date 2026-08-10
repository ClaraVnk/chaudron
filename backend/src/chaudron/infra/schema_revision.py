"""Which schema revision this build needs, and how to judge the one it finds.

``/readyz`` is polled, and until now it answered "ready" against any schema at
all -- including one several migrations behind the code asking the question.
``ops/README.md`` §1.6 has always claimed the probe covered "migrations
applied"; this module is what makes that sentence true.

**Why the revision is written down here rather than read from Alembic.**
:class:`alembic.script.ScriptDirectory` would give the same answer from the
migration files, and that is what ``tests/infra/test_schema_revision.py`` uses to
prove this constant is not stale. It is the wrong thing to do *at runtime*, twice
over:

* ``alembic.ini`` sets ``script_location = migrations``, a **relative** path,
  which Alembic resolves against the current working directory -- not against the
  config file. ``tests/conftest.py`` already has to override it with an absolute
  path for exactly that reason. The runtime image happens to work
  (``WORKDIR=/app``, and ``/app/migrations`` is copied in), and a developer
  running from ``backend/`` happens to work, but both are accidents of cwd. A
  readiness probe whose answer depends on where uvicorn was launched from is one
  that fails closed for the wrong reason -- and failing closed here means an
  instance that never enters service.
* Walking a revision graph off the filesystem on every poll is real I/O on a hot
  path. This is two string comparisons.

The cost of writing it down is that it can drift, and drift is what a test is
for. A migration added without bumping :data:`REQUIRED_SCHEMA_REVISION` fails in
CI, loudly, which is the only place that mistake is cheap. **One line per
migration** is the whole of the tax, deliberately: an earlier draft of this
module listed the entire chain, which put every migration author in conflict with
this file for no additional certainty.

**How two revisions are ordered without the graph.** ``alembic.ini`` states the
project's own naming rule: *"Revision files are named ``NNNN_slug.py``: sorted
lexically, they are in dependency order, which a bare hash never is."* So a plain
string comparison answers "older or newer", and the test asserts that invariant
holds across the real chain rather than trusting it -- if anybody ever lands a
hash-style revision id, that test fails instead of this comparison silently
misjudging a deployment.

**Why the answer has four values and not two.** The deployment in
``ops/README.md`` §5.4 runs migrations *outside* the automatic update loop and
requires every migration to be backward compatible with the code currently
running -- expand, migrate, contract. So a database *ahead* of the code is a
supported, documented state that lasts for the few minutes of a rolling update,
and refusing readiness there would take a healthy instance out of service in the
middle of the procedure the runbook prescribes. A database *behind* the code is
the opposite: new code against a schema missing the columns it was written for,
which is the failure this module exists to catch. The two are not the same fact
and must not collapse into one status code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

#: The Alembic revision this build was written against -- the head of
#: ``backend/migrations/versions/`` at the time it was compiled. Anything older
#: is a schema missing something the code assumes.
#:
#: **Bump this in the same commit as a new migration.**
#: ``tests/infra/test_schema_revision.py`` fails otherwise, which is the point.
REQUIRED_SCHEMA_REVISION: Final = "0028"


class SchemaRevisionState(StrEnum):
    """What the schema stamped in ``alembic_version`` means for this build.

    The values are the tokens ``/readyz`` publishes, so they are deliberately
    plain: the body is read by a reverse proxy and by an operator with ``curl``,
    and neither wants a sentence.
    """

    #: The database is stamped at exactly the revision this build needs.
    CURRENT = "ok"

    #: The database carries a *later* revision than this build knows about: a
    #: rolling update where the migration has landed and this process has not
    #: been replaced yet. Supported by ``ops/README.md`` §5.4, which requires
    #: migrations to be backward compatible for exactly this window.
    AHEAD = "ahead"

    #: The database is stamped at an *earlier* revision than the one this build
    #: needs. New code, old schema -- the state where a request fails on a column
    #: that does not exist.
    OUTDATED = "outdated"

    #: There is no usable answer: no ``alembic_version`` table, no row in it, or
    #: more than one head stamped. Never treated as good news.
    UNKNOWN = "unknown"

    @property
    def is_serviceable(self) -> bool:
        """Whether an instance on this schema may be given traffic.

        ``UNKNOWN`` is on the refusing side, and that is the fail-closed
        direction: a database that has never been migrated has no
        ``alembic_version`` table at all, so "cannot tell" and "brand new empty
        database" are the same observation. Reporting it as its own token rather
        than folding it into :attr:`OUTDATED` is what lets an operator tell "you
        have not migrated this database" from "you deployed ahead of your
        migration" -- different faults, different steps of the runbook.
        """
        return self in (SchemaRevisionState.CURRENT, SchemaRevisionState.AHEAD)


def classify_schema_revision(applied: str | None) -> SchemaRevisionState:
    """Judge the revision stamped in the database against the one this build needs.

    ``applied`` is what ``alembic_version.version_num`` holds, or ``None`` when
    there is nothing to read -- no table, no row, or several rows, all of which
    are :attr:`SchemaRevisionState.UNKNOWN` rather than an exception, because the
    caller is a probe and a probe reports rather than raises.

    Ordering is the lexical comparison the project's file-naming rule licenses;
    the module docstring explains why that is sound and what keeps it so.
    """
    if applied is None:
        return SchemaRevisionState.UNKNOWN
    if applied == REQUIRED_SCHEMA_REVISION:
        return SchemaRevisionState.CURRENT
    return (
        SchemaRevisionState.OUTDATED
        if applied < REQUIRED_SCHEMA_REVISION
        else SchemaRevisionState.AHEAD
    )
