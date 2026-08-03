# Contributing to Chaudron

Thanks for looking. Chaudron is a small, opinionated, self-hostable project, and it
is at the stage where an outside opinion is worth more than an outside patch.

Everything below is meant to make a contribution predictable — for you, because
you know in advance what will be asked of it, and for the maintainer, because
review time is the scarcest resource here.

---

## 1. Where the project is right now

**Chaudron is in the scoping phase.** This repository contains architecture
documents, architecture decision records, a project skeleton, container units
and a CI pipeline. It contains **no feature code**: there is no working
application, no release, and nothing to install and use. Do not expect to run it
and get recipe suggestions.

That shapes what is useful today.

### Most useful right now

- **Reading [`docs/architecture.md`](docs/architecture.md),
  [`docs/data-model.md`](docs/data-model.md) and the ADRs in
  [`docs/adr/`](docs/adr/), and disagreeing in public.** An ADR that turns out to
  be wrong costs an afternoon to replace now, and a data migration later.
- **Poking holes in a rejected alternative.** Every ADR has an
  `Alternatives écartées` section. If one was dismissed for a reason that does
  not hold, that is a genuinely valuable issue — bring the new argument, not just
  the preference.
- **Reporting design-level security problems**, especially around tenant
  isolation, the SSRF surface of the household-supplied Ollama URL, and the
  storage of household provider keys. See [`SECURITY.md`](SECURITY.md) — those go
  privately, not into an issue.
- **Operational review**: the quadlet units in [`ops/`](ops/), the SELinux notes,
  the container image, the CI workflow.
- **Filling gaps in the skeleton**: the CI, the ops docs, the configuration
  template. Small, self-contained, reviewable.

### Not useful right now

- **Large feature implementations.** The domain model is not settled. Code
  written against an unsettled model gets rewritten, and rewriting someone
  else's donated work is unpleasant for everybody. If you want to build a
  feature, open a discussion first and let's agree on the shape.
- **Frontend scaffolding.** `frontend/` is empty on purpose; the CI job for it is
  already written and activates itself the day `frontend/package.json` appears.
  That first commit is a decision, so it goes through a discussion.
- **Dependency additions** that arrive inside an unrelated pull request.

When in doubt: **open an issue or a discussion before writing code.** A rejected
pull request is a worse outcome than a five-minute conversation.

---

## 2. Setting up a development environment

### 2.1 Prerequisites

| Tool | Why | Check |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | Python toolchain, dependencies, lockfile | `uv --version` |
| Python 3.14 | Target runtime, pinned in `backend/.python-version` | `uv python install 3.14` |
| Podman 5.x | Container engine. **Not Docker** — see §4.6 | `podman --version` |
| Node.js 22+ | Only once the frontend exists | `node --version` |

Rootless Podman with a working `systemctl --user` session is what the ops
documentation assumes. On a SELinux host, keep it enforcing and read
[`ops/README.md`](ops/README.md) §3 rather than reaching for `setenforce 0`.

### 2.2 Clone and configure

```sh
git clone https://github.com/ClaraVnk/chaudron.git
cd chaudron
cp .env.example .env
```

Fill `.env` in. Every key in `.env.example` is read at startup and validated —
the application is designed to fail fast on a missing or malformed value rather
than to run half-configured. `.env` is gitignored and must stay that way;
`.env.example` is the source of truth for *which* keys exist, and it never
carries a value that matters.

Generate the secrets locally, do not invent them by hand:

```sh
openssl rand -hex 32   # CHAUDRON_SECRET_KEY
```

### 2.3 Start PostgreSQL 16

Chaudron uses PostgreSQL and only PostgreSQL, including for tests (§4.5). The
password goes in through a Podman secret, never as a command-line argument —
arguments are visible in `ps` and land in your shell history:

```sh
podman network create chaudron-net
mkdir -p ~/chaudron/data/postgres
```

```sh
read -rs -p 'Postgres password: ' PW && printf '%s' "$PW" | podman secret create chaudron-db-password - && unset PW
```

```sh
podman run -d --name chaudron-db \
  --network chaudron-net \
  -p 127.0.0.1:5432:5432 \
  --secret chaudron-db-password,type=env,target=POSTGRES_PASSWORD \
  -e POSTGRES_DB=chaudron \
  -e POSTGRES_USER=chaudron \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v ~/chaudron/data/postgres:/var/lib/postgresql/data:Z \
  docker.io/library/postgres:16
```

