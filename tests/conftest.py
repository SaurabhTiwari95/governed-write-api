from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.stores import MemoryBreaker, MemoryNonceStore, MemoryRateLimiter
from client import AsyncWriteClient
from tests.fakes import FakeRepository

KEY_ID = "agent-1"
SECRET = "unit-test-secret-" + "w" * 32
NOW = dt.datetime(2026, 2, 1, 10, 30, tzinfo=dt.UTC)  # a Sunday mid-morning, inside default hours


class Clock:
    def __init__(self, now: dt.datetime = NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += dt.timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="",
        redis_url="",
        client_keys={KEY_ID: SECRET},
        allowed_networks=["127.0.0.1/32"],
        rate_limit_per_minute=10_000,
        breaker_failure_threshold=3,
        breaker_window_seconds=60,
        breaker_cooldown_seconds=30,
    )


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def breaker(settings, clock) -> MemoryBreaker:
    return MemoryBreaker(
        settings.breaker_failure_threshold,
        settings.breaker_window_seconds,
        settings.breaker_cooldown_seconds,
        clock=lambda: clock().timestamp(),
    )


@pytest.fixture
async def app(settings, repo, breaker, clock):
    application = create_app(
        settings, repo=repo, nonces=MemoryNonceStore(), limiter=MemoryRateLimiter(), breaker=breaker, clock=clock
    )
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def service(app):
    return app.state.writeapi.service


@pytest.fixture
async def api(app):
    transport = httpx.ASGITransport(app=app)
    async with AsyncWriteClient("http://writeapi.test", KEY_ID, SECRET, transport=transport) as c:
        yield c


@pytest.fixture
async def raw(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://writeapi.test") as c:
        yield c
