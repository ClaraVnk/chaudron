# Backend tests

Strategy, and the reasoning behind it, lives in
[`docs/testing-strategy.md`](../../docs/testing-strategy.md) (French). This file is
the operating manual: how to run the suite, what it needs, and how to plug a new
LLM adapter into the conformance harness.

Everything below is run from `backend/`.

## Running

```bash
uv sync --group dev        # once
uv run pytest              # everything
uv run pytest -q --no-cov  # fast loop, no coverage report
```

Useful selections:

```bash
uv run pytest -m "not integration"   # no database needed, milliseconds
uv run pytest -m contract            # LLM adapter conformance only
uv run pytest tests/tenancy          # tenant-isolation guards
uv run pytest -q -k llm_provider     # by name
```

A run that ends with skips is normal: the LLM adapters are not all registered yet,
and every test that cannot run says why. `-ra` (already in `addopts`) prints the
reasons — read them, they are the backlog.

## Markers

| Marker | Meaning | Runs in PR CI |
|---|---|---|
| `integration` | Needs a live PostgreSQL 16 | yes |
| `contract` | LLM adapter conformance, against doubles, no network | yes |
| `live_provider` | Calls a real provider with real credentials and real money | **no** |

`integration` is applied automatically to any test requesting a database fixture
(`pytest_collection_modifyitems` in `conftest.py`). Do not add it by hand: a marker
maintained by hand is a marker that drifts, and `-m "not integration"` then hangs on
a test nobody re-tagged.

## Database prerequisites

PostgreSQL 16, always. There is no SQLite mode, not even for unit tests
(ADR-0003) — `tests/test_database_harness.py` fails the build if one ever appears.

The database is resolved in this order:

1. `CHAUDRON_TEST_DATABASE_URL` — explicit override, wins over everything.
2. `CHAUDRON_DATABASE_URL` — what CI sets for its `postgres:16` service container.
3. An ephemeral container started by testcontainers through Podman.

If none is reachable, the database fixtures **skip** with the reason. They never
fall back to another engine.

### Podman

Testcontainers speaks the Docker HTTP API; Podman implements it. Two things must be
true, and the second one is the trap.

**1. The socket must be running and `DOCKER_HOST` must point at it.**

```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/podman/podman.sock"
```

`conftest.py` finds the rootless socket on its own (`$XDG_RUNTIME_DIR/podman/podman.sock`,
then `/run/podman/podman.sock`) when `DOCKER_HOST` is unset, so the export is only
needed for other tools. Exporting it in `.envrc` is the convenient option.

**2. Ryuk must be disabled.** `TESTCONTAINERS_RYUK_DISABLED=true`.

Ryuk is the reaper container testcontainers starts to clean up after a crashed run.
It bind-mounts the container socket and expects to run privileged; under rootless
Podman it never becomes ready. The failure is reported against the *database*
container — `TimeoutError: container did not become running` — which sends you
debugging PostgreSQL for an hour. `conftest.py` sets the variable whenever the
socket looks like Podman, so this is handled; it is documented because anything
else driving testcontainers (a script, an IDE runner) needs it too.

Containers started by the suite are stopped by the session fixture, so nothing is
left behind except after a hard kill of the interpreter. To sweep manually:

```bash
podman ps -a --filter "label=org.testcontainers=true"
podman rm -f $(podman ps -aq --filter "label=org.testcontainers=true")
```

Under SELinux (Enforcing here) nothing extra is required: the suite bind-mounts no
host directory. Any future fixture that does must pass `:Z`.

### Schema

`conftest.py` brings the database to `head` with **Alembic**, exactly as a deployment
does — not `metadata.create_all`, which would validate a schema no environment ever
applies and let a broken migration reach production behind a green suite. The
reference tables (`unit`, `llm_provider`) are therefore seeded by revision `0002`
and are available to every test.

The URL is passed to Alembic programmatically, so the suite needs no application
secrets. A database that already carries a hand-built schema (from an older
`create_all` run) will fail here: drop it and let the suite migrate it.

