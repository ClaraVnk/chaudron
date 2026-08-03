<!--
Thanks for the pull request.

Keep it to one intention. Beyond roughly 400 lines of meaningful diff (lockfiles and
generated files do not count), split it — a smaller pull request gets reviewed sooner and
merged more often.

Fill in the four sections below; they are the structure the project uses everywhere. Delete
these comments, not the headings.
-->

## Context

<!--
Why this change exists. Link the issue, discussion or ADR it comes from
(`Closes #123`, `Refs ADR-0006`). If it comes from none of those, say what prompted it.

Pantry is in the scoping phase: if this adds feature code, state where the design for it was
agreed.
-->

## Changes

<!--
What you actually did, as a reviewer needs to read it — not a restatement of the diff.

Call out anything a reader would not infer from the code: a dependency added or removed, a
schema change, a new environment variable (which also belongs in `.env.example`), a changed
default, a decision you made along the way that could reasonably have gone the other way.
-->

## Tests

<!--
How you know this works. Commands you ran and their outcome; new tests and what they cover;
manual verification steps for anything not covered by an automated test.

"Not verified, no environment available" is a legitimate answer and is worth more than an
implied green. Do not claim a step you did not run.
-->

## Risks

<!--
What could go wrong, who notices first, and how to undo it.

Migrations: state the rollback (a `down` revision, or a documented plan). Behaviour changes:
say what breaks for an existing instance. Security-relevant surfaces — tenant isolation,
household provider keys, the Ollama URL allowlist, the inbound-email webhook, receipt images
— say what you checked.

"None" is a claim, and it will be read as one.
-->

---

## Checklist

Definition of done, in order — tick what you actually ran:

- [ ] The code runs / imports without error
- [ ] `uv run ruff format .` applied
- [ ] `uv run ruff check .` reports no new warning
- [ ] `uv run mypy` passes (strict)
- [ ] `uv run pytest` passes against a real PostgreSQL instance
- [ ] `podman build` of the API image succeeds
- [ ] N/A — this pull request touches no Python (docs, ops or CI only)

Conventions:

- [ ] Commits follow Conventional Commits (`feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `style`, `revert`; `!` or a `BREAKING CHANGE:` footer for a break)
- [ ] Branch is named `feat/…`, `fix/…`, `chore/…`, `refactor/…` or `docs/…`
- [ ] Every commit compiles and passes the tests on its own
- [ ] One intention, and under ~400 lines of meaningful diff
- [ ] Everything new is written in English (code, identifiers, comments, docs)
- [ ] A structural decision here is recorded in a new `docs/adr/` entry — or none was needed
- [ ] `.env.example` updated if a configuration key was added, changed or removed
- [ ] `uv.lock` regenerated and committed alongside any `pyproject.toml` change

Nothing in this pull request:

- [ ] No `docker` command anywhere — in code, docs, scripts or CI (`podman`, or `${CONTAINER_ENGINE:-podman}`). The `docker.io/…` registry hostname and `podman build --format docker` are not commands and are fine
- [ ] No SQLite, anywhere — not as a fallback, not as a test convenience. PostgreSQL only
- [ ] No hardcoded secret: no key, token, password, DSN with credentials or certificate, and no real value in `.env.example`
- [ ] No AI assistant or editor configuration committed (`.claude/`, `CLAUDE.md`, `.claude.json`, `settings.local.json`, `.idea/`, `.vscode/`), and no assistant credited in a commit message
- [ ] No new business table or query missing `household_id`, and no tenant derived from client-controlled input
- [ ] No `import sqlalchemy`, HTTP client or provider SDK inside `domain/` or `services/` — the arrows point inward
- [ ] No disabled test, loosened lint rule, or `# noqa` without a comment explaining why

Licence:

- [ ] I have the right to contribute this code, and I agree it is licensed under AGPL-3.0-or-later
- [ ] Any dependency added is licence-compatible with AGPL-3.0-or-later
