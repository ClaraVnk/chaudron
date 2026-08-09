"""The revision ``/readyz`` judges against, kept honest.

``chaudron.infra.schema_revision`` writes the required revision down rather than
reading it from Alembic at runtime, for the reasons its module docstring gives --
``script_location`` is resolved against the current working directory, and a
readiness probe must not depend on where uvicorn was launched from.

The cost of writing it down is drift. This file is what makes drift impossible to
ship: it reads the real Alembic graph and compares. A migration added without
bumping the constant fails here, in CI, which is the only place that mistake is
cheap.

It also asserts the invariant the comparison *rests* on -- that revision ids sort
lexically into dependency order, which is the rule ``alembic.ini`` states in its
own comments. That one is the more important of the two: a stale constant makes
the probe refuse a good deployment, which is loud, whereas a chain that no longer
sorts would make it accept a bad one, which is silent.

No database is involved -- this reads the migration *files*, so it runs in the
unit suite rather than behind a container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from chaudron.infra.schema_revision import (
    REQUIRED_SCHEMA_REVISION,
    SchemaRevisionState,
    classify_schema_revision,
)

_BACKEND_ROOT: Final = Path(__file__).resolve().parent.parent.parent


def _scripts() -> ScriptDirectory:
    """The real migration graph.

    ``script_location`` is overridden with an absolute path for the same reason
    ``tests/conftest.py`` overrides it: the value in ``alembic.ini`` is relative
    and would otherwise be resolved against pytest's working directory.
    """
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _chain() -> list[str]:
    """Every revision, oldest first."""
    return [script.revision for script in reversed(list(_scripts().walk_revisions()))]


def test_the_required_revision_is_the_alembic_head() -> None:
    """If this fails, a migration was added and the constant was not bumped.

    The fix is to bump ``REQUIRED_SCHEMA_REVISION`` in the same commit as the
    migration, not to relax this test: the whole value of the constant is that it
    says which schema the code was compiled against.

    A second head would also fail here, and has to: ``classify_schema_revision``
    orders two revisions on the assumption that there is one line of them.
    """
    assert _scripts().get_heads() == [REQUIRED_SCHEMA_REVISION]


def test_revision_ids_sort_lexically_into_dependency_order() -> None:
    """The invariant the whole comparison rests on, asserted rather than assumed.

    ``alembic.ini`` states it as a naming rule -- "Revision files are named
    ``NNNN_slug.py``: sorted lexically, they are in dependency order, which a bare
    hash never is". ``classify_schema_revision`` compares two revision ids with
    ``<``, so the day somebody lands a hash-style id that rule stops holding and
    the probe would start calling an outdated schema "ahead" and serving traffic
    to it. Failing here instead is the difference between a loud stop and a silent
    wrong answer.
    """
    chain = _chain()

    assert chain == sorted(chain)


def test_the_chain_is_linear() -> None:
    """No branches, no merges: ``walk_revisions`` must describe one line.

    A merge point would make "earlier than the head" ambiguous, and the lexical
    comparison would answer it anyway.
    """
    chain = _chain()

    assert len(chain) == len(set(chain))
    for script in _scripts().walk_revisions():
        assert not isinstance(script.down_revision, tuple), (
            f"revision {script.revision} is a merge point"
        )


# --------------------------------------------------------------------------- #
# The four verdicts
# --------------------------------------------------------------------------- #


def test_the_head_this_build_needs_is_current() -> None:
    state = classify_schema_revision(REQUIRED_SCHEMA_REVISION)

    assert state is SchemaRevisionState.CURRENT
    assert state.is_serviceable


@pytest.mark.parametrize("applied", ["0001", "0002"])
def test_an_earlier_revision_is_outdated(applied: str) -> None:
    """New code, old schema: the state this whole module exists to catch."""
    state = classify_schema_revision(applied)

    assert state is SchemaRevisionState.OUTDATED
    assert not state.is_serviceable


def test_every_revision_before_the_head_is_judged_outdated() -> None:
    """Against the real chain, not a hand-picked example."""
    for revision in _chain()[:-1]:
        assert classify_schema_revision(revision) is SchemaRevisionState.OUTDATED, revision


def test_a_later_revision_is_ahead_and_serviceable() -> None:
    """The expand phase of ``ops/README.md`` §5.4, which is supported on purpose.

    The migration has landed and this process has not been replaced yet. §5.4
    requires every migration to be backward compatible with the code currently
    running precisely so that this window is safe, so refusing readiness here
    would take a healthy instance out of service during the documented procedure.
    """
    state = classify_schema_revision("9999")

    assert state is SchemaRevisionState.AHEAD
    assert state.is_serviceable


def test_nothing_stamped_is_unknown_and_refuses() -> None:
    """Fail closed. An un-migrated database has no ``alembic_version`` at all."""
    state = classify_schema_revision(None)

    assert state is SchemaRevisionState.UNKNOWN
    assert not state.is_serviceable


def test_the_four_states_publish_four_distinct_tokens() -> None:
    """Every state is distinguishable in the body, and the refusals are named.

    Two of the four refuse, and an operator has to be able to tell them apart:
    "you have not migrated this database" and "you deployed ahead of your
    migration" are different faults with different first steps. Collapsing them
    into one token would be the same mistake as the ``200 ready`` this whole
    check replaced — an answer that is not wrong, exactly, but says less than it
    knows.
    """
    states = list(SchemaRevisionState)

    assert len({state.value for state in states}) == len(states) == 4
    assert {state for state in states if not state.is_serviceable} == {
        SchemaRevisionState.OUTDATED,
        SchemaRevisionState.UNKNOWN,
    }
