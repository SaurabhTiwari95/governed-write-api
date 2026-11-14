"""Config, circuit breaker, stores and the sync client."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from pydantic import ValidationError

from app import signing
from app.config import Settings
from app.stores import MemoryBreaker, MemoryNonceStore, MemoryRateLimiter
from client import WriteClient, WriteError

# --- config -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_keys": {"a": "short"}},
        {"client_keys": {"a": "replace-with-something-long-enough-to-pass-length"}},
        {"client_keys": {"": "x" * 40}},
        {"allowed_networks": ["nope"]},
        {"allowed_hours": "9-17"},
        {"allowed_hours": "25:00-26:00"},
        {"timezone": "Mars/Olympus"},
    ],
)
def test_settings_reject_bad_values(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "hours, local_time, inside",
    [
        ("00:00-24:00", "03:00", True),
        ("09:00-17:00", "08:59", False),
        ("09:00-17:00", "09:00", True),
        ("09:00-17:00", "17:00", False),
        ("22:00-06:00", "23:30", True),
        ("22:00-06:00", "05:59", True),
        ("22:00-06:00", "12:00", False),
        ("10:00-10:00", "10:00", False),
    ],
)
def test_allowed_hours(hours, local_time, inside):
    s = Settings(_env_file=None, allowed_hours=hours, timezone="Asia/Kolkata")
    hh, mm = map(int, local_time.split(":"))
    moment = dt.datetime(2026, 3, 1, hh, mm, tzinfo=s.tz())
    assert s.within_hours(moment) is inside


def test_start_of_day_uses_the_configured_timezone():
    s = Settings(_env_file=None, timezone="Asia/Kolkata")
    utc = dt.datetime(2026, 3, 1, 20, 0, tzinfo=dt.UTC)  # already 2 March 01:30 in IST
    assert s.start_of_day(utc).isoformat() == "2026-03-02T00:00:00+05:30"


# --- circuit breaker ----------------------------------------------------------------------------


async def test_breaker_lifecycle():
    now = [0.0]
    b = MemoryBreaker(threshold=2, window_seconds=10, cooldown_seconds=30, clock=lambda: now[0])
    assert await b.allow() and await b.state() == "closed"
    await b.record_failure()
    now[0] = 11  # first failure fell out of the window
    await b.record_failure()
    assert await b.state() == "closed"
    await b.record_failure()
    assert await b.state() == "open" and not await b.allow()
    now[0] = 45
    assert await b.state() == "half_open"
    assert await b.allow() is True  # one trial
    assert await b.allow() is False  # not two
    await b.record_failure()  # trial failed: open again
    assert await b.state() == "open"
    now[0] = 80
    assert await b.allow()
    await b.record_success()
    assert await b.state() == "closed" and await b.allow()


async def test_success_while_closed_does_not_forget_failures():
    b = MemoryBreaker(threshold=2, window_seconds=60, cooldown_seconds=30, clock=lambda: 0.0)
    await b.record_failure()
    await b.record_success()
    await b.record_failure()
    assert await b.state() == "open"


# --- stores -------------------------------------------------------------------------------------


async def test_nonce_store_and_rate_limiter():
    now = [0.0]
    nonces = MemoryNonceStore(clock=lambda: now[0])
    assert await nonces.claim("n", 5) and not await nonces.claim("n", 5)
    now[0] = 6
    assert await nonces.claim("n", 5) and await nonces.ping()
    for i in range(10_001):
        await nonces.claim(f"k{i}", 1)
    now[0] = 100
    await nonces.claim("fresh", 1)
    assert len(nonces._seen) == 1

    limiter = MemoryRateLimiter(clock=lambda: now[0])
    assert [await limiter.hit("a", 1, 60) for _ in range(2)] == [True, False]


# --- sync client --------------------------------------------------------------------------------


def test_sync_client_signs_and_sends_idempotency_keys():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        h = request.headers
        assert signing.verify(
            "c" * 40,
            h["X-Signature"],
            request.method,
            request.url.path,
            h["X-Timestamp"],
            h["X-Nonce"],
            request.content,
        )
        if request.url.path == "/v1/actions":
            return httpx.Response(200, json={"actions": []})
        if request.url.path.startswith("/v1/validate"):
            return httpx.Response(200, json={"token": "t" * 43, "preview": json.loads(request.content)["params"]})
        if h.get("Idempotency-Key") == "dup-key-001":
            return httpx.Response(201, json={"result": {}}, headers={"Idempotent-Replayed": "true"})
        return httpx.Response(410, json={"error": "token_expired"})

    with WriteClient("http://w", "agent", "c" * 40, transport=httpx.MockTransport(handler)) as api:
        assert api.actions() == []
        check = api.validate("record_payment", {"invoice_id": 1, "paid_on": dt.date(2026, 1, 2)})
        assert check["preview"] == {"invoice_id": 1, "paid_on": "2026-01-02"}
        assert api.commit(check["token"], idempotency_key="dup-key-001")["_replayed"] is True
        with pytest.raises(WriteError) as err:
            api.commit(check["token"])
        assert err.value.status == 410
    assert len(seen[-1].headers["Idempotency-Key"]) == 32  # generated when not given


def test_client_non_json_error():
    t = httpx.MockTransport(lambda r: httpx.Response(502, text="bad gateway"))
    with WriteClient("http://w", "agent", "c" * 40, transport=t) as api, pytest.raises(WriteError) as err:
        api.actions()
    assert err.value.body == {"error": "non_json_response"}
