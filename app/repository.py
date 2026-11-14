"""PostgreSQL access for the write path.

The app role (writeapi_app) can read what the rules need, INSERT invoices, lines and payments,
UPDATE only invoices.status, and write its own validation, idempotency and audit rows.
It has no DELETE anywhere (see migrations/003_grants.sql).
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.actions import InvoiceState

STUCK_AFTER = dt.timedelta(seconds=60)  # an in-progress idempotency row older than this was abandoned


class QueryTimeout(Exception):
    pass


@dataclass(frozen=True)
class Validation:
    id: str
    token_hash: str
    key_id: str
    action: str
    params: dict[str, Any]
    preview: dict[str, Any]
    created_at: dt.datetime
    expires_at: dt.datetime
    used_at: dt.datetime | None = None


@dataclass(frozen=True)
class IdemClaim:
    kind: Literal["new", "replay", "in_progress", "mismatch"]
    status_code: int | None = None
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditEntry:
    request_id: str
    key_id: str
    action: str
    event: str
    status_code: int
    validation_id: str | None = None
    detail: dict[str, Any] | None = None


class Repository(Protocol):
    def read(self): ...  # async context manager -> ReadUnitOfWork
    def write(self, key_id: str): ...  # async context manager -> transactional unit of work
    async def save_validation(self, v: Validation) -> None: ...
    async def claim_idempotency(self, key_id: str, idem_key: str, request_hash: str, now: dt.datetime) -> IdemClaim: ...
    async def finish_idempotency(self, key_id: str, idem_key: str, status_code: int, body: dict) -> None: ...
    async def fail_idempotency(self, key_id: str, idem_key: str) -> None: ...
    async def audit(self, entry: AuditEntry) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


_AUDIT_SQL = """
INSERT INTO writeapi.audit_log (request_id, key_id, action, event, status_code, validation_id, detail)
VALUES ($1, $2, $3, $4, $5, $6::uuid, $7::jsonb)
"""


def _audit_args(e: AuditEntry) -> tuple:
    return (e.request_id, e.key_id, e.action, e.event, e.status_code, e.validation_id, json.dumps(e.detail or {}))


class PgUnitOfWork:
    def __init__(self, conn):
        self.conn = conn

    async def customer_exists(self, customer_id: int) -> bool:
        return bool(await self.conn.fetchval("SELECT 1 FROM acme.customers WHERE id = $1", customer_id))

    async def product_prices(self, product_ids: list[int]) -> dict[int, Decimal]:
        rows = await self.conn.fetch("SELECT id, unit_price FROM acme.products WHERE id = ANY($1::int[])", product_ids)
        return {r["id"]: r["unit_price"] for r in rows}

    async def invoice_state(self, invoice_id: int, lock: bool = False) -> InvoiceState | None:
        if lock:  # serialise concurrent payments on the same invoice
            await self.conn.execute("SELECT id FROM acme.invoices WHERE id = $1 FOR UPDATE", invoice_id)
        row = await self.conn.fetchrow(
            """SELECT i.id, i.customer_id, i.status, i.issued_on, i.total,
                      COALESCE((SELECT sum(p.amount) FROM acme.payments p WHERE p.invoice_id = i.id), 0) AS paid
               FROM acme.invoices i WHERE i.id = $1""",
            invoice_id,
        )
        return InvoiceState(**dict(row)) if row else None

    # --- writes (only reachable inside the commit transaction) ---

    async def insert_invoice(self, customer_id, issued_on, due_on, total, lines) -> int:
        invoice_id = await self.conn.fetchval(
            """INSERT INTO acme.invoices (customer_id, issued_on, due_on, status, total)
               VALUES ($1, $2, $3, 'open', $4) RETURNING id""",
            customer_id,
            issued_on,
            due_on,
            total,
        )
        await self.conn.executemany(
            "INSERT INTO acme.invoice_lines (invoice_id, product_id, quantity, unit_price) VALUES ($1, $2, $3, $4)",
            [(invoice_id, pid, qty, price) for pid, qty, price in lines],
        )
        return invoice_id

    async def insert_payment(self, invoice_id, paid_on, amount) -> int:
        return await self.conn.fetchval(
            "INSERT INTO acme.payments (invoice_id, paid_on, amount) VALUES ($1, $2, $3) RETURNING id",
            invoice_id,
            paid_on,
            amount,
        )

    async def set_invoice_status(self, invoice_id, status) -> None:
        await self.conn.execute("UPDATE acme.invoices SET status = $2 WHERE id = $1", invoice_id, status)

    # --- governance ---

    async def take_validation(self, token_hash: str) -> Validation | None:
        row = await self.conn.fetchrow(
            """SELECT id::text, token_hash, key_id, action, params, preview, created_at, expires_at, used_at
               FROM writeapi.validations WHERE token_hash = $1 FOR UPDATE""",
            token_hash,
        )
        if row is None:
            return None
        data = dict(row)
        data["params"], data["preview"] = json.loads(data["params"]), json.loads(data["preview"])
        return Validation(**data)

    async def mark_validation_used(self, validation_id: str, at: dt.datetime) -> None:
        await self.conn.execute("UPDATE writeapi.validations SET used_at = $2 WHERE id = $1::uuid", validation_id, at)

    async def commits_since(self, key_id: str, since: dt.datetime) -> int:
        return await self.conn.fetchval(
            "SELECT count(*) FROM writeapi.validations WHERE key_id = $1 AND used_at >= $2", key_id, since
        )

    async def audit(self, entry: AuditEntry) -> None:
        await self.conn.execute(_AUDIT_SQL, *_audit_args(entry))


class PostgresRepository:
    def __init__(self, pool):
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str, min_size: int, max_size: int, statement_timeout_ms: int):
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=statement_timeout_ms / 1000 + 1,
            server_settings={"statement_timeout": str(statement_timeout_ms), "application_name": "governed-write-api"},
        )
        return cls(pool)

    @asynccontextmanager
    async def read(self):
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            yield PgUnitOfWork(conn)

    @asynccontextmanager
    async def write(self, key_id: str):
        import asyncpg

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                # One commit at a time per caller, so the daily cap is exact.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key_id)
                yield PgUnitOfWork(conn)
        except asyncpg.exceptions.QueryCanceledError as exc:
            raise QueryTimeout() from exc

    async def save_validation(self, v: Validation) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO writeapi.validations
                   (id, token_hash, key_id, action, params, preview, created_at, expires_at)
                   VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8)""",
                v.id,
                v.token_hash,
                v.key_id,
                v.action,
                json.dumps(v.params),
                json.dumps(v.preview),
                v.created_at,
                v.expires_at,
            )

    async def claim_idempotency(self, key_id: str, idem_key: str, request_hash: str, now: dt.datetime) -> IdemClaim:
        async with self._pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchval(
                """INSERT INTO writeapi.idempotency (key_id, idem_key, request_hash, state, created_at)
                   VALUES ($1, $2, $3, 'in_progress', $4)
                   ON CONFLICT (key_id, idem_key) DO NOTHING RETURNING 1""",
                key_id,
                idem_key,
                request_hash,
                now,
            )
            if inserted:
                return IdemClaim("new")
            row = await conn.fetchrow(
                """SELECT request_hash, state, status_code, response, created_at
                   FROM writeapi.idempotency WHERE key_id = $1 AND idem_key = $2 FOR UPDATE""",
                key_id,
                idem_key,
            )
            if row["request_hash"] != request_hash:
                return IdemClaim("mismatch")
            if row["state"] == "done":
                return IdemClaim("replay", row["status_code"], json.loads(row["response"]))
            if row["state"] == "in_progress" and now - row["created_at"] < STUCK_AFTER:
                return IdemClaim("in_progress")
            await conn.execute(
                """UPDATE writeapi.idempotency SET state = 'in_progress', created_at = $3
                   WHERE key_id = $1 AND idem_key = $2""",
                key_id,
                idem_key,
                now,
            )
            return IdemClaim("new")

    async def finish_idempotency(self, key_id: str, idem_key: str, status_code: int, body: dict) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE writeapi.idempotency
                   SET state = 'done', status_code = $3, response = $4::jsonb, finished_at = now()
                   WHERE key_id = $1 AND idem_key = $2""",
                key_id,
                idem_key,
                status_code,
                json.dumps(body),
            )

    async def fail_idempotency(self, key_id: str, idem_key: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE writeapi.idempotency SET state = 'failed' WHERE key_id = $1 AND idem_key = $2", key_id, idem_key
            )

    async def audit(self, entry: AuditEntry) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_AUDIT_SQL, *_audit_args(entry))

    async def ping(self) -> bool:
        try:
            async with self._pool.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:
            return False

    async def close(self) -> None:
        await self._pool.close()