Two details that are not optional:

- **`:Z` on the bind mount.** Under SELinux enforcing, a bind mount without a
  container label is denied. `:Z` applies a private label; `:z` a shared one.
- **`docker.io/library/postgres:16` is a registry hostname**, not the Docker CLI.
  Podman needs the registry to be fully qualified. This is the one place the
  string appears legitimately.

Set `CHAUDRON_DATABASE_URL` in `.env` accordingly:

```
CHAUDRON_DATABASE_URL=postgresql+asyncpg://chaudron:<password>@127.0.0.1:5432/chaudron
```

`ops/README.md` §1 covers the same ground in more depth, plus teardown.

### 2.4 Install the backend toolchain

```sh
cd backend
uv sync --locked --group dev
```

`--locked` fails instead of silently re-resolving `uv.lock`. If it fails, your
change to `pyproject.toml` needs a regenerated lockfile — that is a real change,
commit it.

### 2.5 What does not work yet, and why that is expected

`backend/src/chaudron/domain/models.py` holds the full schema — 17 tables, and it
does create against a real PostgreSQL 16. Everything above it is still empty.
So:

- **There is no ASGI application to serve**, and no Alembic migration yet. The
  schema exists as SQLAlchemy metadata; the first migration has to be generated
  from it.
- **The four checks below all pass today.** `uv run pytest` reports roughly
  `55 passed, 145 skipped`. Every skip carries a reason: most are the LLM adapter
  conformance suite waiting for its first adapter, the rest are documented
  tenancy exemptions. A skip without a reason is a bug — do not add one.
- **The conformance suite arms itself.** Register an adapter in
  `chaudron.infra.llm.contract.CONTRACT_ADAPTERS` and 140 skipped tests start
  running against it. See `backend/tests/README.md`.
- The frontend job in CI detects the absence of `frontend/package.json` and skips
  itself.

So the commands in §3 are meaningful now: run them before you push, and expect
them green.

---

## 3. Lint, types, tests, build

Run all of these from `backend/`. Any pull request touching Python must be clean
on all of them; CI runs exactly the same commands, in this order.

```sh
uv run ruff format .          # 1. format (rewrites files)
uv run ruff check .           # 2. lint
uv run mypy                   # 3. types — strict, paths come from pyproject.toml
uv run pytest                 # 4. tests, against real PostgreSQL
```

Notes:

- **`ruff format` in CI is a check, never a fix** (`ruff format --check --diff`).
  Format locally; do not expect the pipeline to tidy up after you.
- **`ruff check --fix` is fine locally**, but read the diff. Some `S` (bandit)
  findings deserve a real fix, not a `# noqa`. A `# noqa` needs a comment saying
  why.
- **mypy runs in strict mode** with `warn_unreachable` and `disallow_any_generics`.
  `Any` is a smell; `type: ignore` needs an error code and a reason.
- **Tests use `testcontainers[postgres]`**, which starts a real PostgreSQL 16
  container. Your container engine must be reachable for the integration tests to
  run.
- The linter bans relative imports and stray `print()`, and rejects naive
  datetimes (`DTZ`). Timezone-aware or nothing.

Building the image (step 5 of §4.4):

```sh
podman build --format docker -t localhost/chaudron-api:dev -f backend/Containerfile backend
```

`--format docker` names an **image format**, not an engine: Podman's default OCI
output drops the `HEALTHCHECK` instruction. The command is `podman build`.

---

## 4. Conventions

These are not negotiable inside a pull request, but they are negotiable in an
issue. If one of them is wrong, argue it separately.

### 4.1 Commit messages — Conventional Commits

```
<type>(<optional scope>): <short imperative description>

[optional body]

[optional footer]
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`,
`style`, `revert`.

A breaking change is marked with `!` after the type (`feat!:`) or with a
`BREAKING CHANGE:` footer.

```
feat(inventory): add expiry date to stock items
fix(auth): reject tokens signed with a rotated key
docs(adr): record the decision to skip retailer drive integrations
chore(deps): pin anthropic to 0.120.2
```

### 4.2 Branches

`feat/<description>`, `fix/<description>`, `chore/<description>`,
`refactor/<description>`, `docs/<description>`. Include a ticket id when one
exists: `feat/ABC-123-add-sso`.

### 4.3 Pull requests

- **One intention per pull request.** Small and atomic beats complete.
- **Beyond roughly 400 lines of meaningful diff, split it.** Generated files and
  lockfiles do not count toward that; hand-written code and prose do.
