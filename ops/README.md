# Chaudron — Operations

Build, run and deploy Chaudron with **Podman**. There is no Docker anywhere in
this project: the container engine is Podman, images are built from
`Containerfile`s, and services run as rootless systemd **quadlets**.

Target platform: **Rocky Linux 10, SELinux Enforcing**, rootless Podman under a
dedicated unprivileged user.

---

## Contents

| File | Purpose |
| --- | --- |
| `chaudron.container` | Quadlet unit for the API |
| `chaudron-db.container` | Quadlet unit for PostgreSQL 16 |
| `chaudron-migrate.container` | One-shot Alembic runner, started by hand only |
| `podman-auto-update.timer.d/override.conf` | 15-minute polling for the API image |

---

## 1. Local development

### 1.1 Prerequisites

```sh
podman --version          # 5.x or later
systemctl --user status   # rootless systemd session must be available
getenforce                # expect: Enforcing
```

If a script must stay portable across engines, use
`${CONTAINER_ENGINE:-podman}` rather than hardcoding another engine.

### 1.2 Create the shared network

Both containers resolve each other by name on a user-defined network.

```sh
podman network create chaudron-net
```

### 1.3 Create the data directories

```sh
mkdir -p ~/chaudron/data/postgres ~/chaudron/data/uploads
```

### 1.4 Start PostgreSQL 16

The password is passed through a Podman secret, never on the command line
(arguments are visible in `ps` and land in shell history):

```sh
read -rs -p 'Postgres password: ' PW && printf '%s' "$PW" | podman secret create chaudron-db-password - && unset PW
```

```sh
podman run -d --name chaudron-db \
  --network chaudron-net \
  --secret chaudron-db-password,type=env,target=POSTGRES_PASSWORD \
  -e POSTGRES_DB=chaudron \
  -e POSTGRES_USER=chaudron \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v ~/chaudron/data/postgres:/var/lib/postgresql/data:Z \
  --health-cmd 'pg_isready -U chaudron -d chaudron' \
  docker.io/library/postgres:16
```

> **`:Z` is not optional.** Under SELinux Enforcing, a bind mount without a
> container label is denied. `:Z` applies a **private** label (only this
> container may read the directory); `:z` applies a **shared** label. Use `:Z`
> unless two containers genuinely need the same directory.

Wait until it is healthy:

```sh
podman healthcheck run chaudron-db && podman inspect -f '{{ .State.Health.Status }}' chaudron-db
```

### 1.5 Build the API image

`HEALTHCHECK` is a Docker-format instruction. Podman's default OCI output
format drops it with a warning, so pass `--format docker` when you want the
baked-in healthcheck in the image. (Quadlet deployments declare `HealthCmd=`
themselves and do not depend on it.)

```sh
podman build --format docker -t localhost/chaudron-api:dev -f backend/Containerfile backend
```

### 1.6 Run the API

```sh
podman run -d --name chaudron \
  --network chaudron-net \
  -p 127.0.0.1:8000:8000 \
  --env-file ./.env \
  -v ~/chaudron/data/uploads:/var/lib/chaudron/uploads:Z \
  localhost/chaudron-api:dev
```

Check both endpoints — they are deliberately separate:

```sh
curl -fsS http://127.0.0.1:8000/healthz   # liveness: process is up, no dependency check
curl -fsS http://127.0.0.1:8000/readyz    # readiness: database reachable, migrations applied
```

A failing `/readyz` with a passing `/healthz` means the process is alive but a
dependency is down — do not restart the container, fix the dependency.

### 1.7 Tear down

```sh
podman rm -f chaudron chaudron-db
podman network rm chaudron-net
```

---

## 2. Deployment — rootless systemd quadlets

Quadlets are declarative container units that systemd generates services from.
They run under an unprivileged user account with lingering enabled, so services
survive logout and start at boot.

### 2.1 Prepare the service account

```sh
sudo useradd --create-home --shell /usr/sbin/nologin chaudron
sudo loginctl enable-linger chaudron
```

Everything below runs **as that user**:

