"""The write actions an agent may request, and the business rules each must pass.

Each action has three parts:
  parse()  typed, bounded parameters (Pydantic); unknown fields refused
  check()  business rules against current data; returns a preview of exactly what will be written
  apply()  writes it (only ever called inside the commit transaction, after check() ran again)

The agent never supplies prices or totals: they're computed here from the database.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from app.config import Settings

CENT = Decimal("0.01")
MAX_BACKDATE_DAYS = 90


class ParamError(ValueError):
    def __init__(self, errors: list[dict[str, str]]):
        super().__init__("invalid parameters")
        self.errors = errors


class RuleViolation(Exception):
    def __init__(self, violations: list[dict[str, str]]):
        super().__init__(", ".join(v["rule"] for v in violations))
        self.violations = violations


@dataclass(frozen=True)
class InvoiceState:
    id: int
    customer_id: int
    status: str
    issued_on: dt.date
    total: Decimal
    paid: Decimal


class ReadUnitOfWork(Protocol):
    async def customer_exists(self, customer_id: int) -> bool: ...
    async def product_prices(self, product_ids: list[int]) -> dict[int, Decimal]: ...
    async def invoice_state(self, invoice_id: int, lock: bool = False) -> InvoiceState | None: ...


class WriteUnitOfWork(ReadUnitOfWork, Protocol):
    async def insert_invoice(
        self, customer_id: int, issued_on: dt.date, due_on: dt.date, total: Decimal, lines: list[tuple]
    ) -> int: ...
    async def insert_payment(self, invoice_id: int, paid_on: dt.date, amount: Decimal) -> int: ...
    async def set_invoice_status(self, invoice_id: int, status: str) -> None: ...


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineIn(_Params):
    product_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=10_000)


class CreateInvoiceParams(_Params):
    customer_id: int = Field(ge=1)
    issued_on: dt.date
    due_in_days: int = Field(ge=0, le=180)
    lines: list[LineIn] = Field(min_length=1, max_length=500)


class RecordPaymentParams(_Params):
    invoice_id: int = Field(ge=1)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    paid_on: dt.date


class Action:
    name: str
    description: str
    params_model: type[_Params]

    def parse(self, raw: dict[str, Any]) -> Any:
        try:
            return self.params_model.model_validate(raw)
        except ValidationError as exc:
            raise ParamError(
                [{"param": ".".join(map(str, e["loc"])) or "params", "error": e["msg"]} for e in exc.errors()]
            ) from None

    async def check(self, uow, params, settings: Settings, today: dt.date, lock: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    async def apply(self, uow, params, preview: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class CreateInvoice(Action):
    name = "create_invoice"
    description = "Raise an open invoice for a customer. Prices come from the product list."
    params_model = CreateInvoiceParams

    async def check(self, uow, params: CreateInvoiceParams, settings, today, lock=False):
        problems: list[dict[str, str]] = []
        ids = [line.product_id for line in params.lines]
        if len(ids) != len(set(ids)):
            problems.append({"rule": "duplicate_product", "message": "each product may appear on one line only"})
        if len(ids) > settings.max_invoice_lines:
            problems.append({"rule": "too_many_lines", "message": f"at most {settings.max_invoice_lines} lines"})
        if params.issued_on > today:
            problems.append({"rule": "issued_in_future", "message": "issued_on can't be after today"})
        if params.issued_on < today - dt.timedelta(days=MAX_BACKDATE_DAYS):
            problems.append(
                {"rule": "issued_too_old", "message": f"issued_on can't be over {MAX_BACKDATE_DAYS} days ago"}
            )
        if not await uow.customer_exists(params.customer_id):
            problems.append({"rule": "unknown_customer", "message": "no such customer"})
        prices = await uow.product_prices(sorted(set(ids)))
        missing = sorted(set(ids) - set(prices))
        if missing:
            problems.append({"rule": "unknown_product", "message": f"unknown product ids: {missing}"})
        if problems:
            raise RuleViolation(problems)

        lines = []
        total = Decimal("0")
        for line in params.lines:
            line_total = (prices[line.product_id] * line.quantity).quantize(CENT)
            total += line_total
            lines.append(
                {
                    "product_id": line.product_id,
                    "quantity": line.quantity,
                    "unit_price": _money(prices[line.product_id]),
                    "line_total": _money(line_total),
                }
            )
        if total > settings.max_invoice_total:
            raise RuleViolation(
                [{"rule": "amount_over_limit", "message": f"total exceeds {_money(settings.max_invoice_total)}"}]
            )
        return {
            "customer_id": params.customer_id,
            "issued_on": params.issued_on.isoformat(),
            "due_on": (params.issued_on + dt.timedelta(days=params.due_in_days)).isoformat(),
            "lines": lines,
            "total": _money(total),
        }

    async def apply(self, uow, params: CreateInvoiceParams, preview):
        invoice_id = await uow.insert_invoice(
            params.customer_id,
            dt.date.fromisoformat(preview["issued_on"]),
            dt.date.fromisoformat(preview["due_on"]),
            Decimal(preview["total"]),
            [(ln["product_id"], ln["quantity"], Decimal(ln["unit_price"])) for ln in preview["lines"]],
        )
        return {"invoice_id": invoice_id, "total": preview["total"], "status": "open"}


class RecordPayment(Action):
    name = "record_payment"
    description = "Record a payment against an open invoice; marks it paid when settled."
    params_model = RecordPaymentParams

    async def check(self, uow, params: RecordPaymentParams, settings, today, lock=False):
        invoice = await uow.invoice_state(params.invoice_id, lock=lock)
        if invoice is None:
            raise RuleViolation([{"rule": "unknown_invoice", "message": "no such invoice"}])
        problems: list[dict[str, str]] = []
        if invoice.status != "open":
            problems.append({"rule": "invoice_not_open", "message": f"invoice is {invoice.status}"})
        outstanding = invoice.total - invoice.paid
        if params.amount > outstanding:
            problems.append({"rule": "overpayment", "message": f"outstanding is {_money(outstanding)}"})
        if params.paid_on > today:
            problems.append({"rule": "paid_in_future", "message": "paid_on can't be after today"})
        if params.paid_on < invoice.issued_on:
            problems.append({"rule": "paid_before_issue", "message": "paid_on is before the invoice date"})
        if problems:
            raise RuleViolation(problems)
        after = outstanding - params.amount
        return {
            "invoice_id": invoice.id,
            "amount": _money(params.amount),
            "paid_on": params.paid_on.isoformat(),
            "outstanding_before": _money(outstanding),
            "outstanding_after": _money(after),
            "settles_invoice": after == 0,
        }

    async def apply(self, uow, params: RecordPaymentParams, preview):
        payment_id = await uow.insert_payment(params.invoice_id, params.paid_on, params.amount)
        status = "open"
        if preview["settles_invoice"]:
            await uow.set_invoice_status(params.invoice_id, "paid")
            status = "paid"
        return {"payment_id": payment_id, "invoice_id": params.invoice_id, "invoice_status": status}


ACTIONS: dict[str, Action] = {a.name: a for a in (CreateInvoice(), RecordPayment())}
