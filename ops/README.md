# Pantry — Operations

Build, run and deploy Pantry with **Podman**. There is no Docker anywhere in
this project: the container engine is Podman, images are built from
`Containerfile`s, and services run as rootless systemd **quadlets**.

Target platform: **Rocky Linux 10, SELinux Enforcing**, rootless Podman under a
dedicated unprivileged user.

---

## Contents

| File | Purpose |
| --- | --- |
| `pantry.container` | Quadlet unit for the API |
| `pantry-db.container` | Quadlet unit for PostgreSQL 16 |

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
podman network create pantry-net
```

### 1.3 Create the data directories

```sh
mkdir -p ~/pantry/data/postgres ~/pantry/data/uploads
```

### 1.4 Start PostgreSQL 16

The password is passed through a Podman secret, never on the command line
(arguments are visible in `ps` and land in shell history):

```sh
read -rs -p 'Postgres password: ' PW && printf '%s' "$PW" | podman secret create pantry-db-password - && unset PW
```

```sh
podman run -d --name pantry-db \
  --network pantry-net \
  --secret pantry-db-password,type=env,target=POSTGRES_PASSWORD \
  -e POSTGRES_DB=pantry \
  -e POSTGRES_USER=pantry \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  -v ~/pantry/data/postgres:/var/lib/postgresql/data:Z \
  --health-cmd 'pg_isready -U pantry -d pantry' \
  docker.io/library/postgres:16
```

> **`:Z` is not optional.** Under SELinux Enforcing, a bind mount without a
> container label is denied. `:Z` applies a **private** label (only this
> container may read the directory); `:z` applies a **shared** label. Use `:Z`
> unless two containers genuinely need the same directory.

Wait until it is healthy:

```sh
podman healthcheck run pantry-db && podman inspect -f '{{ .State.Health.Status }}' pantry-db
```

### 1.5 Build the API image

`HEALTHCHECK` is a Docker-format instruction. Podman's default OCI output
format drops it with a warning, so pass `--format docker` when you want the
baked-in healthcheck in the image. (Quadlet deployments declare `HealthCmd=`
themselves and do not depend on it.)

```sh
podman build --format docker -t localhost/pantry-api:dev -f backend/Containerfile backend
```

### 1.6 Run the API

```sh
podman run -d --name pantry \
  --network pantry-net \
  -p 127.0.0.1:8000:8000 \
  --env-file ./.env \
  -v ~/pantry/data/uploads:/var/lib/pantry/uploads:Z \
  localhost/pantry-api:dev
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
podman rm -f pantry pantry-db
podman network rm pantry-net
```

---

## 2. Deployment — rootless systemd quadlets

Quadlets are declarative container units that systemd generates services from.
They run under an unprivileged user account with lingering enabled, so services
survive logout and start at boot.

### 2.1 Prepare the service account

```sh
sudo useradd --create-home --shell /usr/sbin/nologin pantry
sudo loginctl enable-linger pantry
```

Everything below runs **as that user**:

```sh
sudo -u pantry XDG_RUNTIME_DIR=/run/user/$(id -u pantry) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u pantry)/bus \
  systemctl --user <command>
```

Without `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`, `systemctl --user`
fails with *"Failed to connect to bus"*.

### 2.2 Install the units

```sh
install -d -m 0755 ~pantry/.config/containers/systemd
install -m 0644 ops/pantry.container ops/pantry-db.container \
  ~pantry/.config/containers/systemd/
