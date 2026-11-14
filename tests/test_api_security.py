"""Request authentication, limits, health endpoints and app lifecycle."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app import signing
from app.main import SECURITY_HEADERS, create_app
from app.stores import MemoryNonceStore, MemoryRateLimiter
from client import AsyncWriteClient, WriteError
from tests.conftest import KEY_ID, SECRET
from tests.fakes import FakeRepository


def signed(method, path, body=b"", **overrides):
    headers = signing.signed_headers(KEY_ID, SECRET, method, path, body)
    headers.update(overrides)
    return headers


async def test_health_and_headers(raw):
    r = await raw.get("/healthz")
    assert r.json() == {"status": "ok"}
    assert all(r.headers[k] == v for k, v in SECURITY_HEADERS.items())
    ready = (await raw.get("/readyz")).json()
    assert ready == {"status": "ready", "database": True, "store": True, "breaker": "closed"}


async def test_readyz_503_when_database_down(raw, repo):
    repo.healthy = False
    assert (await raw.get("/readyz")).status_code == 503


async def test_docs_off_by_default(raw):
    assert (await raw.get("/docs")).status_code == 404


async def test_unsigned_is_401(raw):
    r = await raw.post("/v1/validate/create_invoice", json={})
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


async def test_tampered_body_is_401(raw):
    path = "/v1/validate/create_invoice"
    r = await raw.post(path, content=b'{"params":{"customer_id":2}}', headers=signed("POST", path, b'{"params":{}}'))
    assert r.status_code == 401


async def test_stale_is_401(raw):
    headers = signing.signed_headers(KEY_ID, SECRET, "GET", "/v1/actions", b"", now=time.time() - 3_600)
    r = await raw.get("/v1/actions", headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "stale_request"


async def test_replay_is_401(raw):
    headers = signed("GET", "/v1/actions")
    assert (await raw.get("/v1/actions", headers=headers)).status_code == 200
    assert (await raw.get("/v1/actions", headers=headers)).json()["error"] == "replayed_request"


async def test_outside_network_is_403(app):
    t = httpx.ASGITransport(app=app, client=("192.0.2.9", 5000))
    async with AsyncWriteClient("http://w", KEY_ID, SECRET, transport=t) as c:
        with pytest.raises(WriteError) as err:
            await c.actions()
    assert err.value.status == 403


async def test_trusted_proxy_uses_rightmost_forwarded_address(settings, repo):
    settings.trust_proxy_headers, settings.allowed_networks = True, ["198.51.100.0/24"]
    app = create_app(settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w") as raw:
            ok = signed("GET", "/v1/actions", **{"X-Forwarded-For": "203.0.113.1, 198.51.100.4"})
            assert (await raw.get("/v1/actions", headers=ok)).status_code == 200
            bad = signed("GET", "/v1/actions", **{"X-Forwarded-For": "junk"})
            assert (await raw.get("/v1/actions", headers=bad)).status_code == 403


async def test_rate_limited_is_429(settings, repo):
    settings.rate_limit_per_minute = 1
    app = create_app(settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
    async with app.router.lifespan_context(app):
        async with AsyncWriteClient("http://w", KEY_ID, SECRET, transport=httpx.ASGITransport(app=app)) as c:
            await c.actions()
            with pytest.raises(WriteError) as err:
                await c.actions()
    assert err.value.status == 429


async def test_body_too_large_is_413(raw, settings):
    path = "/v1/validate/create_invoice"
    body = json.dumps({"params": {"x": "y" * settings.max_body_bytes}}).encode()
    r = await raw.post(path, content=body, headers=signed("POST", path, body))
    assert r.status_code == 413


async def test_chunked_body_too_large_is_413(raw, settings):
    path = "/v1/validate/create_invoice"
    body = b" " * (settings.max_body_bytes + 1)

    async def chunks():
        yield body[:10]
        yield body[10:]

    r = await raw.post(path, content=chunks(), headers=signed("POST", path, body))
    assert r.status_code == 413


async def test_missing_database_url_fails_fast(settings):
    app = create_app(settings)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        async with app.router.lifespan_context(app):
            pass


async def test_defaults_to_memory_stores_and_closes_repo(settings):
    repo = FakeRepository()
    settings.client_keys = {}
    app = create_app(settings, repo=repo)
    async with app.router.lifespan_context(app):
        assert type(app.state.writeapi.breaker).__name__ == "MemoryBreaker"
    assert repo.closed