```sh
sudo -u chaudron XDG_RUNTIME_DIR=/run/user/$(id -u chaudron) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u chaudron)/bus \
  systemctl --user <command>
```

Without `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`, `systemctl --user`
fails with *"Failed to connect to bus"*.

### 2.2 Install the units

```sh
install -d -m 0755 ~chaudron/.config/containers/systemd
install -m 0644 ops/chaudron.container ops/chaudron-db.container \
  ops/chaudron-migrate.container \
  ~chaudron/.config/containers/systemd/
```

`chaudron-migrate.container` is installed but never enabled: it has no
`[Install]` section, so it only ever runs when somebody starts it.

Create the runtime state the units expect:

```sh
podman network create chaudron-net
mkdir -p ~/chaudron/data/postgres ~/chaudron/data/uploads
```

### 2.3 Create the secrets

Each command is a single line with masked input, transmitted over stdin, and
run **on the server as the `chaudron` user**. `printf '%s'` matters: `echo` and
`podman secret create <file>` both keep a trailing newline, which is invisible
until the value crosses an HTTP header or an HTML form and silently fails.

```sh
read -rs -p 'DB password: ' V && printf '%s' "$V" | podman secret create chaudron-db-password - && unset V
```

```sh
read -rs -p 'App secret key: ' V && printf '%s' "$V" | podman secret create chaudron-secret-key - && unset V
```

```sh
read -rs -p 'Anthropic API key: ' V && printf '%s' "$V" | podman secret create chaudron-anthropic-api-key - && unset V
```

```sh
read -rs -p 'Inbound email webhook key: ' V && printf '%s' "$V" | podman secret create chaudron-inbound-email-key - && unset V
```

### 2.4 Non-secret configuration

Copy `.env.example` to `~chaudron/chaudron.env`, fill in the non-secret keys, and
restrict its permissions. `EnvironmentFile=` in `chaudron.container` points at it.

```sh
install -m 0600 /dev/null ~chaudron/chaudron.env
```

### 2.5 Start

```sh
systemctl --user daemon-reload
systemctl --user start chaudron-db.service
systemctl --user start chaudron.service
systemctl --user status chaudron.service
journalctl --user -u chaudron.service -f
```

Quadlet units are **not** enabled with `systemctl enable`. The generator reads
`[Install] WantedBy=default.target` from the `.container` file, so a
`daemon-reload` is all that is required.

### 2.6 Update to a new image

```sh
podman pull <registry>/chaudron-api:<tag>
podman tag <registry>/chaudron-api:<tag> localhost/chaudron-api:latest
systemctl --user restart chaudron.service
```

Rollback is the same sequence with the previous tag. Always keep the previous
image on the host until the new one has passed `/readyz`.

---

## 3. SELinux checklist

Do **not** run `setenforce 0`. Every problem below has a targeted fix.

| Symptom | Check | Fix |
| --- | --- | --- |
| Container cannot read/write a bind mount | `ls -Z ~/chaudron/data/postgres` | Add `:Z` to the `Volume=` line, or `restorecon -Rv <path>` |
| Denials with no obvious cause | `sudo ausearch -m AVC -ts recent` | Feed the output to `audit2why` before changing anything |
| Service binds a non-standard port | `sudo semanage port -l \| grep <port>` | `sudo semanage port -a -t http_port_t -p tcp <port>` |
| Reverse proxy cannot reach the API | `getsebool httpd_can_network_connect` | `sudo setsebool -P httpd_can_network_connect on` |

Useful one-liners:

```sh
getenforce
ls -Z ~/chaudron/data
ps -eZ | grep chaudron
sudo ausearch -m AVC -ts recent | audit2why
```

### The `:U` trap

Never add `:U` to a volume in a quadlet. It chowns the directory to the
container's **declared** user at start time, not the runtime one — the first
start works and every subsequent start breaks with permission errors. Set
ownership on the host once instead:

```sh
podman unshare chown -R 10001:10001 ~/chaudron/data/uploads
```

---

## 4. Backups