- **Every commit compiles and passes the tests on its own.** Rebase and clean up
  your history before asking for review; "fix lint" commits get squashed away.
- The description follows the template — **Context, Changes, Tests, Risks** —
  and [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) is
  filled in, not deleted. The *Risks* section saying "none" is a claim, and it
  will be read as one.

### 4.4 Definition of done

In this order. A step is not skipped because the previous one "obviously" passes:

1. The code runs / imports without error.
2. `ruff format` has been applied.
3. `ruff check` reports no new warning.
4. The relevant unit and integration tests pass.
5. The final build succeeds (`podman build` for the API image).

If a step cannot be verified — no test exists for that path, no environment
available — **say so in the pull request**. An honest "not verified" is worth
more than an implied green.

### 4.5 PostgreSQL only, never SQLite

Chaudron targets PostgreSQL 16 with `asyncpg`, in development, in CI, in tests and
in production ([ADR 0003](docs/adr/0003-backend-stack.md)). SQLite is not a
supported fallback and not a test convenience.

The reasons are load-bearing, not stylistic: SQLite serialises writes, has no
real type system (booleans as 0/1, naive timestamps, JSON as text), and a schema
tested on it is not the schema that runs in production. A test suite that passes
on SQLite proves nothing about the engine that actually serves users.

### 4.6 Podman only, never Docker

The container engine is Podman: `podman build`, `podman run`, `podman secret`,
rootless systemd quadlets. Not a preference — the target platform is Rocky Linux
with SELinux enforcing and rootless containers, and the ops documentation depends
on it.

No `docker` command may appear in a contribution: not in documentation, not in a
script, not in a Makefile, not in CI. Where portability across engines is genuinely
needed, use `${CONTAINER_ENGINE:-podman}`.

Two false positives that are fine, because neither is the Docker CLI: the
`docker.io/...` registry hostname, and `podman build --format docker`, which names
an image format.

### 4.7 English, everywhere

Everything versioned is in English: code, identifiers, inline comments, commit
messages, pull request descriptions, documentation, log messages, error strings.

**One inherited exception:** several scoping documents under `docs/` — including
the architecture document and the existing ADRs — are written in French, with
technical identifiers in English. They stay as they are; do not open a pull
request to translate them. **New contributions are in English**, including new
ADRs.

### 4.8 Dependencies

Pinned exactly, no ranges, no `latest`. Adding one means: is there a standard
library or existing-dependency equivalent? Is the licence compatible with
AGPL-3.0-or-later? Is it actively maintained? Any known vulnerabilities
(`pip-audit`)? Is the API surface proportionate to the need?

`uv add <pkg>` updates `pyproject.toml` and `uv.lock` together. Commit both.

### 4.9 Secrets

No hardcoded secret, ever — no token, key, password, connection string or
certificate. Configuration comes from the environment; `.env` is gitignored;
`.env.example` documents the keys with no values.

CI runs `gitleaks` over the full history. If you leak something, say so
immediately: it has to be rotated, and a `git reset` does not rotate anything.

---

## 5. Proposing an architecture decision

Structural decisions live in [`docs/adr/`](docs/adr/), in Michael Nygard's
format, versioned with the code
([ADR 0001](docs/adr/0001-record-architecture-decisions.md)).

### When a decision needs an ADR

When it commits the project to an external dependency that is hard to remove,
constrains the data model, defines a boundary between layers or services, rules
out a feature someone could reasonably expect, or creates a recurring cost —
financial, operational or maintenance.

**Not** for: a replaceable utility library, a naming convention, an
implementation detail local to one module.

### How to propose one

1. **Open a discussion or an issue first.** An ADR is a conclusion; the argument
   comes before it. Writing 800 words to have the premise rejected wastes your
   evening.
2. Once the direction is agreed, add `docs/adr/NNNN-title-in-kebab-case.md`, with
   the next sequential number, on a `docs/` branch.
3. Follow the imposed structure: `# NNNN. Title`, then `## Statut` (Accepted /
   Rejected / Superseded by ADR-NNNN, with a date), `## Contexte`,
   `## Décision`, `## Conséquences` — positive and negative kept separate —
   `## Alternatives écartées` with one reason per alternative, and `## Révision`
   when a concrete signal could reopen the decision. New ADRs are written in
   English; keep the section headings consistent with the existing files so the
   corpus stays navigable.
