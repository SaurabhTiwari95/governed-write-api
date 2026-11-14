# Release checklist

The same checklist every one of my projects meets before publishing. Ticked only when verified;
anything checked only by CI (not yet run) says so.

Last checked: 2026-11-09

## A. Installation and usage

- [x] What it is, with a sequence diagram
- [x] Prerequisites with versions and links
- [x] Install steps for Windows (PowerShell) and macOS/Linux
- [x] Usage with expected output: example agent, Python client, actions table, HTTP contract, error table
- [x] One command for tests; integration test steps documented
- [x] Configuration table for every environment variable
- [x] Troubleshooting
- [x] Design decisions, how it scales, performance, what's next
- [x] AI-assisted line
- [x] Relative links and anchors checked (0 broken); external links checked in CI by lychee
- [ ] Docker install steps run end to end: **not run on the build machine (no Docker there)**. CI runs the compose stack plus the example agent on every push

## B. No private data

- [x] Made-up "Acme Traders" data; Faker company names; emails on `example.com`
- [x] No employer, product or people names; no real hosts, IPs, account ids or keys
- [x] No work code, names, counts or costs
- [x] Pre-publish check: 0 hits locally against the private block-list; also runs in CI
- [ ] gitleaks: CI only

## C. Secure by default

- [x] Secrets only from the environment; `.env` git-ignored; `scripts/make_env.py` generates them
- [x] Weak or placeholder secrets, bad hours and unknown time zones refused at start-up (tested)
- [x] Pydantic validation with bounds; unknown fields refused; body size limit
- [x] Parameterised SQL only
- [x] Least privilege: no DELETE; UPDATE only on `invoices.status` and the API's own bookkeeping columns
- [x] Server-side pricing; preview re-checked under row locks at commit
- [x] Tokens single-use, expiring, stored only as SHA-256 hashes (tested)
- [x] Rate limits, daily caps, allowed hours, circuit breaker (all tested)
- [x] Errors never echo internals (tested)
- [x] Dependencies pinned to current releases; **pip-audit: no known vulnerabilities** (2026-11-09)
- [x] Dependabot (pip, Actions, Docker); CodeQL workflow; gitleaks in CI
- [x] Docker: multi-stage, non-root, read-only filesystem, no capabilities, ports bound to 127.0.0.1
- [x] `SECURITY.md`
- [ ] CodeQL results: first run on GitHub after the push
- [ ] Actions pinned to commit SHAs: major versions for now; Dependabot keeps them current
- [ ] Repo settings on GitHub after the push: secret scanning + push protection, private vulnerability reporting, branch protection

## D. Scalable

- [x] Stateless API; nonces, rate limits and breaker in Redis (breaker sharing integration-tested in CI)
- [x] 12-factor config
- [x] Connection pool; SQL migrations with a runner and an advisory lock; cleanup job
- [x] Indexes for the cap query, token lookup, idempotency and audit
- [x] Narrow locks: per caller and per row, not global
- [x] `/healthz` and `/readyz` (with breaker state)
- [x] "How it scales" section
- N/A Paging: no list endpoints (the read gateway serves reads)

## E. Optimised

- [x] Async I/O end to end
- [x] One transaction per commit; batched line inserts (`executemany`)
- [x] Profiled the load test; fixed the real hotspot (the test repository's deep copy, 10× faster)
- [x] Multi-stage Docker image
- [x] Load test with numbers in the README (in-process). End-to-end numbers need the Docker stack

## F. Repo basics

- [x] Tests: 124 passed locally, 92% coverage (gate 80% in CI); 5 integration tests run in CI against PostgreSQL and Redis
- [x] Lint: ruff check and format clean
- [x] Markdown: core rules checked locally (0 issues); full markdownlint in CI
- [x] `.gitignore`, `LICENSE` (MIT), `CHANGELOG.md`, `docker-compose.yml`
