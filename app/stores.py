"""Shared state that must work across many API instances: nonces, rate limits, the circuit breaker.

Redis versions are used when REDIS_URL is set. In-memory versions are for tests and one instance.
"""

from __future__ import annotations

import time
from typing import Protocol


class NonceStore(Protocol):
    async def claim(self, key: str, ttl_seconds: int) -> bool: ...
    async def ping(self) -> bool: ...


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> bool: ...


class Breaker(Protocol):
    async def allow(self) -> bool:
        """False while open. After the cooldown, lets exactly one trial request through."""

    async def record_success(self) -> None: ...
    async def record_failure(self) -> None: ...
    async def state(self) -> str: ...


# --- in-memory ----------------------------------------------------------------------------------


class MemoryNonceStore:
    def __init__(self, clock=time.monotonic):
        self._seen: dict[str, float] = {}
        self._clock = clock

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        now = self._clock()
        if len(self._seen) > 10_000:
            self._seen = {k: exp for k, exp in self._seen.items() if exp > now}
        if self._seen.get(key, 0) > now:
            return False
        self._seen[key] = now + ttl_seconds
        return True

    async def ping(self) -> bool:
        return True


class MemoryRateLimiter:
    def __init__(self, clock=time.monotonic):
        self._windows: dict[str, tuple[int, int]] = {}
        self._clock = clock

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        window = int(self._clock() // window_seconds)
        start, count = self._windows.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        self._windows[key] = (start, count + 1)
        return count + 1 <= limit


class MemoryBreaker:
    def __init__(self, threshold: int, window_seconds: int, cooldown_seconds: int, clock=time.monotonic):
        self.threshold, self.window, self.cooldown = threshold, window_seconds, cooldown_seconds
        self._clock = clock
        self._failures: list[float] = []
        self._open_until: float | None = None
        self._trial_running = False

    async def allow(self) -> bool:
        now = self._clock()
        if self._open_until is None:
            return True
        if now < self._open_until or self._trial_running:
            return False
        self._trial_running = True  # half-open: one trial
        return True

    async def record_success(self) -> None:
        if self._trial_running:
            self._open_until, self._trial_running, self._failures = None, False, []

    async def record_failure(self) -> None:
        now = self._clock()
        if self._trial_running:
            self._trial_running = False
            self._open_until = now + self.cooldown
            return
        self._failures = [t for t in self._failures if t > now - self.window] + [now]
        if len(self._failures) >= self.threshold:
            self._open_until = now + self.cooldown
            self._failures = []

    async def state(self) -> str:
        if self._open_until is None:
            return "closed"
        if self._clock() < self._open_until:
            return "open"
        return "half_open"


# --- Redis --------------------------------------------------------------------------------------


class RedisNonceStore:
    def __init__(self, redis):
        self._redis = redis

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        return bool(await self._redis.set(f"wapi:nonce:{key}", "1", nx=True, ex=ttl_seconds))

    async def ping(self) -> bool:
        return bool(await self._redis.ping())


class RedisRateLimiter:
    def __init__(self, redis):
        self._redis = redis

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        redis_key = f"wapi:rate:{key}:{int(time.time() // window_seconds)}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds + 1, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit


class RedisBreaker:
    """One breaker shared by every instance.

    wapi:breaker:open   exists while open (TTL = cooldown); `tripped` records that it was opened
    wapi:breaker:fails  failures in the current window (TTL = window)
    wapi:breaker:trial  set NX by the single request allowed through after the cooldown
    """

    def __init__(self, redis, threshold: int, window_seconds: int, cooldown_seconds: int):
        self._r = redis
        self.threshold, self.window, self.cooldown = threshold, window_seconds, cooldown_seconds

    async def allow(self) -> bool:
        if await self._r.exists("wapi:breaker:open"):
            return False
        if not await self._r.exists("wapi:breaker:tripped"):
            return True
        # cooldown over: half-open, one trial at a time
        return bool(await self._r.set("wapi:breaker:trial", "1", nx=True, ex=self.cooldown))

    async def record_success(self) -> None:
        # Only a successful trial closes the breaker; while closed, failures keep counting in their window.
        if await self._r.exists("wapi:breaker:tripped"):
            await self._r.delete("wapi:breaker:tripped", "wapi:breaker:trial", "wapi:breaker:fails")

    async def record_failure(self) -> None:
        if await self._r.exists("wapi:breaker:tripped"):
            await self._trip()
            return
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.incr("wapi:breaker:fails")
            pipe.expire("wapi:breaker:fails", self.window, nx=True)
            count, _ = await pipe.execute()
        if int(count) >= self.threshold:
            await self._trip()

    async def _trip(self) -> None:
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.set("wapi:breaker:open", "1", ex=self.cooldown)
            pipe.set("wapi:breaker:tripped", "1")
            pipe.delete("wapi:breaker:trial", "wapi:breaker:fails")
            await pipe.execute()

    async def state(self) -> str:
        if await self._r.exists("wapi:breaker:open"):
            return "open"
        return "half_open" if await self._r.exists("wapi:breaker:tripped") else "closed"