4. **Be honest in the negative consequences.** An ADR with no drawbacks has not
   been thought through, and that section is the part a reviewer reads first.

### ADRs are immutable

An accepted ADR is not edited. You write a new one that supersedes it and mark
the old one `Remplacé par ADR-NNNN`. The record of abandoned decisions is worth
as much as the record of live ones — it is what stops the same idea being
re-litigated every six months.

---

## 6. What will be refused

Not to be unwelcoming — to save you the work. A pull request is refused if it:

- **Introduces SQLite** anywhere, including as a test-only convenience (§4.5).
- **Contains a `docker` command** in code, docs, scripts or CI (§4.6).
- **Hardcodes a secret**, or adds a real value to `.env.example` (§4.9).
- **Versions assistant or editor configuration** — `.claude/`, `CLAUDE.md`,
  `.claude.json`, `settings.local.json`, `.idea/`, `.vscode/`. These are
  gitignored; a pull request that un-ignores them is refused.
- **Breaks the dependency rule.** `api → services → domain ← infra`, arrows point
  inward only. An `import sqlalchemy` in `domain/` or `services/` is an
  architecture bug, not a shortcut.
- **Drops `household_id`** from a business table or from a query. Tenant isolation
  is a convention enforced by review, which is precisely why review enforces it
  ([ADR 0006](docs/adr/0006-multi-tenant-from-day-one.md)).
- **Derives the current tenant from client-controlled input** — a header, a
  subdomain, a body field. It comes from the authenticated session, only.
- **Calls a provider SDK from a handler.** Model access goes through the domain
  ports ([ADR 0005](docs/adr/0005-llm-provider-abstraction.md)).
- **Adds a mode where the application pays for someone else's inference.** That
  is a deliberate design decision, not a gap
  ([ADR 0007](docs/adr/0007-byok-and-local-inference.md)).
- **Reintroduces retailer drive integration** by scraping or reverse-engineered
  mobile endpoints ([ADR 0002](docs/adr/0002-no-retailer-drive-integration.md)).
- **Weakens the SSRF allowlist** on the household-supplied Ollama URL in the name
  of convenience.
- **Bypasses CI**: disabled tests, loosened lint rules, `# noqa` without a reason,
  a skipped security scan.
- **Mixes unrelated changes**, or lands a large feature that was never discussed.
- **Is machine-generated and unreviewed.** Tooling is your business; the pull
  request is yours, you are expected to understand every line and to answer for
  it in review. Do not credit an assistant in a commit message.

---

## 7. Licence and what it means for you

Chaudron is licensed under **AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)).

**By contributing, you agree that your contribution is licensed under
AGPL-3.0-or-later.** There is no contributor licence agreement and no copyright
assignment: you keep the copyright on what you write, the project simply
distributes it under its licence. There is no relicensing to a permissive licence
later, and no proprietary edition — the copyright is spread across contributors,
which is a feature.

Practically, this means:

- **Anyone who distributes a modified Chaudron must publish their source**, under
  the same licence.
- **Section 13 is the point of the AGPL**: if someone runs a modified Chaudron as a
  network service that other people use, they must offer those users the modified
  source. Self-hosting your own instance for your own household triggers nothing —
  you are not providing a service to anyone else.
- **Every dependency must be AGPL-compatible.** Permissive licences (MIT, BSD,
  Apache-2.0, ISC) are fine. A GPL-incompatible or source-available licence
  (SSPL, BUSL, "non-commercial") is a blocker, whatever its technical merits.
- **Only contribute code you have the right to contribute.** Not your employer's
  under a contract that claims it, not copied from an incompatibly licensed
  project, not lifted from an answer whose licence you have not checked. If a
  snippet came from somewhere, say where.

If any of that is a problem for your situation, raise it before you write the
code, not in review.

---

## 8. Reporting

- **Bug** → [bug report form](https://github.com/ClaraVnk/chaudron/issues/new?template=bug_report.yml).
  Never paste an API key into it.
- **Idea or feature** → [feature request form](https://github.com/ClaraVnk/chaudron/issues/new?template=feature_request.yml).
- **Question, or disagreement with an ADR** →
  [Discussions](https://github.com/ClaraVnk/chaudron/discussions).
- **Security vulnerability** → **not an issue.** See [`SECURITY.md`](SECURITY.md):
  private advisory, or kevin@stackops.ch.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

Maintainer: ClaraVnk. Reviews happen when they happen — this is a spare-time
project, and a slow reply is not a rejection.
