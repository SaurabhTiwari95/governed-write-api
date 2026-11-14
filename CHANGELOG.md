# Changelog

## 0.1.0 (2026-11-09)

- Two-step write API for AI agents: validate (preview + single-use token) → commit.
- Actions: `create_invoice` (server-side pricing, line and amount limits), `record_payment` (no overpayment, settles invoices).
- Idempotency keys with stored responses; per-caller daily cap; allowed-hours window; shared circuit breaker.
- Re-check under row locks at commit; `state_changed` when data moved since validation.
- PostgreSQL role without DELETE; append-only audit log; housekeeping job.
- Docker image, docker compose stack, Python client, example agent, load test, CI with integration tests.