```

Create the runtime state the units expect:

```sh
podman network create pantry-net
mkdir -p ~/pantry/data/postgres ~/pantry/data/uploads
```

### 2.3 Create the secrets

Each command is a single line with masked input, transmitted over stdin, and
run **on the server as the `pantry` user**. `printf '%s'` matters: `echo` and
`podman secret create <file>` both keep a trailing newline, which is invisible
until the value crosses an HTTP header or an HTML form and silently fails.

```sh
read -rs -p 'DB password: ' V && printf '%s' "$V" | podman secret create pantry-db-password - && unset V
```

```sh
read -rs -p 'App secret key: ' V && printf '%s' "$V" | podman secret create pantry-secret-key - && unset V
```

```sh
read -rs -p 'Anthropic API key: ' V && printf '%s' "$V" | podman secret create pantry-anthropic-api-key - && unset V
```

```sh
read -rs -p 'Inbound email webhook key: ' V && printf '%s' "$V" | podman secret create pantry-inbound-email-key - && unset V
```

### 2.4 Non-secret configuration

Copy `.env.example` to `~pantry/pantry.env`, fill in the non-secret keys, and
restrict its permissions. `EnvironmentFile=` in `pantry.container` points at it.

```sh
install -m 0600 /dev/null ~pantry/pantry.env
```

### 2.5 Start

```sh
systemctl --user daemon-reload
systemctl --user start pantry-db.service
systemctl --user start pantry.service
systemctl --user status pantry.service
journalctl --user -u pantry.service -f
```

Quadlet units are **not** enabled with `systemctl enable`. The generator reads
`[Install] WantedBy=default.target` from the `.container` file, so a
`daemon-reload` is all that is required.

### 2.6 Update to a new image

```sh
podman pull <registry>/pantry-api:<tag>
podman tag <registry>/pantry-api:<tag> localhost/pantry-api:latest
systemctl --user restart pantry.service
```

Rollback is the same sequence with the previous tag. Always keep the previous
image on the host until the new one has passed `/readyz`.

---

## 3. SELinux checklist

Do **not** run `setenforce 0`. Every problem below has a targeted fix.

| Symptom | Check | Fix |
| --- | --- | --- |
| Container cannot read/write a bind mount | `ls -Z ~/pantry/data/postgres` | Add `:Z` to the `Volume=` line, or `restorecon -Rv <path>` |
| Denials with no obvious cause | `sudo ausearch -m AVC -ts recent` | Feed the output to `audit2why` before changing anything |
| Service binds a non-standard port | `sudo semanage port -l \| grep <port>` | `sudo semanage port -a -t http_port_t -p tcp <port>` |
| Reverse proxy cannot reach the API | `getsebool httpd_can_network_connect` | `sudo setsebool -P httpd_can_network_connect on` |

Useful one-liners:

```sh
getenforce
ls -Z ~/pantry/data
ps -eZ | grep pantry
sudo ausearch -m AVC -ts recent | audit2why
```

### The `:U` trap

Never add `:U` to a volume in a quadlet. It chowns the directory to the
container's **declared** user at start time, not the runtime one — the first
start works and every subsequent start breaks with permission errors. Set
ownership on the host once instead:

```sh
podman unshare chown -R 10001:10001 ~/pantry/data/uploads
```

---

## 4. Backups

PostgreSQL is backed up with `pg_dump`, not by copying the data directory.

```sh
podman exec pantry-db pg_dump -U pantry -d pantry --format=custom > pantry-$(date -I).dump
```

Restore into an empty database:

```sh
podman exec -i pantry-db pg_restore -U pantry -d pantry --clean --if-exists < pantry-YYYY-MM-DD.dump
```

Verify a restore against a scratch database before any migration that drops or
rewrites data.

---

## 5. Continuous deployment

Production follows `ghcr.io/claravnk/pantry:latest`. A merge to `main` reaches
the server within 15 minutes, unattended.

### How the chain fits together

```
merge to main → CI passes → publish.yml pushes :latest to GHCR
                                        ↓  (≤ 15 min)
              podman-auto-update.timer sees a new digest
                                        ↓
              pantry.service restarts on the new image
                                        ↓
              health check fails? → Podman rolls back automatically
```

Two units make this work, and both must be installed:

```sh
# 1. The quadlet already declares AutoUpdate=registry (see pantry.container).

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

### Rolling back

Auto-update rolls back on its own **only** when the new container fails its
health check. For a change that starts cleanly and is simply wrong, roll back by
republishing a known-good commit as `:latest`:

```sh
gh workflow run publish.yml -f ref=<good-sha>
```

Then either wait for the timer or run `podman auto-update`. To stop the bleeding
first, pin the service to the previous image and disable the timer:

```sh
systemctl --user stop podman-auto-update.timer
podman tag ghcr.io/claravnk/pantry:<good-sha> ghcr.io/claravnk/pantry:latest
systemctl --user restart pantry.service
```

### Migrations are deliberately outside this loop

`pantry-migrate.container` has no `AutoUpdate=` and no `[Install]` section: it
never runs on its own. That is the point.

If migrations ran in the API entrypoint, this pipeline would apply schema changes
to production with nobody watching — and Podman's rollback would become actively
misleading, because it restores the previous *image* and cannot restore the
previous *schema*. Old code against a new database is usually worse than the
failure it was undoing.

For a release carrying a migration:

```sh
systemctl --user start pantry-migrate.service
journalctl --user -u pantry-migrate.service -n 50     # read it before continuing
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