PostgreSQL is backed up with `pg_dump`, not by copying the data directory.

```sh
podman exec chaudron-db pg_dump -U chaudron -d chaudron --format=custom > chaudron-$(date -I).dump
```

Restore into an empty database:

```sh
podman exec -i chaudron-db pg_restore -U chaudron -d chaudron --clean --if-exists < chaudron-YYYY-MM-DD.dump
```

Verify a restore against a scratch database before any migration that drops or
rewrites data.

---

## 5. Continuous deployment

Production follows `ghcr.io/claravnk/chaudron:latest`. A merge to `main` reaches
the server within 15 minutes, unattended.

### How the chain fits together

```
merge to main → CI passes → publish.yml checks the commit is on main
                                        ↓
                        builds, pushes :latest, signs the digest (cosign)
                                        ↓  (≤ 15 min)
              podman-auto-update.timer sees a new digest
                                        ↓
              chaudron.service restarts on the new image
                                        ↓
              health check fails? → Podman rolls back automatically
```

The signature is not checked by the host — see *Image provenance* below for what
that does and does not buy you.

Only the API is in this loop. `chaudron-db.container` deliberately carries no
`AutoUpdate=`: the database restarting itself at a moment nobody chose, with no
backup taken first, is not a trade worth making for a component that cannot roll
its own state back. Update it by hand, after a dump — the unit file spells out
the sequence.

Two units make this work, and both must be installed:

```sh
# 1. The quadlet already declares AutoUpdate=registry (see chaudron.container).

# 2. Override the stock timer, which only fires once a day.
mkdir -p ~/.config/systemd/user/podman-auto-update.timer.d
cp podman-auto-update.timer.d/override.conf \
   ~/.config/systemd/user/podman-auto-update.timer.d/

systemctl --user daemon-reload
systemctl --user enable --now podman-auto-update.timer
systemctl --user list-timers podman-auto-update.timer
```

Force a check without waiting:

```sh
podman auto-update             # apply
podman auto-update --dry-run   # report what would change, touch nothing
```

Keep the session alive across logouts, or the timer dies with it:

```sh
loginctl enable-linger "$USER"
```

### Image provenance — what `:latest` does and does not guarantee

Read this before deciding that continuous deployment is acceptable here.

`AutoUpdate=registry` follows a **mutable tag**. Every 15 minutes the host asks
GHCR what digest `ghcr.io/claravnk/chaudron:latest` points at, and runs whatever
comes back. It does not ask who put it there. Anyone able to push that tag — a
stolen `packages: write` token, a compromised maintainer account, a workflow
that can be tricked into building somebody else's commit — owns the production
container within a quarter of an hour, with no human in the loop.

Two things narrow that, and it is worth being precise about which is which.

**What is closed.** `publish.yml` refuses to build anything that is not a commit
already reachable from `refs/heads/main` in this repository, verified against
the repository itself rather than against the event payload. A pull request from
a fork — including one whose branch is called `main`, which used to satisfy the
`workflow_run` branch filter — never reaches the build step.

**What is now provable.** Each published digest is signed with cosign in keyless
mode: the runner exchanges its GitHub OIDC token for a short-lived Fulcio
certificate, and the signature is logged in Rekor. No signing key exists to be
stolen. The certificate records *which workflow, in which repository, on which
ref* produced the image, so a signature cannot be forged by someone who merely
holds a registry token.

Verify a digest before trusting it:

```sh
podman pull ghcr.io/claravnk/chaudron:latest
DIGEST=$(podman image inspect ghcr.io/claravnk/chaudron:latest --format '{{ .Digest }}')

cosign verify "ghcr.io/claravnk/chaudron@${DIGEST}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity \
    'https://github.com/ClaraVnk/chaudron/.github/workflows/publish.yml@refs/heads/main' \
  | jq '.[].optional'
```

The `--certificate-identity` argument is the control. Dropping it, or replacing
it with `--certificate-identity-regexp '.*'`, verifies only that *somebody*
signed the image — which is worth nothing, since anybody can sign anything.

**What is not closed: the host does not check the signature by itself.**