## Fixtures

| Fixture | Scope | What it gives |
|---|---|---|
| `postgres_url` | session | A reachable PostgreSQL 16 URL, from the environment or a container |
| `initialised_database` | session | Same URL, schema created |
| `engine` | function | `AsyncEngine`, `NullPool` |
| `db_session` | function | `AsyncSession` rolled back after the test, `commit()` included |
| `make_household` / `make_user` / `make_member` | function | Factories, all arguments optional |
| `tenant_pair` | function | Two unrelated households with their owners — the seed of isolation tests |
| `api_app` | function | The real `create_app()` application, with only the session dependency overridden |
| `api_client` | function | `httpx.AsyncClient` speaking to `api_app` in-process over ASGI |

`api_app` overrides **one** dependency: the request-scoped session, so handler writes
join the test transaction. The household resolution is deliberately *not* overridden —
tests send `X-Household-Id` like a real client (`tests.conftest.household_headers`) and
get the real `401` when they do not. Overriding it would bypass the only code the
isolation tests exist to exercise. `tests/api/conftest.py` adds `make_location`,
`make_product` and `RecordingCatalog`, a scripted stand-in for Open Food Facts.

`db_session` opens a transaction on the connection and joins the session to it with
`join_transaction_mode="create_savepoint"`. Code under test may commit; the outer
rollback still wipes everything. No truncation between tests, no ordering coupling.

## Adding an LLM adapter to the conformance harness

`tests/contracts/test_llm_provider_contract.py` is parametrised over the five
providers of ADR-0005 and currently skips: nothing is registered. It activates on
its own, one provider at a time — **you never edit the test file to add a provider**.

Create `chaudron/infra/llm/contract.py` exposing `CONTRACT_ADAPTERS`, a mapping from
provider key (`anthropic`, `openai`, `gemini`, `mistral`, `ollama`) to an object
matching this shape (structural, so infrastructure never imports the test package):

```python
class ContractAdapter(Protocol):
    key: str

    def build_port(self, port_name: str, scenario: str) -> object:
        """Return the port implementation wired to a double replaying `scenario`.

        `port_name` is "RecipeSuggester" or "ReceiptExtractor".
        Raise LookupError when the port is not offered at all.
        """

    def capabilities(self) -> object:
        """The ProviderCapabilities value for the tested (provider, model) pair."""

    def degradation_for(self, capability: str) -> str | None:
        """None when the capability is present, else "emulated" | "degraded" | "unavailable"."""
```

The scenarios the harness asks for, and what each must produce:

| Scenario | Expected outcome |
|---|---|
| `nominal` | A valid domain object |
| `connection_refused`, `timeout`, `server_error` | `ProviderUnavailable` |
| `rate_limited`, `quota_exhausted` | `ProviderQuotaExceeded` |
| `malformed_payload`, `schema_violation` | `ProviderResponseInvalid` |
| `missing_<capability>` | The behaviour declared by `degradation_for` |

The double lives with the adapter, not here: only the adapter knows what a rate
limit looks like on *its* wire. Build it from recorded responses of the real
provider (`tests/contracts/recordings/<provider>/`), not from hand-written JSON — a
double invented from the documentation tests your reading of the documentation.

Capability keys: `structured_output`, `vision`, `prompt_caching`, `long_context`.
`capabilities()` must expose `supports_<key>` booleans, a `source` of `"static"` or
`"probed"`, and `probed_at` — timezone-aware when probed, `None` when static
(ADR-0005: the provenance is part of the value, because only a probed capability can
go stale).

Then run:

```bash
uv run pytest -m contract -q
```

Every skip that turns into a pass is one line of the ADR-0005 contract honoured.

## Live provider tests

`live_provider` tests call real APIs with real credentials and cost real money.
They never run on a pull request. See section 5.4 of the strategy document for when
they do run and what they are allowed to assert.
