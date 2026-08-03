<div align="center">

<img src="assets/wordmark.png" alt="Chaudron" width="420">

**Throw in what you have. See what comes out.**

Self-hostable food stock management with AI recipe suggestions and receipt
scanning — running on *your* model, *your* key, *your* server.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![CI](https://github.com/ClaraVnk/chaudron/actions/workflows/ci.yml/badge.svg)](https://github.com/ClaraVnk/chaudron/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2A6DB2.svg)](https://mypy-lang.org/)
[![Podman](https://img.shields.io/badge/containers-Podman-892CA0.svg?logo=podman&logoColor=white)](https://podman.io/)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

</div>

---

> [!WARNING]
> **Chaudron is in the scoping phase.** This repository currently contains the
> architecture, the decision records and the project baseline — **not a working
> application**. There is nothing to install yet. Issues and feedback on the
> design are the most valuable contribution right now.
>
> Start with [`docs/architecture.md`](docs/architecture.md) and
> [`docs/adr/`](docs/adr/).

---

## Why

Most food inventory apps want your data, your subscription, or both. The ones
that generate recipes send your grocery habits to a service you don't control,
and stop working when the company pivots.

Chaudron takes the opposite position: **you host it, and you bring your own
model.** There is no Chaudron cloud, no Chaudron account, no Chaudron API key. The
application never pays for anyone's inference and never sees anyone's data.

## Features

| | |
|---|---|
| 📦 **Stock tracking** | What you own, where it's stored, and when it expires — per household, not per person. |
| 📷 **Barcode scanning** | Point your phone's camera at a barcode. Products resolve through [Open Food Facts](https://world.openfoodfacts.org/). |
| 🧾 **Receipt import** | Photograph a till receipt; a vision model extracts the lines. **Nothing is written without your review.** |
| 🍳 **Recipe suggestions** | Generated from the stock actually on hand, not from a generic database. |
| 🛒 **Shopping list** | Built from what ran out, exportable to the tools you already use. |
| 🔑 **Bring your own model** | Anthropic, OpenAI, Gemini, Mistral, or a local Ollama. Your key, your bill, your choice. |
| 🏠 **Self-hosted** | Podman + systemd quadlets. Runs on a small VPS or a home server. |

## Bring your own model

Each household configures its own model access. There is deliberately **no mode
in which the application funds inference for its users** — that decision removes
spend caps, quotas, abuse protection and a large amount of GDPR exposure in one
stroke.

| Mode | What you provide | Who pays |
|---|---|---|
| `byok` | Your own API key — Anthropic, OpenAI, Gemini or Mistral | You, directly to the provider |
| `ollama` | A base URL and a model name | Nobody — local inference |
| `instance_owner` | Nothing — uses the server's configured key | The operator, for their own household only |

Supported providers:

| Provider | Models | Vision | Notes |
|---|---|---|---|
| **Anthropic** | Claude | ✅ | Default in documentation and examples |
| **OpenAI** | GPT | ✅ | The API behind ChatGPT — a ChatGPT Plus subscription is *not* an API key |
| **Google** | Gemini | ✅ | |
| **Mistral AI** | Mistral, Pixtral | ✅ | **EU-hosted** — your grocery data never leaves European jurisdiction |
| **Ollama** | Whatever you load | ⚠️ depends | Fully local, zero outbound calls. Capabilities detected at configuration time |

> [!TIP]
> If keeping data under EU jurisdiction matters to you, **Mistral** (EU-hosted)
> or **Ollama** (nothing leaves your machine) are the two options that give you
> that without compromise.

### Honest about degradation

Providers are not equivalent. Reading a creased, faded thermal receipt is hard,
and a small local model will do it worse than a frontier one.

Chaudron does not paper over this. Providers **declare their capabilities**, and
the interface tells you what you're getting:

- Missing a capability that can be approximated → the feature works, with a
  documented quality drop.
- Missing a capability that changes the experience → **degraded mode**, shown as
  a persistent indicator explaining exactly what is reduced.
- Missing a capability the feature depends on → the feature is **disabled with
  the reason displayed**, not left to fail at runtime.

You will never discover a limitation at the moment it breaks.

## Architecture

```mermaid
flowchart TB
    subgraph client["📱 Client"]
        PWA["PWA — React + Vite<br/>camera, barcode decoding,<br/>receipt review"]
    end

    subgraph server["🖥️ Server"]
        API["FastAPI<br/>api → services → domain ← infra"]
        DB[("PostgreSQL 16<br/>household-scoped")]
        API --- DB
    end

    subgraph ext["🌐 External — all optional"]
        OFF["Open Food Facts<br/>EAN → product"]
        LLM["Model provider<br/>Anthropic · OpenAI · Gemini<br/>Mistral · Ollama<br/><i>configured per household</i>"]
        MAIL["Inbound email<br/>forwarded orders"]
    end

    PWA -->|HTTPS| API
    API --> OFF
    API --> LLM
    MAIL -.->|signed webhook| API
```

Dependencies only point inward: `api → services → domain ← infra`. The domain
layer knows nothing about SQLAlchemy, HTTP, or any model SDK — it declares
interfaces, and infrastructure implements them. That is what makes three model
providers possible without the recipe logic knowing any of them exist.

Every business table carries a `household_id`, from the very first commit. See
[ADR 0006](docs/adr/0006-multi-tenant-from-day-one.md).

## Screenshots

> [!NOTE]
> Not yet. There is no interface to photograph — see the status warning above.
> This section will be filled with real captures of a running instance, never
> with mockups presented as screenshots.

## Quick start

> [!IMPORTANT]
> Not usable yet. These are the intended commands, kept here so the shape of the
> project is clear. They will work when the first release ships.

Requires [uv](https://docs.astral.sh/uv/), Podman, and Node.js 22+.

```sh
git clone https://github.com/ClaraVnk/chaudron.git && cd chaudron
cp .env.example .env          # the app refuses to start if this is incomplete

# Database
podman run -d --name chaudron-db \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" \
  -v chaudron-db-data:/var/lib/postgresql/data:Z \
  -p 127.0.0.1:5432:5432 docker.io/library/postgres:16   # loopback only, never 0.0.0.0

# Backend
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn chaudron.api.main:app --reload

# Frontend
cd frontend && npm install && npm run dev
```

Liveness and readiness are separate endpoints on purpose: `/healthz` says the
process is alive, `/readyz` says it can actually serve traffic.

## Development

```sh
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest --cov
```

Tests run against a **real PostgreSQL instance** via testcontainers. SQLite is
not used anywhere, including in tests — the reasoning is in
[ADR 0003](docs/adr/0003-backend-stack.md).

Containers are built with **Podman**, never Docker. See
[`ops/README.md`](ops/README.md) for the quadlet units and the SELinux labelling
that bind mounts require.

## Documentation

| Document | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | System shape, layers, data flows, the Ollama topology problem |
| [Data model](docs/data-model.md) | Entities, tenancy, units, expiry batches |
| [Scanning notes](docs/technical-notes-scanning.md) | Barcode reading in-browser, camera in a PWA, Open Food Facts |
| [Ingestion notes](docs/technical-notes-ingestion.md) | Inbound email, receipt OCR, shopping list export |
| [Decision records](docs/adr/) | Why things are the way they are — including what it costs |

## Contributing

Feedback on the architecture is worth more than code right now. Read
[CONTRIBUTING.md](CONTRIBUTING.md), and note the house rules: Conventional
Commits, PostgreSQL only, Podman only, everything versioned in English, and no
secrets ever.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Security issues go through [SECURITY.md](SECURITY.md) — not public issues.

## License

[GNU AGPL v3.0 or later](LICENSE).

Copyleft that covers network use: if you run a modified Chaudron as a service, you
owe your users the source. That is deliberate — this project exists so people
can own their food data, and a closed fork serving it back to them would defeat
the point.
