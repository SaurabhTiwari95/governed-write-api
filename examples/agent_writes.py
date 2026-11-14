"""What an AI agent does: validate a write, look at the preview, commit it, and retry safely.

    python examples/agent_writes.py

Reads WRITEAPI_URL, AGENT_KEY_ID and AGENT_SECRET from the environment (or .env).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import WriteClient, WriteError, new_idempotency_key  # noqa: E402


def load_dotenv(path: Path = Path(".env")) -> None:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def show(title: str, data) -> None:
    print(f"\n{title}\n" + json.dumps(data, indent=2))


def main() -> int:
    load_dotenv()
    url = os.environ.get("WRITEAPI_URL", "http://127.0.0.1:8001")
    key_id, secret = os.environ.get("AGENT_KEY_ID"), os.environ.get("AGENT_SECRET")
    if not key_id or not secret:
        print("Set AGENT_KEY_ID and AGENT_SECRET (see .env.example)", file=sys.stderr)
        return 2
    today = dt.datetime.now(dt.UTC).date().isoformat()  # the server judges "today" in its TIMEZONE (UTC by default)

    with WriteClient(url, key_id, secret) as api:
        try:
            # 1. Validate: nothing is written; the server prices the lines itself.
            check = api.validate(
                "create_invoice",
                {
                    "customer_id": 1,
                    "issued_on": today,
                    "due_in_days": 30,
                    "lines": [{"product_id": 1, "quantity": 3}, {"product_id": 2, "quantity": 5}],
                },
            )
            show("1. Preview of the invoice (nothing written yet):", check["preview"])

            # 2. Commit with an idempotency key, then "retry" as if the response was lost.
            key = new_idempotency_key()
            done = api.commit(check["token"], idempotency_key=key)
            again = api.commit(check["token"], idempotency_key=key)
            show("2. Committed:", done["result"])
            print(f"   retry with the same key replayed: {again['_replayed']} (same invoice, nothing written twice)")

            # 3. A rule the server enforces: no overpayment.
            invoice_id = done["result"]["invoice_id"]
            try:
                api.validate("record_payment", {"invoice_id": invoice_id, "amount": "99999999", "paid_on": today})
            except WriteError as err:
                show("3. Overpayment refused at validation:", err.body["details"])

            # 4. Pay it in full; the invoice is marked paid in the same transaction.
            pay = api.validate(
                "record_payment", {"invoice_id": invoice_id, "amount": done["result"]["total"], "paid_on": today}
            )
            show("4. Payment preview:", pay["preview"])
            show("   Payment committed:", api.commit(pay["token"])["result"])

            # 5. Tokens are single-use.
            try:
                api.commit(pay["token"])
            except WriteError as err:
                print(f"\n5. Reusing a token: {err.status} {err.body['error']}")
        except WriteError as err:
            print(f"Write API refused: {err.status} {json.dumps(err.body)}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
