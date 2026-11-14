"""End-to-end tests against real PostgreSQL and Redis (run in CI; see README for local steps)."""

from __future__ import annotations

import asyncio
import datetime as dt
import os

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from client import AsyncWriteClient, WriteError

pytestmark = pytest.mark.skipif(not os.environ.get("INTEGRATION"), reason="set INTEGRATION=1 to run")

KEY_ID, SECRET = "it-agent", "integration-secret-" + "k" * 32
TODAY = dt.datetime.now(dt.UTC).date().isoformat()


@pytest.fixture
async def api():
    settings = Settings(_env_file=None, client_keys={KEY_ID: SECRET}, allowed_networks=["127.0.0.1/32"])
    app = create_app(settings)  # real PostgresRepository + Redis stores from the environment
    async with app.router.lifespan_context(app):
        async with AsyncWriteClient("http://w", KEY_ID, SECRET, transport=httpx.ASGITransport(app=app)) as c:
            yield c


@pytest.fixture
async def admin():
    import asyncpg

    conn = await asyncpg.connect(os.environ["ADMIN_DATABASE_URL"])
    yield conn
    await conn.close()


async def test_create_invoice_end_to_end(api, admin):
    check = await api.validate(
        "create_invoice",
        {"customer_id": 1, "issued_on": TODAY, "due_in_days": 30, "lines": [{"product_id": 1, "quantity": 2}]},
    )
    done = await api.commit(check["token"], idempotency_key="it-create-0001")
    invoice_id = done["result"]["invoice_id"]
    row = await admin.fetchrow("SELECT total, status FROM acme.invoices WHERE id = $1", invoice_id)
    assert str(row["total"]) == check["preview"]["total"] and row["status"] == "open"
    replay = await api.commit(check["token"], idempotency_key="it-create-0001")
    assert replay["_replayed"] and replay["result"] == done["result"]
    assert await admin.fetchval("SELECT count(*) FROM acme.invoices WHERE id > $1", invoice_id) == 0


async def test_concurrent_payments_on_one_invoice_cannot_overpay(api, admin):
    invoice_id = await admin.fetchval(
        "SELECT id FROM acme.invoices i WHERE status = 'open' "
        "AND NOT EXISTS (SELECT 1 FROM acme.payments p WHERE p.invoice_id = i.id) ORDER BY id LIMIT 1"
    )
    total = await admin.fetchval("SELECT total FROM acme.invoices WHERE id = $1", invoice_id)
    params = {"invoice_id": invoice_id, "amount": str(total), "paid_on": TODAY}
    a, b = await api.validate("record_payment", params), await api.validate("record_payment", params)
    results = await asyncio.gather(api.commit(a["token"]), api.commit(b["token"]), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    refused = next(r for r in results if isinstance(r, WriteError))
    assert refused.status == 422 and refused.body["details"][0]["rule"] in ("invoice_not_open", "overpayment")
    assert await admin.fetchval("SELECT status FROM acme.invoices WHERE id = $1", invoice_id) == "paid"


async def test_app_role_cannot_delete_or_change_totals():
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for sql in [
            "DELETE FROM acme.invoices WHERE id = 1",
            "UPDATE acme.invoices SET total = 0 WHERE id = 1",
            "UPDATE acme.products SET unit_price = 0",
            "DELETE FROM writeapi.audit_log",
            "SELECT * FROM writeapi.audit_log",
        ]:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(sql)
    finally:
        await conn.close()


async def test_audit_log_is_append_only_even_for_the_owner(admin):
    import asyncpg

    with pytest.raises(asyncpg.exceptions.RaiseError):
        await admin.execute("UPDATE writeapi.audit_log SET event = 'x'")


async def test_redis_breaker_is_shared():
    from redis.asyncio import Redis

    from app.stores import RedisBreaker

    redis = Redis.from_url(os.environ["REDIS_URL"])
    try:
        await redis.delete("wapi:breaker:open", "wapi:breaker:tripped", "wapi:breaker:trial", "wapi:breaker:fails")
        one, two = RedisBreaker(redis, 2, 60, 1), RedisBreaker(redis, 2, 60, 1)
        await one.record_failure()
        await two.record_failure()
        assert await one.state() == "open" and not await two.allow()
        await asyncio.sleep(1.2)
        assert await one.allow() is True and await two.allow() is False  # a single trial across instances
        await two.record_success()
        assert await one.state() == "closed"
    finally:
        await redis.aclose()
