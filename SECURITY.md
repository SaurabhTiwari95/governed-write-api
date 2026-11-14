# Security policy

This is a personal demonstration project. Please report a security problem privately:
**Security** tab → **Report a vulnerability**. Please don't open a public issue.
I aim to reply within 7 days.

## What's in place

- HMAC-SHA256 request signing, five-minute freshness window, single-use nonces, IP allow-list, rate limits
- Two-step writes: validate (read-only) → single-use, expiring token → commit, which re-checks every rule under row locks
- Prices and totals computed by the server, never taken from the caller
- Idempotency keys, so retries never write twice; daily caps per caller; an allowed-hours window
- A circuit breaker shared across instances, that stops writes after repeated failures
- Database role with no DELETE, and UPDATE on one column only (`invoices.status`); statement timeouts
- Append-only audit log (database trigger); tokens stored only as SHA-256 hashes
- Pinned dependencies, pip-audit, Dependabot, CodeQL, gitleaks, a non-root container with a read-only filesystem

## Not in scope for this demo

- TLS termination: put a reverse proxy or load balancer with TLS in front.
- Secret storage: keep `CLIENT_KEYS` in a secrets manager in production.
- Human approval of each write: see the companion project **agent-approval-flow**.
