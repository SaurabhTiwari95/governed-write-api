"""An in-memory repository with real transaction semantics (snapshot + rollback), for unit and API tests.
The PostgreSQL repository is exercised by tests/integration in CI."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
from contextlib import asynccontextmanager
from decimal import Decimal

from app.actions import InvoiceState
from app.repository import AuditEntry, IdemClaim, QueryTimeout, Validation

STUCK_AFTER = dt.timedelta(seconds=60)


class _Data:
    def __init__(self):
        self.customers = {1: "Acme Customer One", 2: "Acme Customer Two"}
        self.products = {1: Decimal("10.00"), 2: Decimal("2.50"), 3: Decimal("999.99")}
        self.invoices = {
            100: {"customer_id": 1, "status": "open", "issued_on": dt.date(2026, 1, 10), "total": Decimal("100.00")},
            101: {"customer_id": 2, "status": "paid", "issued_on": dt.date(2026, 1, 11), "total": Decimal("50.00")},
        }
        self.lines: list[tuple] = []
        self.payments: dict[int, dict] = {
            1: {"invoice_id": 101, "paid_on": dt.date(2026, 1, 12), "amount": Decimal("50.00")}
        }
        self.validations: dict[str, Validation] = {}
        self.audit: list[AuditEntry] = []

    def snapshot(self) -> _Data:
        """Cheap copy for rollback: nested dicts copied, frozen Validation objects shared."""
        s = _Data.__new__(_Data)
        s.customers, s.products = dict(self.customers), dict(self.products)
        s.invoices = {k: dict(v) for k, v in self.invoices.items()}
        s.payments = {k: dict(v) for k, v in self.payments.items()}
        s.lines, s.validations, s.audit = list(self.lines), dict(self.validations), list(self.audit)
        return s


class FakeUnitOfWork:
    def __init__(self, data: _Data, writable: bool):
        self.d, self.writable = data, writable

    async def customer_exists(self, customer_id):
        return customer_id in self.d.customers

    async def product_prices(self, product_ids):
        return {i: self.d.products[i] for i in product_ids if i in self.d.products}

    async def invoice_state(self, invoice_id, lock=False):
        inv = self.d.invoices.get(invoice_id)
        if inv is None:
            return None
        paid = sum((p["amount"] for p in self.d.payments.values() if p["invoice_id"] == invoice_id), Decimal("0"))
        return InvoiceState(invoice_id, inv["customer_id"], inv["status"], inv["issued_on"], inv["total"], paid)

    def _need_write(self):
        if not self.writable:
            raise PermissionError("cannot execute INSERT in a read-only transaction")

    async def insert_invoice(self, customer_id, issued_on, due_on, total, lines):
        self._need_write()
        new_id = max(self.d.invoices) + 1
        self.d.invoices[new_id] = {"customer_id": customer_id, "status": "open", "issued_on": issued_on, "total": total}
        self.d.lines += [(new_id, *line) for line in lines]
        return new_id

    async def insert_payment(self, invoice_id, paid_on, amount):
        self._need_write()
        new_id = max(self.d.payments, default=0) + 1
        self.d.payments[new_id] = {"invoice_id": invoice_id, "paid_on": paid_on, "amount": amount}
        return new_id

    async def set_invoice_status(self, invoice_id, status):
        self._need_write()
        self.d.invoices[invoice_id]["status"] = status

    async def take_validation(self, token_hash):
        return next((v for v in self.d.validations.values() if v.token_hash == token_hash), None)

    async def mark_validation_used(self, validation_id, at):
        v = self.d.validations[validation_id]
        self.d.validations[validation_id] = Validation(**{**v.__dict__, "used_at": at})

    async def commits_since(self, key_id, since):
        return sum(1 for v in self.d.validations.values() if v.key_id == key_id and v.used_at and v.used_at >= since)

    async def audit(self, entry):
        self.d.audit.append(entry)


class FakeRepository:
    def __init__(self):
        self.data = _Data()
        self.idem: dict[tuple[str, str], dict] = {}
        self._lock = asyncio.Lock()
        self.fail_on_write: Exception | None = None
        self.audit_fails = False
        self.healthy = True
        self.closed = False

    @property
    def audit_log(self) -> list[AuditEntry]:
        return self.data.audit

    @asynccontextmanager
    async def read(self):
        yield FakeUnitOfWork(self.data, writable=False)

    @asynccontextmanager
    async def write(self, key_id):
        async with self._lock:  # like the per-caller advisory lock, but global
            snapshot = self.data.snapshot()
            try:
                if self.fail_on_write is not None:
                    raise self.fail_on_write
                yield FakeUnitOfWork(self.data, writable=True)
            except BaseException:
                self.data = snapshot  # rollback: nothing written inside the transaction survives
                raise

    async def save_validation(self, v):
        self.data.validations[v.id] = v

    async def claim_idempotency(self, key_id, idem_key, request_hash, now):
        row = self.idem.get((key_id, idem_key))
        if row is None:
            self.idem[(key_id, idem_key)] = {"hash": request_hash, "state": "in_progress", "created_at": now}
            return IdemClaim("new")
        if row["hash"] != request_hash:
            return IdemClaim("mismatch")
        if row["state"] == "done":
            return IdemClaim("replay", row["status"], copy.deepcopy(row["body"]))
        if row["state"] == "in_progress" and now - row["created_at"] < STUCK_AFTER:
            return IdemClaim("in_progress")
        row.update(state="in_progress", created_at=now)
        return IdemClaim("new")

    async def finish_idempotency(self, key_id, idem_key, status_code, body):
        self.idem[(key_id, idem_key)].update(state="done", status=status_code, body=copy.deepcopy(body))

    async def fail_idempotency(self, key_id, idem_key):
        self.idem[(key_id, idem_key)]["state"] = "failed"

    async def audit(self, entry):
        if self.audit_fails:
            raise RuntimeError("audit unavailable")
        self.data.audit.append(entry)

    async def ping(self):
        return self.healthy

    async def close(self):
        self.closed = True


__all__ = ["FakeRepository", "QueryTimeout"]
