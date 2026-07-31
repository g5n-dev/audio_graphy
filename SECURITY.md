# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report it through GitHub's private vulnerability reporting: go to the
[Security tab](https://github.com/g5n-dev/audio_graphy/security/advisories/new)
and open a draft advisory. That channel is private to the maintainers until an
advisory is published.

Useful things to include, as far as you have them: affected version or commit,
configuration involved (adapter modes, enabled feature flags, deployment
profile), reproduction steps, and what an attacker gains. A proof of concept
helps but is not required — a clear description of the flaw is enough to start.

Please do not test against a deployment you do not operate.

## What is in scope

AudioGraphy processes recorded conversations, so the parts worth the most
scrutiny are:

- **Authentication and tenancy** — JWT issuing and verification, the public-path
  allow-list in the auth middleware, and anything that lets a request read or
  write data belonging to another `tenant_id`. Tenant scoping is currently
  enforced per query rather than by a session-level guard, so a missing
  predicate is a realistic class of bug here.
- **Audio at rest** — envelope encryption of stored audio, master-key handling,
  and the signed playback grants that serve audio without a bearer token.
- **Privacy operations** — DSAR export and erasure, retention sweeps, and PII
  scrubbing. A deletion that leaves recoverable residue counts.
- **Injection and traversal** — anything reaching SQL, a filesystem path, or an
  outbound URL from request data.

## What is not

- Findings that require an already-compromised host or database.
- Denial of service through sheer volume against a self-hosted deployment.
- Missing hardening on the default development configuration where the
  documentation already states it is not production-ready — for example
  `docker compose --profile mock`, which runs with mock model adapters, a
  development `JWT_SECRET`, and `uvicorn --reload`. If you find a case where a
  **production** configuration inherits a development default, that *is* in
  scope and we want to hear about it.
- The `/metrics` endpoint being unauthenticated. This is deliberate so
  Prometheus can scrape it; operators are expected to keep it off any public
  listener. Report it if you find it exposing tenant-identifying data.

## Supported versions

The project has not cut a stable release yet. Fixes land on the default branch;
there are no maintained release branches to backport to.