This is a limitation, not an oversight, and it should not be papered over.
Podman can enforce sigstore signatures through `/etc/containers/policy.json`
(`"type": "sigstoreSigned"`), and for a plain key pair (`keyPath`) it works
well. For a *keyless* GitHub Actions identity it does not: the `fulcio` stanza
matches the signer by `subjectEmail`, and a GitHub Actions certificate carries
no email — its identity is a URI SAN
(`https://github.com/…/publish.yml@refs/heads/main`). There is no field in
`containers-policy.json(5)` that matches a URI SAN, so the policy cannot express
"signed by this workflow". Checked against `podman 5.6.0` on Rocky 10.

So `podman auto-update` pulls an unverified digest, exactly as it did before.
The signature is evidence available *after* the fact, not a gate. Pick one:

1. **Verify by hand after each deploy** (the command above), and treat a
   verification failure as an incident. Cheap, honest, and manual — which means
   it will be skipped on the day it matters.
2. **Gate the update.** Stop the timer and replace it with a script that pulls,
   runs `cosign verify` against the pinned identity, and only then runs
   `podman auto-update`. Verification becomes a real gate, and the 15-minute
   unattended pipeline becomes a 15-minute *verified* pipeline.
3. **Sign with a key pair instead of keyless**, store the private key in a
   GitHub secret, and put the public key in `policy.json` with
   `"type": "sigstoreSigned", "keyPath": …`. Podman then refuses an unsigned or
   wrongly-signed image at pull time, with no host-side scripting. The cost is a
   long-lived private key — the thing keyless exists to avoid — and a rotation
   procedure nobody has written.
4. **Stop following a mutable tag.** Point the quadlet at
   `ghcr.io/claravnk/chaudron@sha256:…`, drop `AutoUpdate=registry`, and make
   deployment a deliberate act. This is the only option that removes the
   unattended path entirely.

Until one of 2–4 is in place, treat `:latest` as trusted-by-convention and note
that the only automatic gate is the health check, which catches "does not
start", never "is not ours".

### Rolling back

Auto-update rolls back on its own **only** when the new container fails its
health check. For a change that starts cleanly and is simply wrong, roll back by
republishing a known-good commit as `:latest`:

```sh
gh workflow run publish.yml -f ref=<good-sha>
```

`ref` must be a commit SHA (7–40 hex characters) that is **already reachable
from `main`**; the workflow verifies this and refuses anything else, tags
included. A tag can be moved, so it is not an acceptable description of "the
code I reviewed".

Then either wait for the timer or run `podman auto-update`. To stop the bleeding
first, pin the service to the previous image and disable the timer — note the
immutable tag is the **first 12 characters** of the commit SHA, which is what
`publish.yml` tags and pushes:

```sh
systemctl --user stop podman-auto-update.timer
podman tag ghcr.io/claravnk/chaudron:<good-sha-12> ghcr.io/claravnk/chaudron:latest
systemctl --user restart chaudron.service
```

### Migrations are deliberately outside this loop

`chaudron-migrate.container` has no `AutoUpdate=` and no `[Install]` section: it
never runs on its own. That is the point.

If migrations ran in the API entrypoint, this pipeline would apply schema changes
to production with nobody watching — and Podman's rollback would become actively
misleading, because it restores the previous *image* and cannot restore the
previous *schema*. Old code against a new database is usually worse than the
failure it was undoing.

For a release carrying a migration:

```sh
systemctl --user start chaudron-migrate.service
journalctl --user -u chaudron-migrate.service -n 50     # read it before continuing
podman auto-update                                     # then let the API roll
```

Because both versions coexist for a few minutes, **every migration must be
backward compatible with the code currently running**. Expand, migrate, contract:
add columns in one release, remove them in a later one, never both at once.

### What this cadence costs

- 96 registry polls per day per image. GHCR absorbs that without complaint.
- **No human between a merged pull request and production.** The health check is
  the only automatic gate, and it catches "does not start", not "is wrong". If
  that trade stops being acceptable — a second maintainer, real users — put a
  GitHub Environment with a required reviewer in front of `publish.yml` rather
  than lengthening the timer.

