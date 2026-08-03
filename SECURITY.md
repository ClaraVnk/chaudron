# Security Policy

Pantry stores third-party API keys, personal purchase history and photographs of
till receipts. A vulnerability here has a real cost for the people running an
instance, so reports are welcome and taken seriously — including reports about
design documents, before a single line of the affected code exists.

## Project status

Pantry is in the **scoping** phase. The repository holds architecture documents,
architecture decision records (ADRs), a project skeleton and a CI pipeline. There
is no feature code, no release, and no deployed instance to attack.

Consequently:

- **There is no supported version.** No version has been published, so no
  version receives security fixes. The table below will be filled in when the
  first release is tagged.
- Reports against `main` are in scope.
- **Reports against the design are explicitly in scope and especially useful
  right now.** If an ADR describes a control that does not hold, or a data flow
  that leaks, say so — fixing a document costs minutes, fixing a shipped schema
  costs a migration.

| Version | Supported |
| --- | --- |
| `main` (scoping, unreleased) | Best effort, no guarantee |

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use one of these two
channels:

1. **GitHub Private Vulnerability Reporting — preferred.** Go to the
   [Security tab](https://github.com/ClaraVnk/pantry/security/advisories/new) of
   this repository and open a private advisory. The report stays private, the
   discussion happens in one place, and a CVE can be requested from the same
   thread if one turns out to be warranted.
2. **Email:** kevin@stackops.ch. Use this if you cannot use GitHub, or if you
   consider the existence of the report itself sensitive. PGP is not currently
   published; if you need an encrypted channel, ask in a first contentless email
   and one will be arranged.

### Never include a real secret in a report

Not in the advisory, not in an email, not in an attached log, not in a
screenshot. This includes provider API keys (`sk-ant-…`, Gemini keys), the
instance `PANTRY_SECRET_KEY`, database passwords, JWTs cut from a live session,
and the inbound-email webhook key.

If a proof of concept needs a credential, **generate a throwaway one** and say so.
Redact captured values down to a prefix and a length (`sk-ant-…, 108 chars`).

If a real secret does end up in a report, say so immediately in the same thread:
it then has to be rotated, and a deleted message does not rotate anything.

### What to include

- What an attacker gains, and what they need to start (a household account? an
  instance operator account? nothing?).
- Reproduction steps or a proof of concept, against your own instance.
- The commit SHA, and the relevant configuration (provider mode, deployment
  shape) with all secret values removed.
- Any disclosure deadline you intend to hold to.

## Response targets

Pantry is maintained by one person, unpaid. The targets below are commitments of
effort, not of a service level:

| Step | Target |
| --- | --- |
| Acknowledgement of receipt | 5 business days |
| Initial assessment (valid / not valid, severity) | 15 calendar days |
| Fix or documented mitigation for a confirmed high-severity issue | 90 calendar days |

If you receive no acknowledgement within 10 business days, assume the message
was lost and send a reminder through the other channel.

Coordinated disclosure is expected: please give the maintainer a chance to ship a
fix before publishing. Credit is given in the advisory unless you ask otherwise.

## Scope

These are the surfaces where a defect actually costs a user something. They are
listed as designed — see the ADRs for the intended controls, and report anything
that shows a control missing, weaker than described, or bypassable.

### 1. Household-supplied provider API keys, stored encrypted

Each household configures its own model access, in `byok` mode with its own
provider key ([ADR 0007](docs/adr/0007-byok-and-local-inference.md)). These keys
are billable secrets belonging to third parties. Anything that exposes one is
high severity: encryption-at-rest bypass, a read endpoint returning more than
the provider name, timestamp and last four characters, a key surfacing in
structured logs, in an exception traceback returned to the client, in an error
message propagated from a provider SDK, or in a backup or database dump.

### 2. Operator-supplied Ollama URL called by the server (SSRF)

In `ollama` mode the household supplies a base URL that **the backend then
requests**. That is an SSRF primitive by construction, and the usual defence
(reject private ranges) does not apply, because a co-located Ollama legitimately
lives on a private address. The designed control is an explicit host allowlist
set by instance environment variable, plus `http`/`https` only, DNS resolved both
at validation time and immediately before the call (against rebinding),
redirects disabled, and bounded timeout and response size. Report any way to
reach a host outside the allowlist, to defeat the rebinding check, to follow a
redirect, or to use the fetch as a port scanner or metadata-endpoint reader.

### 3. Isolation between households (multi-tenancy)

Every business table carries a `household_id`, and isolation is enforced by
application convention rather than by the database
([ADR 0006](docs/adr/0006-multi-tenant-from-day-one.md)) — which is exactly the
weakness that makes this the highest-value place to look. Any read or write
reaching another household's stock, receipts, shopping list or provider
configuration is in scope, as is any path where the tenant is derived from
client-controlled input (a header, a subdomain, a body field, a path parameter)
rather than from the authenticated session. So is any lock-down of the
`instance_owner` provider mode that a non-owner household can talk its way past.

### 4. Inbound-email webhook

Forwarded order confirmations arrive through a webhook authenticated by a shared
secret (`PANTRY_INBOUND_EMAIL_WEBHOOK_KEY`). In scope: signature verification
that can be skipped, replayed or defeated by a non-constant-time comparison;
attributing a forwarded email to a household that did not send it; attachment
handling that ignores the size limit, escapes its storage directory, or parses
hostile MIME into code execution.

### 5. Receipt images and personal data

Receipt photographs are personal data — they record what a household bought,
when, where and for how much, and frequently carry a loyalty-card number. In
scope: unauthenticated or cross-household access to stored images, guessable or
enumerable object paths, images served without the correct content type or
disposition, EXIF geolocation retained and re-served, images left behind after a
delete, and images or their extracted contents leaking into logs or into a model
request the household did not consent to.

### 6. Everything else in the repository

Also in scope: authentication and session handling (JWT signing, token lifetime,
`PANTRY_SECRET_KEY` handling), CORS configuration, dependency vulnerabilities,
the container image and its quadlet units (privileges, secret handling, SELinux
labelling), and the CI workflows (anything that lets a fork's pull request read a
repository secret).

## Out of scope

- Findings from automated scanners with no demonstrated impact on Pantry.
- Missing hardening headers or TLS configuration on an instance, when the
  reverse proxy is the operator's responsibility.
- Vulnerabilities in Open Food Facts, a model provider, Ollama, or an inbound
  email service — report those to their maintainers. The way Pantry *calls*
  them is in scope.
- Attacks requiring an already-compromised host or an already-privileged
  instance operator.
- Social engineering of the maintainer.

## Contributor obligations

- Never commit a real secret. `.env` is gitignored; `.env.example` carries the
  keys with no values.
- The CI runs `gitleaks` over the full history and `pip-audit` over the locked
  dependencies. Do not work around either.
- If you notice a leaked secret in the history, report it privately rather than
  opening an issue. A `git reset` does not fix a pushed secret — it has to be
  rotated.
