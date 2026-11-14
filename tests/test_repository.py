"""PostgresRepository and the migration runner against small fakes of asyncpg.
The real database is exercised by tests/integration in CI."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from app import migrate
from app.repository import AuditEntry, PgUnitOfWork, PostgresRepository, QueryTimeout, Validation

NOW = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)


class FakeConn:
    def __init__(self, fetchval=None, fetchrow=None, fetch=None, fail=None):
        self._fetchval, self._fetchrow, self._fetch, self.fail = fetchval, fetchrow, fetch, fail
        self.calls: list[tuple] = []
        self.transactions: list[dict] = []

    def _answer(self, value, sql, args):
        return value(sql, args) if callable(value) else value

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self._answer(self._fetchval, sql, args)

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._answer(self._fetchrow, sql, args)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._answer(self._fetch, sql, args) or []

    async def execute(self, sql, *args):
        if self.fail:
            raise self.fail
        self.calls.append(("execute", sql, args))
        return "DELETE 3"

    async def executemany(self, sql, rows):
        self.calls.append(("executemany", sql, rows))

    async def copy_records_to_table(self, table, **kwargs):
        self.calls.append(("copy", table))

    @asynccontextmanager
    async def _tx(self, **kw):
        self.transactions.append(kw)
        yield

    def transaction(self, **kw):
        return self._tx(**kw)


class FakePool:
    def __init__(self, conn):
        self.conn, self.closed = conn, False

    @asynccontextmanager
    async def _acquire(self):
        yield self.conn

    def acquire(self):
        return self._acquire()

    async def close(self):
        self.closed = True


def sqls(conn):
    return [c[1] for c in conn.calls if len(c) > 1 and isinstance(c[1], str)]


async def test_read_is_read_only_and_write_takes_per_caller_lock():
    conn = FakeConn(fetchval=1)
    repo = PostgresRepository(FakePool(conn))
    async with repo.read() as uow:
        assert await uow.customer_exists(1)
    async with repo.write("agent-1") as uow:
        assert isinstance(uow, PgUnitOfWork)
    assert conn.transactions == [{"readonly": True}, {}]
    assert any("pg_advisory_xact_lock" in s for s in sqls(conn))


async def test_write_turns_query_cancel_into_timeout():
    asyncpg = pytest.importorskip("asyncpg")
    repo = PostgresRepository(FakePool(FakeConn(fail=asyncpg.exceptions.QueryCanceledError("x"))))
    with pytest.raises(QueryTimeout):
        async with repo.write("agent-1"):
            pass


async def test_unit_of_work_queries():
    row = {
        "id": 5,
        "customer_id": 1,
        "status": "open",
        "issued_on": dt.date(2026, 1, 1),
        "total": Decimal("9"),
        "paid": Decimal("2"),
    }
    conn = FakeConn(fetchval=77, fetchrow=row, fetch=[{"id": 1, "unit_price": Decimal("3.50")}])
    uow = PgUnitOfWork(conn)
    assert await uow.product_prices([1]) == {1: Decimal("3.50")}
    state = await uow.invoice_state(5, lock=True)
    assert state.total - state.paid == Decimal("7")
    assert any("FOR UPDATE" in s for s in sqls(conn))
    assert (
        await uow.insert_invoice(1, dt.date(2026, 1, 1), dt.date(2026, 2, 1), Decimal("9"), [(1, 2, Decimal("4.5"))])
        == 77
    )
    assert conn.calls[-1][0] == "executemany" and conn.calls[-1][2] == [(77, 1, 2, Decimal("4.5"))]
    assert await uow.insert_payment(5, dt.date(2026, 1, 2), Decimal("1")) == 77
    await uow.set_invoice_status(5, "paid")
    assert await uow.commits_since("agent", NOW) == 77
    await uow.mark_validation_used("v-id", NOW)
    await uow.audit(AuditEntry("rid", "agent", "create_invoice", "committed", 201, None, {"invoice_id": 1}))
    assert json.loads(conn.calls[-1][2][-1]) == {"invoice_id": 1}
    conn._fetchrow = None
    assert await uow.invoice_state(404) is None


async def test_take_validation_decodes_json():
    row = {
        "id": "v1", "token_hash": "h", "key_id": "agent", "action": "create_invoice",
        "params": '{"a": 1}', "preview": '{"total": "1.00"}', "created_at": NOW, "expires_at": NOW, "used_at": None,
    }  # fmt: skip
    uow = PgUnitOfWork(FakeConn(fetchrow=row))
    v = await uow.take_validation("h")
    assert v.params == {"a": 1} and v.preview == {"total": "1.00"}
    assert await PgUnitOfWork(FakeConn(fetchrow=None)).take_validation("h") is None


async def test_save_validation_audit_ping_close():
    conn = FakeConn(fetchval=1)
    pool = FakePool(conn)
    repo = PostgresRepository(pool)
    await repo.save_validation(Validation("v1", "h", "agent", "create_invoice", {"a": 1}, {"b": 2}, NOW, NOW))
    assert json.loads(conn.calls[-1][2][4]) == {"a": 1}
    await repo.audit(AuditEntry("rid", "agent", "-", "replayed", 201))
    assert await repo.ping() is True
    await repo.close()
    assert pool.closed


async def test_ping_false_when_down():
    class Down(FakePool):
        def acquire(self):
            raise OSError("refused")

    assert await PostgresRepository(Down(FakeConn())).ping() is False


def idem_row(state, request_hash="h", status=None, response=None, age=dt.timedelta(0)):
    return {
        "request_hash": request_hash,
        "state": state,
        "status_code": status,
        "response": response,
        "created_at": NOW - age,
    }


@pytest.mark.parametrize(
    "inserted, row, expected",
    [
        (1, None, "new"),
        (None, idem_row("done", request_hash="other", status=201, response="{}"), "mismatch"),
        (None, idem_row("done", status=201, response='{"ok": 1}'), "replay"),
        (None, idem_row("in_progress"), "in_progress"),
        (None, idem_row("in_progress", age=dt.timedelta(minutes=5)), "new"),
        (None, idem_row("failed"), "new"),
    ],
)
async def test_claim_idempotency_branches(inserted, row, expected):
    conn = FakeConn(fetchval=inserted, fetchrow=row)
    claim = await PostgresRepository(FakePool(conn)).claim_idempotency("agent", "key-0001", "h", NOW)
    assert claim.kind == expected
    if expected == "replay":
        assert claim.body == {"ok": 1} and claim.status_code == 201


async def test_finish_and_fail_idempotency():
    conn = FakeConn()
    repo = PostgresRepository(FakePool(conn))
    await repo.finish_idempotency("agent", "k", 201, {"x": 1})
    await repo.fail_idempotency("agent", "k")
    assert "state = 'done'" in sqls(conn)[0] and "state = 'failed'" in sqls(conn)[1]


# --- migrate ------------------------------------------------------------------------------------


async def test_ensure_role_uses_writeapi_app_and_quotes_password():
    conn = FakeConn(fetchval=lambda sql, args: None if "pg_roles" in sql else "CREATE ROLE x")
    await migrate.ensure_app_role(conn, "p" * 20)
    assert "CREATE ROLE writeapi_app" in conn.calls[1][1] and conn.calls[1][2] == ("p" * 20,)
    with pytest.raises(ValueError):
        await migrate.ensure_app_role(conn, "short")


async def test_apply_migrations_and_cleanup(tmp_path):
    (tmp_path / "001.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "002.sql").write_text("SELECT 2", encoding="utf-8")
    conn = FakeConn(fetch=[{"version": "001.sql"}])
    assert await migrate.apply_migrations(conn, tmp_path) == ["002.sql"]
    assert await migrate.cleanup(conn) == (3, 3)


async def test_seed_and_sample_data():
    data = migrate.generate_sample_data(customers=5, products=3, invoices=10, seed=3, today=dt.date(2026, 2, 1))
    assert all(c[2].endswith(".example.com") for c in data.customers)
    assert await migrate.seed(FakeConn(fetchval=1), data) is False
    conn = FakeConn(fetchval=0)
    assert await migrate.seed(conn, data) is True
    assert [c[1] for c in conn.calls if c[0] == "copy"] == [
        "customers",
        "products",
        "invoices",
        "invoice_lines",
        "payments",
    ]


def test_migrate_main_needs_environment(monkeypatch):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DB_PASSWORD", raising=False)
    assert migrate.main(["--cleanup"]) == 2
