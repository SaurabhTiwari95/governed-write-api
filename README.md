# governed-write-api

[![CI](https://github.com/SaurabhTiwari95/governed-write-api/actions/workflows/ci.yml/badge.svg)](https://github.com/SaurabhTiwari95/governed-write-api/actions/workflows/ci.yml)
[![CodeQL](https://github.com/SaurabhTiwari95/governed-write-api/actions/workflows/codeql.yml/badge.svg)](https://github.com/SaurabhTiwari95/governed-write-api/actions/workflows/codeql.yml)

A safe write path for AI agents. Agents can create invoices and record payments, but every write is
**validated first, previewed, committed once, capped, time-boxed and audited**. The rules are
checked again at the moment of writing, so nothing slips through between "looks fine" and "saved".

```mermaid
sequenceDiagram
    participant A as AI agent
    participant W as governed-write-api
    participant DB as PostgreSQL
    A->>W: POST /v1/validate/create_invoice (signed)
    W->>DB: read-only: customer? products? prices?
    W-->>A: preview (server-computed totals) + single-use token (10 min)
    Note over A: show a person, or decide
    A->>W: POST /v1/commit {token} + Idempotency-Key (signed)
    W->>W: breaker closed? inside allowed hours?
    W->>DB: BEGIN; lock caller; daily cap; take token; re-check rules with row locks
    W->>DB: preview unchanged? → INSERT …; mark token used; audit; COMMIT
    W-->>A: 201 result (stored under the Idempotency-Key)
    A->>W: same Idempotency-Key again (lost response, retry)
    W-->>A: same 201, Idempotent-Replayed: true. Nothing written twice
```

The sample data is a made-up wholesaler, "Acme Traders". It's the companion to
[secure-read-gateway](https://github.com/SaurabhTiwari95/secure-read-gateway), which handles reads.

## Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Install and run](#install-and-run)
- [Use it](#use-it)
- [Configuration](#configuration)
- [Run the tests](#run-the-tests)
- [Troubleshooting](#troubleshooting)
- [Security model](#security-model)
- [How it scales](#how-it-scales)
- [Performance](#performance)
- [Design decisions](#design-decisions)
- [Project layout](#project-layout)

## Features

| Control | What it stops |
|---|---|
| **Validate → commit**, with a preview and a single-use token that expires in 10 minutes | Blind writes; reusing an old approval |
| **Server-side pricing.** The agent sends product and quantity; the server prices them | An agent inventing prices or totals |
| **Re-check at commit** under row locks; `409 state_changed` if the preview no longer holds | Writing on stale data (for example a payment made in between) |
| **Idempotency keys** with the stored response | Duplicate invoices when an agent retries after a timeout |
| **Daily cap per caller**, exact under concurrency (per-caller lock) | A looping agent writing thousands of records |
| **Allowed-hours window** (time zone aware, can cross midnight) | Writes at 3 a.m. that nobody will see until morning |
| **Circuit breaker** shared by every instance | Hammering a failing database; cascading errors |
| **Append-only audit log** (database trigger) | Quietly rewriting history |
| **Least-privilege role:** no DELETE; UPDATE only on `invoices.status` | Damage even if the code had a bug |
| **Signed requests:** HMAC, timestamp, single-use nonce, IP allow-list, rate limit | Stolen or replayed requests |

## Prerequisites

| Tool | Version | Needed for | Get it |
|---|---|---|---|
| Docker Desktop (or Docker Engine + Compose v2) | 24+ | Running the stack | <https://docs.docker.com/get-docker/> |
| Python | 3.12 | Example, tests, `.env` generator | <https://www.python.org/downloads/> |
| Git | any recent | Cloning | <https://git-scm.com/downloads> |

## Install and run

### Windows (PowerShell)

```powershell
git clone https://github.com/SaurabhTiwari95/governed-write-api.git
cd governed-write-api
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python scripts/make_env.py
docker compose up -d --build --wait api
```

### macOS / Linux

```bash
git clone https://github.com/SaurabhTiwari95/governed-write-api.git
cd governed-write-api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/make_env.py
docker compose up -d --build --wait api
```

What happens:

1. `make_env.py` writes `.env` with fresh random secrets (git-ignored).
2. Compose starts PostgreSQL and Redis.
3. The `migrate` job creates the tables, the least-privilege role and the sample data.
4. The API starts on `http://127.0.0.1:8001`, reachable from this computer only.

It uses ports 8001, 5433 and 6380, so it runs alongside `secure-read-gateway`.

```bash
curl http://127.0.0.1:8001/readyz
```

```json
{"status":"ready","database":true,"store":true,"breaker":"closed"}
```

Stop it with `docker compose down`, or `docker compose down -v` to delete the data as well.

## Use it

### Run the example agent

```bash
python examples/agent_writes.py
```

It validates an invoice, commits it, retries with the same key, gets an overpayment refused, pays
the invoice in full and tries to reuse a token. Abridged output (prices are random sample data):

```text
1. Preview of the invoice (nothing written yet):
{ "customer_id": 1, "issued_on": "2026-11-09", "due_on": "2026-12-09",
  "lines": [ { "product_id": 1, "quantity": 3, "unit_price": "123.40", "line_total": "370.20" }, ... ],
  "total": "1021.45" }

2. Committed:
{ "invoice_id": 6001, "total": "1021.45", "status": "open" }
   retry with the same key replayed: True (same invoice, nothing written twice)

3. Overpayment refused at validation:
[ { "rule": "overpayment", "message": "outstanding is 1021.45" } ]

4. Payment preview:
{ "invoice_id": 6001, "amount": "1021.45", ..., "outstanding_after": "0.00", "settles_invoice": true }
   Payment committed:
{ "payment_id": 3912, "invoice_id": 6001, "invoice_status": "paid" }

5. Reusing a token: 409 token_already_used
```

### From your own code

```python
from client import WriteClient

with WriteClient("http://127.0.0.1:8001", key_id="demo-agent", secret="<AGENT_SECRET from .env>") as api:
    check = api.validate("record_payment", {"invoice_id": 12, "amount": "500.00", "paid_on": "2026-11-09"})
    print(check["preview"])  # decide, or show a person
    done = api.commit(check["token"])  # generates an Idempotency-Key; pass your own to retry safely
```

An async client is included too (`AsyncWriteClient`).

### Actions

| Action | Params | Rules |
|---|---|---|
| `create_invoice` | `customer_id`, `issued_on`, `due_in_days` (0–180), `lines[]` of `{product_id, quantity}` | Customer and products exist; no duplicate products; not in the future or over 90 days old; at most `MAX_INVOICE_LINES` lines; total ≤ `MAX_INVOICE_TOTAL` |
| `record_payment` | `invoice_id`, `amount` (2 decimals), `paid_on` | Invoice exists and is open; amount ≤ outstanding; not in the future or before the invoice date. Settling it marks the invoice `paid` |

`GET /v1/actions` returns each action's full JSON Schema.

### HTTP contract

All `/v1` calls are signed the same way as the read gateway: `X-Key-Id`, `X-Timestamp`, `X-Nonce` and
`X-Signature`. The signature is a hex HMAC-SHA256 of `METHOD\nPATH\nTIMESTAMP\nNONCE\nsha256_hex(body)`.

| Call | Body | Success |
|---|---|---|
| `POST /v1/validate/{action}` | `{"params": {...}}` | `200 {"action", "token", "expires_at", "preview", "request_id"}` |
| `POST /v1/commit` + header `Idempotency-Key` (8–128 of `A-Z a-z 0-9 _ -`) | `{"token": "..."}` | `201 {"action", "result", "request_id"}` |

### Errors

| Status | `error` | Stored under the Idempotency-Key? | What to do |
|---|---|---|---|
| 400 | `idempotency_key_required` | — | Send a valid `Idempotency-Key` |
| 401 | `unauthorized` / `stale_request` / `replayed_request` | — | Fix the signature or clock; never reuse a nonce |
| 403 | `forbidden` | — | Your IP isn't allowed |
| 403 | `outside_allowed_hours` | No | Retry inside the window, same key |
| 404 | `unknown_action` / `unknown_token` | Yes (token) | Validate again |
| 409 | `token_already_used` | Yes | It's done. Use the earlier response |
| 409 | `state_changed` | Yes | Data moved since validation; `details` shows before and now. Validate again |
| 409 | `idempotency_key_in_progress` | No | A request with this key is running; retry shortly |
| 410 | `token_expired` | Yes | Validate again |
| 422 | `invalid_params` / `invalid_request` | Yes (at commit) | Fix the input; `details` says which field |
| 422 | `rule_violation` | Yes (at commit) | `details` lists each broken rule |
| 422 | `idempotency_key_reused` | — | That key was used for a different request; use a new key |
| 429 | `rate_limited` / `daily_cap_reached` | No | Wait; same key works later |
| 503 | `circuit_open` | No | The breaker is open after repeated failures; see `Retry-After` |
| 504 / 500 | `query_timeout` / `internal_error` | No | Safe to retry with the same key |

"Stored" answers are final: a retry with the same key gets the same answer. "Not stored" answers
are temporary: a retry with the same key can still succeed.

## Configuration

| Variable | Purpose | Default | Required |
|---|---|---|---|
| `DATABASE_URL` | PostgreSQL DSN for the `writeapi_app` role | — | Yes |
| `REDIS_URL` | Nonces, rate limits and the breaker, shared by instances. Empty = in-memory: **one instance only** | empty | For more than one instance |
| `CLIENT_KEYS` | JSON `{"key-id": "secret"}`; secrets 32+ characters | `{}` | Yes |
| `ALLOWED_NETWORKS` | JSON list of CIDRs | `["127.0.0.1/32"]` | No |
| `TRUST_PROXY_HEADERS` | Use the right-most `X-Forwarded-For` entry (one trusted proxy only) | `false` | No |
| `VALIDATION_TTL_SECONDS` | Token lifetime | `600` | No |
| `DAILY_COMMIT_CAP` | Commits per caller per day (day in `TIMEZONE`) | `200` | No |
| `ALLOWED_HOURS` | Commit window `HH:MM-HH:MM`; may cross midnight (`22:00-06:00`) | `00:00-24:00` | No |
| `TIMEZONE` | IANA name for hours and "today" (for example `Asia/Kolkata`) | `UTC` | No |
| `MAX_INVOICE_TOTAL` / `MAX_INVOICE_LINES` | Limits on one invoice | `1000000` / `50` | No |
| `BREAKER_FAILURE_THRESHOLD` / `BREAKER_WINDOW_SECONDS` / `BREAKER_COOLDOWN_SECONDS` | Open after N failures in the window; stay open for the cooldown, then allow one trial | `5` / `60` / `300` | No |
| `RATE_LIMIT_PER_MINUTE` | Requests per key per minute | `60` | No |
| `MAX_CLOCK_SKEW_SECONDS` / `MAX_BODY_BYTES` | Freshness window / largest body | `300` / `32768` | No |
| `STATEMENT_TIMEOUT_MS` / `DB_POOL_MIN` / `DB_POOL_MAX` | Database limits | `5000` / `1` / `10` | No |
| `ENABLE_DOCS` / `LOG_LEVEL` | `/docs` on or off / log level | `false` / `INFO` | No |

The migrate job also needs `ADMIN_DATABASE_URL` and `APP_DB_PASSWORD`. Run
`python -m app.migrate --cleanup` daily, as the admin, to delete expired tokens (after 7 days) and
old idempotency keys (after 24 hours). The audit log is never touched.

## Run the tests

Unit, flow and API tests (no Docker needed):

```bash
pytest
```

```text
124 passed, 5 skipped ... TOTAL ... 92%
```

The 5 skipped tests are integration tests against real PostgreSQL and Redis. They cover concurrent
payments on one invoice, role permissions and the shared breaker. To run them:

```bash
docker compose up -d --wait db redis
```

Then set these (PowerShell: `$env:NAME = "value"`; macOS/Linux: `export NAME=value`), using the
passwords from `.env`:

```text
INTEGRATION=1
ADMIN_DATABASE_URL=postgresql://acme_admin:<POSTGRES_PASSWORD>@127.0.0.1:5433/acme
APP_DB_PASSWORD=<APP_DB_PASSWORD>
DATABASE_URL=postgresql://writeapi_app:<APP_DB_PASSWORD>@127.0.0.1:5433/acme
REDIS_URL=redis://127.0.0.1:6380/0
```

```bash
python -m app.migrate --seed
pytest
```

Also:

```bash
ruff check . && ruff format --check .
pip-audit -r requirements-dev.txt
python scripts/prepublish_check.py
```

CI runs all of it on every push, plus CodeQL, gitleaks, a non-root image check and a full
`docker compose` run of the example agent.

## Troubleshooting

| Problem | Fix |
|---|---|
| `set POSTGRES_PASSWORD in .env` | Run `python scripts/make_env.py` first |
| `/readyz` 503, `"database": false` | `docker compose logs migrate`; the migrate job must finish first |
| `403 forbidden` from the example | Check `ALLOWED_NETWORKS` in `.env` includes Docker's private ranges (the generated file does) |
| `403 outside_allowed_hours` | Check `ALLOWED_HOURS` and `TIMEZONE`; validating works at any time, committing doesn't |
| `issued_in_future` / `paid_in_future` for today's date | The server decides "today" in `TIMEZONE` (UTC by default). Send dates in that time zone, or set `TIMEZONE` to yours |
| `409 state_changed` | Someone wrote to the same record after you validated. Validate again and review the new preview |
| `422 idempotency_key_reused` | Each different request needs its own key; reuse a key only to retry the *same* request |
| `503 circuit_open` | Look at the logs for the failures that opened it; it lets one trial through after the cooldown |
| Ports 8001, 5433 or 6380 in use | Change the left-hand port in `docker-compose.yml` |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once |

## Security model

What happens on `POST /v1/commit`, in order:

1. Signature, freshness, nonce, IP and rate limit (as in the read gateway).
2. Idempotency claim: replay a stored answer, or refuse a key that's in use or was used for a different request.
3. Circuit breaker, then the allowed-hours window. Both are "try later" answers and are not stored.
4. One transaction, holding a per-caller advisory lock:
   1. Check the daily cap. This is exact, because one caller's commits run one at a time.
   2. Take the token with `FOR UPDATE`. It must be the caller's own, unused and unexpired.
   3. Re-run the action's rules with row locks, for example on the invoice being paid.
   4. Compare the new preview with the validated one. Any difference returns `state_changed`.
   5. Write, mark the token used, and add the audit row, all in the same transaction.
5. Store the response under the idempotency key.

Even if the code were wrong, the database role can't `DELETE` anything or change prices or totals
(`migrations/003_grants.sql`). The integration tests check this by connecting as that role.

## How it scales

- **Stateless API.** Nonces, rate limits and the breaker live in Redis, so you can run any number of
  instances behind a load balancer.
- **Locks are narrow:** one advisory lock per caller (not global) and row locks only on the record
  being changed. Different agents and different invoices commit in parallel.
- **Indexes** on the cap query (`key_id, used_at`), token lookup (unique `token_hash`), idempotency
  (primary key) and audit (`at`, `key_id, at`).
- **Housekeeping** keeps the token and idempotency tables small (`--cleanup`).

| Load | First bottleneck | Fix |
|---|---|---|
| 10× | One agent's commits are serialised by its lock | Fine for agents; give high-volume agents several keys if needed |
| 10× | Audit and idempotency writes on the primary | Partition `audit_log` by month; move old rows to cheap storage |
| 100× | Validation previews for big invoices | Cache product prices briefly (they change rarely) |
| 100× | One Redis | Redis cluster, or a managed service |

## Performance

Measured on a busy Windows laptop, **in-process**: client and API share one Python thread, and an
in-memory repository stands in for PostgreSQL. This measures the write path's own cost.

```bash
python scripts/loadtest.py --in-process --writes 1000 --concurrency 20
```

| Run | Writes/s (validate + commit = 2 requests) | p50 | p95 | p99 |
|---|---|---|---|---|
| 1 | 257 | 76 ms | 102 ms | 107 ms |
| 2 | 258 | 75 ms | 102 ms | 109 ms |
| 3 | 260 | 74 ms | 103 ms | 106 ms |

**A lesson from measuring:** the first run gave 27 writes/s. A profile showed 90% of the time in
the test repository's deep copy (its rollback snapshot), not in the API. A cheap snapshot fixed the
tool. Without the profile, I'd have "optimised" the wrong thing.

End to end against the real stack (after `docker compose up`):

```bash
python scripts/loadtest.py --writes 500 --concurrency 10
```

## Design decisions

- **Two steps, not one.** A preview the caller (or a person) can check, then a commit that can only
  write that preview. Agents make mistakes; this makes them visible before they're saved.
- **Server-side pricing.** Agents send intent (which product, how many), never amounts.
- **Re-check at commit, compare previews.** Validation is advice; the commit transaction is the
  truth.
- **Store final answers, not temporary ones,** under idempotency keys, so retries behave the way
  a careful client expects.
- **Business rejections close the breaker's trial.** A `422` means the system is healthy; only
  timeouts and server errors count as failures.
- **No DELETE at all.** Corrections are new records (for example a reversing payment), which keeps
  the history honest.

The reasoning is written up in my [architecture notes](https://github.com/SaurabhTiwari95/architecture-notes):
ADR 0003 and note 01.

## Project layout

```text
app/
  main.py        HTTP API, request authentication
  service.py     validate / commit flow, idempotency, caps, hours, breaker
  actions.py     create_invoice and record_payment: params, rules, preview, apply
  repository.py  PostgreSQL access: units of work, idempotency, audit
  stores.py      nonces, rate limits, circuit breaker (Redis / in-memory)
  config.py      settings, allowed hours, time zone
  signing.py     HMAC signing (shared with the client)
  migrate.py     migrations, app role, sample data, cleanup
client/          Python client (sync and async)
examples/        agent_writes.py
migrations/      schema, grants
scripts/         make_env, loadtest, prepublish_check
tests/           flow, API, units, repository, integration
```

## What's next

- Per-key action permissions and per-key caps.
- Amount-based daily caps (not only counts).
- The companion **agent-approval-flow**, where a person approves the preview before the commit.

---

AI-assisted (Claude Code); design and review by me.

Licence: [MIT](LICENSE). Security: [SECURITY.md](SECURITY.md).
