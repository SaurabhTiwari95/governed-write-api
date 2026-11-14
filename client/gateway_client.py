"""Client an AI agent uses for governed writes. Signs every request.

from client import WriteClient
with WriteClient("http://127.0.0.1:8001", key_id="demo-agent", secret=...) as api:
    check = api.validate("record_payment", {"invoice_id": 12, "amount": "500.00", "paid_on": "2026-11-09"})
    print(check["preview"])          # show a person, or decide
    done = api.commit(check["token"])  # retries safely with the same idempotency key
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from app.signing import signed_headers


class WriteError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        super().__init__(f"{status} {body.get('error', 'error')}")
        self.status, self.body = status, body


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), default=str).encode()


def _result(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {"error": "non_json_response"}
    if response.status_code >= 400:
        raise WriteError(response.status_code, body)
    body["_replayed"] = response.headers.get("Idempotent-Replayed") == "true"
    return body


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


class _Base:
    def __init__(self, key_id: str, secret: str):
        self._key_id, self._secret = key_id, secret

    def _headers(self, method: str, path: str, body: bytes, idem_key: str | None) -> dict[str, str]:
        headers = signed_headers(self._key_id, self._secret, method, path, body)
        headers["Content-Type"] = "application/json"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        return headers


class WriteClient(_Base):
    def __init__(self, base_url: str, key_id: str, secret: str, timeout: float = 10.0, **client_kwargs):
        super().__init__(key_id, secret)
        self._http = httpx.Client(base_url=base_url, timeout=timeout, **client_kwargs)

    def _send(self, method: str, path: str, body: bytes = b"", idem_key: str | None = None) -> dict[str, Any]:
        return _result(
            self._http.request(method, path, content=body, headers=self._headers(method, path, body, idem_key))
        )

    def actions(self) -> list[dict[str, Any]]:
        return self._send("GET", "/v1/actions")["actions"]

    def validate(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._send("POST", f"/v1/validate/{action}", _encode({"params": params}))

    def commit(self, token: str, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._send("POST", "/v1/commit", _encode({"token": token}), idempotency_key or new_idempotency_key())

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class AsyncWriteClient(_Base):
    def __init__(self, base_url: str, key_id: str, secret: str, timeout: float = 10.0, **client_kwargs):
        super().__init__(key_id, secret)
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, **client_kwargs)

    async def _send(self, method: str, path: str, body: bytes = b"", idem_key: str | None = None) -> dict[str, Any]:
        response = await self._http.request(
            method, path, content=body, headers=self._headers(method, path, body, idem_key)
        )
        return _result(response)

    async def actions(self) -> list[dict[str, Any]]:
        return (await self._send("GET", "/v1/actions"))["actions"]

    async def validate(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._send("POST", f"/v1/validate/{action}", _encode({"params": params}))

    async def commit(self, token: str, idempotency_key: str | None = None) -> dict[str, Any]:
        return await self._send(
            "POST", "/v1/commit", _encode({"token": token}), idempotency_key or new_idempotency_key()
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
