"""Small load test: N concurrent validate + commit pairs, then throughput and latency.

Against a running stack (docker compose up):
    python scripts/loadtest.py --writes 500 --concurrency 10

Write-path overhead only (no database; in-memory repository, in this process):
    python scripts/loadtest.py --in-process --writes 2000 --concurrency 20
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from client import AsyncWriteClient, WriteError  # noqa: E402


async def _worker(api, jobs: asyncio.Queue, latencies: list[float], errors: list[str], issued_on: str) -> None:
    params = {"customer_id": 1, "issued_on": issued_on, "due_in_days": 30, "lines": [{"product_id": 1, "quantity": 2}]}
    while True:
        try:
            jobs.get_nowait()
        except asyncio.QueueEmpty:
            return
        start = time.perf_counter()
        try:
            check = await api.validate("create_invoice", params)
            await api.commit(check["token"])
            latencies.append(time.perf_counter() - start)
        except WriteError as err:
            errors.append(f"{err.status} {err.body.get('error')}")


async def run(args) -> int:
    jobs: asyncio.Queue = asyncio.Queue()
    for _ in range(args.writes):
        jobs.put_nowait(1)
    latencies: list[float] = []
    errors: list[str] = []
    lifespan = None

    if args.in_process:
        from app.config import Settings
        from app.main import create_app
        from app.stores import MemoryNonceStore, MemoryRateLimiter
        from tests.fakes import FakeRepository

        key_id, secret = "load-agent", "l" * 40
        settings = Settings(
            _env_file=None, client_keys={key_id: secret}, rate_limit_per_minute=100_000, daily_commit_cap=1_000_000
        )
        app = create_app(settings, repo=FakeRepository(), nonces=MemoryNonceStore(), limiter=MemoryRateLimiter())
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        url, kwargs = "http://writeapi.test", {"transport": httpx.ASGITransport(app=app)}
        issued_on = dt.datetime.now(dt.UTC).date().isoformat()
    else:
        key_id, secret = os.environ.get("AGENT_KEY_ID", ""), os.environ.get("AGENT_SECRET", "")
        url = os.environ.get("WRITEAPI_URL", "http://127.0.0.1:8001")
        kwargs = {"limits": httpx.Limits(max_connections=args.concurrency)}
        issued_on = dt.datetime.now(dt.UTC).date().isoformat()
        if not key_id or not secret:
            print("Set AGENT_KEY_ID and AGENT_SECRET", file=sys.stderr)
            return 2

    started = time.perf_counter()
    async with AsyncWriteClient(url, key_id, secret, **kwargs) as api:
        await asyncio.gather(*[_worker(api, jobs, latencies, errors, issued_on) for _ in range(args.concurrency)])
    elapsed = time.perf_counter() - started
    if lifespan is not None:
        await lifespan.__aexit__(None, None, None)

    if not latencies:
        print(f"all {len(errors)} writes failed: {sorted(set(errors))}")
        return 1
    q = statistics.quantiles(sorted(latencies), n=100)
    print(f"writes: {len(latencies)} ok, {len(errors)} failed {sorted(set(errors)) if errors else ''}")
    print(f"throughput: {len(latencies) / elapsed:,.0f} writes/s (validate + commit) over {elapsed:.1f}s")
    print(f"latency ms per write: p50 {q[49] * 1000:.1f}  p95 {q[94] * 1000:.1f}  p99 {q[98] * 1000:.1f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--writes", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--in-process", action="store_true", help="no network or database: write-path overhead only")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