---

## 6. Row-level security

Since migration `0004`, PostgreSQL itself refuses to return one household's rows
to another. Thirteen tables carry a policy; a connection that has not named a
household reads **nothing** from any of them. This closes the engine half of
SEC-001 / AUD-002 — until then, isolation rested entirely on every query
remembering its `WHERE household_id`, which is a property that degrades in
silence.

**The whole guarantee reduces to one sentence: the API must not connect as the
role that owns the tables.** PostgreSQL exempts a table's owner from its own
policies unless `FORCE ROW LEVEL SECURITY` is set, and this schema deliberately
does not set it — the owner is Alembic, `scripts/seed.py` and your psql prompt,
and forcing policies on them would mean every maintenance statement had to post
a tenant first. So an instance whose `CHAUDRON_DATABASE_URL` still names the
owner passes every health check, answers every endpoint, and enforces nothing.

### 6.1 Enabling it on an instance that is already deployed

Four steps, in this order. Steps 1 and 2 change nothing observable; step 3 is
the switch.

```sh
# 1. Apply the migration (as the owner, from the migration runner)
systemctl --user start chaudron-migrate.service
journalctl --user -u chaudron-migrate.service | tail -20

# 2. Create the application role, as the `chaudron` account on the SERVER.
#    The runtime image ships the wheel and the migrations, not scripts/, so this
#    is the psql form; from a checkout or from CI, `uv run python
#    scripts/provision_app_role.py` does exactly the same thing and then checks
#    its own work. Keep the two in step if you change either.
podman exec -i chaudron-db psql -v ON_ERROR_STOP=1 -U chaudron -d chaudron <<'SQL'
CREATE ROLE chaudron_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS LOGIN;
GRANT CONNECT ON DATABASE chaudron TO chaudron_app;
GRANT USAGE ON SCHEMA public TO chaudron_app;
REVOKE CREATE ON SCHEMA public FROM chaudron_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO chaudron_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO chaudron_app;
GRANT EXECUTE ON FUNCTION chaudron_current_household() TO chaudron_app;
ALTER DEFAULT PRIVILEGES FOR ROLE chaudron IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO chaudron_app;
ALTER DEFAULT PRIVILEGES FOR ROLE chaudron IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO chaudron_app;
SQL

# Then the password, on its own, through psql's `\password`: it prompts without
# echo, hashes with SCRAM client-side, and sends the hash. The plaintext never
# reaches argv, the shell history, the wire or the server log.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c '\password chaudron_app'
```

The `ALTER DEFAULT PRIVILEGES` lines are what stop the next migration from
producing a table the application cannot read. They apply to tables created by
the `chaudron` role *after* this statement, and to nothing already there — which
is why they come with the grants rather than instead of them.

```sh
# 3. Point the API — and only the API — at the new role. Two secrets from here
#    on: the migrator keeps the owner DSN, the API gets the app DSN.
read -rs -p 'App DSN: ' V && printf '%s' "$V" | podman secret create chaudron-database-url-app - && unset V
```

Then, in `~/.config/containers/systemd/`:

* `chaudron.container` — replace
  `Secret=chaudron-database-url,type=env,target=CHAUDRON_DATABASE_URL`
  with `Secret=chaudron-database-url-app,type=env,target=CHAUDRON_DATABASE_URL`
* `chaudron-migrate.container` — leave it exactly as it is. It must stay the
  owner: a role that cannot create tables cannot run a migration.

```sh
# 4. Reload and restart the API, then verify (see 6.2)
systemctl --user daemon-reload && systemctl --user restart chaudron.service
```

Podman secrets keep whatever trailing newline the shell gave them, so use
`printf '%s'` and never `echo`.

### 6.2 Verifying that it is actually on

Three questions, three answers. All of them run against the production database;
none of them writes.

```sh
# Which tables are protected, and by what? Expect 13 tables and 16 policies.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "select tablename, policyname, cmd from pg_policies where schemaname='public' order by 1,2;"

