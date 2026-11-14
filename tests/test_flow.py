"""The governed write flow end to end through the HTTP API (in-memory repository)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from app.main import create_app
from app.repository import QueryTimeout
from app.stores import MemoryNonceStore, MemoryRateLimiter
from client import AsyncWriteClient, WriteError
from tests.conftest import KEY_ID, SECRET
from tests.fakes import FakeUnitOfWork

INVOICE = {"customer_id": 1, "issued_on": "2026-01-31", "due_in_days": 30, "lines": [
    {"product_id": 1, "quantity": 3}, {"product_id": 2, "quantity": 4}]}  # fmt: skip


async def expect(status: int, coro):
    with pytest.raises(WriteError) as err:
        await coro
    assert err.value.status == status, err.value.body
    return err.value.body


# --- happy path ---------------------------------------------------------------------------------


async def test_actions_are_listed_with_parameter_schemas(api):
    actions = {a["name"]: a for a in await api.actions()}
    assert set(actions) == {"create_invoice", "record_payment"}
    assert "lines" in actions["create_invoice"]["params"]["properties"]


async def test_validate_previews_prices_from_the_database(api, repo):
    check = await api.validate("create_invoice", INVOICE)
    assert check["preview"]["total"] == "40.00"  # 3 x 10.00 + 4 x 2.50
    assert check["preview"]["due_on"] == "2026-03-02"
    assert [ln["unit_price"] for ln in check["preview"]["lines"]] == ["10.00", "2.50"]
    assert len(repo.data.invoices) == 2  # validate writes nothing
    assert repo.audit_log[-1].event == "validated"


async def test_commit_writes_exactly_what_was_previewed(api, repo):
    check = await api.validate("create_invoice", INVOICE)
    done = await api.commit(check["token"])
    assert done["result"] == {"invoice_id": 102, "total": "40.00", "status": "open"}
    assert repo.data.invoices[102]["total"] == Decimal("40.00")
    assert repo.data.lines == [(102, 1, 3, Decimal("10.00")), (102, 2, 4, Decimal("2.50"))]
    committed = repo.audit_log[-1]
    assert (committed.event, committed.status_code, committed.detail["invoice_id"]) == ("committed", 201, 102)


async def test_payment_that_settles_marks_invoice_paid(api, repo):
    check = await api.validate("record_payment", {"invoice_id": 100, "amount": "100.00", "paid_on": "2026-02-01"})
    assert check["preview"]["settles_invoice"] is True
    done = await api.commit(check["token"])
    assert done["result"]["invoice_status"] == "paid"
    assert repo.data.invoices[100]["status"] == "paid"


async def test_partial_payment_keeps_invoice_open(api, repo):
    check = await api.validate("record_payment", {"invoice_id": 100, "amount": "40", "paid_on": "2026-01-20"})
    assert check["preview"]["outstanding_after"] == "60.00"
    assert (await api.commit(check["token"]))["result"]["invoice_status"] == "open"


# --- idempotency --------------------------------------------------------------------------------


async def test_retry_with_same_key_replays_without_writing_again(api, repo):
    check = await api.validate("create_invoice", INVOICE)
    first = await api.commit(check["token"], idempotency_key="retry-key-0001")
    second = await api.commit(check["token"], idempotency_key="retry-key-0001")
    assert second["result"] == first["result"] and second["_replayed"] is True and first["_replayed"] is False
    assert len(repo.data.invoices) == 3  # one new invoice, not two
    assert repo.audit_log[-1].event == "replayed"


async def test_same_key_with_different_request_is_refused(api):
    a = await api.validate("create_invoice", INVOICE)
    b = await api.validate("create_invoice", INVOICE)
    await api.commit(a["token"], idempotency_key="shared-key-01")
    body = await expect(422, api.commit(b["token"], idempotency_key="shared-key-01"))
    assert body["error"] == "idempotency_key_reused"


async def test_final_errors_are_replayed_too(api):
    body = await expect(404, api.commit("x" * 43, idempotency_key="final-err-01"))
    assert body["error"] == "unknown_token"
    again = await expect(404, api.commit("x" * 43, idempotency_key="final-err-01"))
    assert again == body


async def test_key_in_progress_is_409_until_stale(api, repo, clock):
    check = await api.validate("create_invoice", INVOICE)
    import hashlib
    import json

    body = json.dumps({"token": check["token"]}, separators=(",", ":")).encode()
    await repo.claim_idempotency(KEY_ID, "busy-key-0001", hashlib.sha256(body).hexdigest(), clock())
    assert (await expect(409, api.commit(check["token"], idempotency_key="busy-key-0001")))["error"] == (
        "idempotency_key_in_progress"
    )
    clock.advance(seconds=61)  # the first attempt crashed; the key can be taken over
    assert (await api.commit(check["token"], idempotency_key="busy-key-0001"))["result"]["invoice_id"] == 102


@pytest.mark.parametrize("key", [None, "short", "has spaces in it", "x" * 129])
async def test_commit_needs_a_well_formed_idempotency_key(raw, key):
    from app.signing import signed_headers

    body = b'{"token":"' + b"t" * 43 + b'"}'
    headers = signed_headers(KEY_ID, SECRET, "POST", "/v1/commit", body)
    if key is not None:
        headers["Idempotency-Key"] = key
    r = await raw.post("/v1/commit", content=body, headers=headers)
    assert r.status_code == 400 and r.json()["error"] == "idempotency_key_required"


# --- tokens -------------------------------------------------------------------------------------


async def test_token_is_single_use(api):
    check = await api.validate("create_invoice", INVOICE)
    await api.commit(check["token"])
    assert (await expect(409, api.commit(check["token"])))["error"] == "token_already_used"


async def test_token_expires(api, clock):
    check = await api.validate("create_invoice", INVOICE)
    clock.advance(minutes=11)
    assert (await expect(410, api.commit(check["token"])))["error"] == "token_expired"


async def test_token_belongs_to_the_caller_who_validated(settings, repo, breaker, clock):
    settings.client_keys = {KEY_ID: SECRET, "agent-2": "second-agent-secret-" + "z" * 30}
    app = create_app(
        settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter(), breaker=breaker, clock=clock
    )
    async with app.router.lifespan_context(app):
        t = httpx.ASGITransport(app=app)
        async with (
            AsyncWriteClient("http://w", KEY_ID, SECRET, transport=t) as one,
            AsyncWriteClient("http://w", "agent-2", settings.client_keys["agent-2"], transport=t) as two,
        ):
            check = await one.validate("create_invoice", INVOICE)
            assert (await expect(404, two.commit(check["token"])))["error"] == "unknown_token"


async def test_concurrent_commits_of_one_token_write_once(api, repo):
    check = await api.validate("create_invoice", INVOICE)
    results = await asyncio.gather(*[api.commit(check["token"]) for _ in range(5)], return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, WriteError)]
    assert len(ok) == 1 and len(refused) == 4
    assert {r.body["error"] for r in refused} == {"token_already_used"}
    assert len(repo.data.invoices) == 3


# --- data changed between validate and commit ---------------------------------------------------


async def test_commit_refuses_when_the_preview_no_longer_holds(api):
    slow = await api.validate("record_payment", {"invoice_id": 100, "amount": "30", "paid_on": "2026-02-01"})
    fast = await api.validate("record_payment", {"invoice_id": 100, "amount": "10", "paid_on": "2026-02-01"})
    await api.commit(fast["token"])
    body = await expect(409, api.commit(slow["token"]))
    assert body["error"] == "state_changed"
    assert body["details"]["validated"]["outstanding_before"] == "100.00"
    assert body["details"]["now"]["outstanding_before"] == "90.00"


async def test_commit_rechecks_rules(api):
    big = await api.validate("record_payment", {"invoice_id": 100, "amount": "80", "paid_on": "2026-02-01"})
    other = await api.validate("record_payment", {"invoice_id": 100, "amount": "50", "paid_on": "2026-02-01"})
    await api.commit(other["token"])
    body = await expect(422, api.commit(big["token"]))
    assert body["error"] == "rule_violation" and body["details"][0]["rule"] == "overpayment"


# --- business rules -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action, params, rule",
    [
        ("create_invoice", {**INVOICE, "customer_id": 999}, "unknown_customer"),
        ("create_invoice", {**INVOICE, "lines": [{"product_id": 77, "quantity": 1}]}, "unknown_product"),
        ("create_invoice", {**INVOICE, "lines": [{"product_id": 1, "quantity": 1}] * 2}, "duplicate_product"),
        ("create_invoice", {**INVOICE, "issued_on": "2026-02-02"}, "issued_in_future"),
        ("create_invoice", {**INVOICE, "issued_on": "2025-10-01"}, "issued_too_old"),
        ("create_invoice", {**INVOICE, "lines": [{"product_id": 3, "quantity": 10_000}]}, "amount_over_limit"),
        ("record_payment", {"invoice_id": 404, "amount": "1", "paid_on": "2026-02-01"}, "unknown_invoice"),
        ("record_payment", {"invoice_id": 101, "amount": "1", "paid_on": "2026-02-01"}, "invoice_not_open"),
        ("record_payment", {"invoice_id": 100, "amount": "100.01", "paid_on": "2026-02-01"}, "overpayment"),
        ("record_payment", {"invoice_id": 100, "amount": "1", "paid_on": "2026-02-02"}, "paid_in_future"),
        ("record_payment", {"invoice_id": 100, "amount": "1", "paid_on": "2026-01-09"}, "paid_before_issue"),
    ],
)
async def test_rule_violations_are_explained_and_audited(api, repo, action, params, rule):
    body = await expect(422, api.validate(action, params))
    assert body["error"] == "rule_violation"
    assert rule in [v["rule"] for v in body["details"]]
    assert repo.audit_log[-1].event == "validate_rejected"


async def test_too_many_lines(api, settings):
    settings.max_invoice_lines = 1
    body = await expect(422, api.validate("create_invoice", INVOICE))
    assert "too_many_lines" in [v["rule"] for v in body["details"]]


@pytest.mark.parametrize(
    "action, params",
    [
        ("create_invoice", {**INVOICE, "discount": 5}),
        ("create_invoice", {**INVOICE, "lines": [{"product_id": 1, "quantity": 0}]}),
        ("create_invoice", {**INVOICE, "lines": []}),
        ("create_invoice", {**INVOICE, "lines": [{"product_id": 1, "quantity": 1, "unit_price": "0.01"}]}),
        ("record_payment", {"invoice_id": 100, "amount": "1.001", "paid_on": "2026-02-01"}),
        ("record_payment", {"invoice_id": 100, "amount": "-5", "paid_on": "2026-02-01"}),
        ("record_payment", {"invoice_id": 100, "paid_on": "2026-02-01"}),
    ],
)
async def test_invalid_params_are_refused_before_any_rule(api, action, params):
    body = await expect(422, api.validate(action, params))
    assert body["error"] == "invalid_params"


async def test_unknown_action(api):
    assert (await expect(404, api.validate("delete_customer", {})))["error"] == "unknown_action"


@pytest.mark.parametrize("body", [b"not json", b'{"params": [], "x": 1}'])
async def test_invalid_validate_body(raw, body):
    from app.signing import signed_headers

    r = await raw.post("/v1/validate/create_invoice", content=body, headers=signed_headers(
        KEY_ID, SECRET, "POST", "/v1/validate/create_invoice", body))  # fmt: skip
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


# --- limits: daily cap, hours, circuit breaker --------------------------------------------------


async def test_daily_cap(api, settings, repo):
    settings.daily_commit_cap = 2
    for _ in range(2):
        await api.commit((await api.validate("create_invoice", INVOICE))["token"])
    third = await api.validate("create_invoice", INVOICE)
    body = await expect(429, api.commit(third["token"]))
    assert body["error"] == "daily_cap_reached" and body["details"] == {"cap": 2}
    assert repo.audit_log[-1].event == "commit_deferred"
    assert len(repo.data.invoices) == 4


async def test_daily_cap_is_not_stored_so_same_request_succeeds_later(api, settings, clock):
    settings.daily_commit_cap = 1
    settings.validation_ttl_seconds = 3_600 * 24
    await api.commit((await api.validate("create_invoice", INVOICE))["token"])
    second = await api.validate("create_invoice", INVOICE)
    await expect(429, api.commit(second["token"], idempotency_key="later-key-001"))
    clock.advance(hours=14)  # 2026-02-02 00:30 UTC: a new day
    done = await api.commit(second["token"], idempotency_key="later-key-001")
    assert done["result"]["invoice_id"] == 103


async def test_outside_allowed_hours(api, settings, clock):
    settings.allowed_hours = "09:00-17:00"
    settings.validation_ttl_seconds = 3_600 * 24
    check = await api.validate("create_invoice", INVOICE)  # validating is always allowed
    clock.advance(hours=8)  # 18:30
    body = await expect(403, api.commit(check["token"], idempotency_key="hours-key-01"))
    assert body["error"] == "outside_allowed_hours" and body["details"] == {"allowed_hours": "09:00-17:00"}
    clock.advance(hours=15)  # next day 09:30
    assert (await api.commit(check["token"], idempotency_key="hours-key-01"))["result"]["invoice_id"] == 102


async def test_breaker_opens_after_failures_then_recovers(api, repo, clock, raw):
    repo.fail_on_write = RuntimeError("database exploded")
    for _ in range(3):
        check = await api.validate("create_invoice", INVOICE)
        body = await expect(500, api.commit(check["token"]))
        assert body["error"] == "internal_error" and "exploded" not in str(body)
    repo.fail_on_write = None
    check = await api.validate("create_invoice", INVOICE)
    r = await expect(503, api.commit(check["token"], idempotency_key="breaker-key-1"))
    assert r["error"] == "circuit_open"
    assert (await raw.get("/readyz")).json()["breaker"] == "open"
    clock.advance(seconds=31)  # cooldown over: one trial allowed, it succeeds, breaker closes
    assert (await api.commit(check["token"], idempotency_key="breaker-key-1"))["result"]["invoice_id"] == 102
    assert (await raw.get("/readyz")).json()["breaker"] == "closed"


async def test_query_timeout_is_504_and_counts_as_failure(api, repo, breaker):
    repo.fail_on_write = QueryTimeout()
    check = await api.validate("create_invoice", INVOICE)
    assert (await expect(504, api.commit(check["token"])))["error"] == "query_timeout"
    assert len(breaker._failures) == 1


async def test_failure_mid_write_rolls_everything_back(api, repo, monkeypatch):
    async def boom(self, invoice_id, status):
        raise RuntimeError("disk full")

    monkeypatch.setattr(FakeUnitOfWork, "set_invoice_status", boom)
    check = await api.validate("record_payment", {"invoice_id": 100, "amount": "100", "paid_on": "2026-02-01"})
    await expect(500, api.commit(check["token"], idempotency_key="rollback-key1"))
    assert len(repo.data.payments) == 1  # the payment insert was rolled back
    assert repo.data.validations[next(iter(repo.data.validations))].used_at is None
    monkeypatch.undo()
    done = await api.commit(check["token"], idempotency_key="rollback-key1")  # same key: retry allowed
    assert done["result"]["invoice_status"] == "paid"


async def test_audit_failure_does_not_block_validation(api, repo):
    repo.audit_fails = True
    assert (await api.validate("create_invoice", INVOICE))["preview"]["total"] == "40.00"


async def test_tokens_are_stored_only_as_hashes(api, repo):
    check = await api.validate("create_invoice", INVOICE)
    stored = next(iter(repo.data.validations.values()))
    assert check["token"] not in str(stored.__dict__)


def test_amounts_are_strings_with_two_decimals():
    from app.actions import _money

    assert [_money(Decimal(v)) for v in ("2.5", "10", "1234.567")] == ["2.50", "10.00", "1234.57"]