# Any table carrying household_id that the migration missed? Expect zero rows.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
   join pg_attribute a on a.attrelid=c.oid
   where n.nspname='public' and c.relkind='r' and a.attname='household_id'
     and not a.attisdropped and not c.relrowsecurity;"

# Is the application's own role exempt? Expect f | f, and no tables owned.
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "select rolsuper, rolbypassrls from pg_roles where rolname='chaudron_app';" -c \
  "select relname from pg_class where relowner='chaudron_app'::regrole;"
```

`python scripts/provision_app_role.py --check` answers all three at once and
exits non-zero when any of them is wrong — that is the form to put in a
deployment pipeline, after every migration.

The end-to-end check, which no catalogue query can replace: connect **as the
application role** and read a tenant table without naming a household.

```sh
psql "$APP_DSN" -c "select count(*) from inventory_lot;"   # expect 0
psql "$APP_DSN" -c "set local chaudron.household_id = '<a real household uuid>';
                    select count(*) from inventory_lot;"   # expect that household's rows
```

### 6.3 What breaks when the role is wrong, and how it looks

| Symptom | Cause |
| --- | --- |
| Every endpoint returns empty lists; no errors anywhere | The API connects as a non-owner but nothing posts the household. Check that `api.deps` resolves the tenant and that the session is the one from `infra/db.py`. |
| Writes fail with `new row violates row-level security policy` | Same cause, seen from the write side. RLS filters reads silently and refuses writes loudly, so this is the *useful* symptom of the two. |
| Everything works and `pg_policies` is full — but two households see each other | The API is still connecting as the owner, or as a superuser. Run `provision_app_role.py --check`. This is the failure mode with no symptom of its own. |
| `permission denied for table …` after a new migration | A table created by a later revision that the app role was never granted. `ALTER DEFAULT PRIVILEGES` covers tables created *after* provisioning; re-run the script once. |
| `alembic upgrade` fails with `must be owner of table` | The migrator is using the app DSN. It needs the owner. |
| `invalid input syntax for type uuid: ""` | Something is posting an empty household. `chaudron_current_household()` treats `''` as "no tenant" precisely to prevent this; a caller writing raw SQL against `current_setting` will not. |

### 6.4 Rolling back

The migration is reversible and the switch is not one-way:

```sh
# Put the API back on the owner DSN (policies stay, but stop applying)
systemctl --user edit --full chaudron.service   # or restore the previous Secret= line
systemctl --user daemon-reload && systemctl --user restart chaudron.service

# Or remove the policies entirely
podman exec chaudron-migrate alembic downgrade 0003
```

`downgrade 0003` drops the sixteen policies, disables row-level security on the
thirteen tables and drops `chaudron_current_household()`. It does **not** drop
the `chaudron_app` role — roles are cluster-wide and may be shared. Remove it by
hand if you mean to:

```sh
podman exec -it chaudron-db psql -U chaudron -d chaudron -c \
  "reassign owned by chaudron_app to chaudron; drop owned by chaudron_app; drop role chaudron_app;"
```

### 6.5 What is deliberately not protected

* `household`, `user_account`, `unit`, `llm_provider` carry no `household_id`
  and get no policy. The first two are read *before* a household is known — the
  tenant resolution itself queries `household` — and the last two are shared
  reference data. Guarding them would mean guarding the lookup that decides what
  to guard.
* The **public product catalogue** (`product.household_id IS NULL`) is readable
  and writable by any household that has posted one: it is a shared cache of
  Open Food Facts answers (ADR-0008), and scoping it per tenant would multiply
  the outbound calls by the number of households for byte-identical content. It
  is still invisible to a connection with no tenant, and no household can delete
  from it.
* **Background jobs** run outside any HTTP request and therefore outside the
  tenant resolution. They must open their session with
  `Database.session(household_id=...)`, taking the household from the row being
  processed. `docs/data-model.md` section 5.4 expects them to be the first thing
  to leak; under RLS they now read nothing instead, which is a much better first
  symptom.
