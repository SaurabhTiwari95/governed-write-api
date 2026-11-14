"""HTTP API. Run with:  uvicorn app.main:create_app --factory

Endpoints
  GET  /healthz                 liveness
  GET  /readyz                  readiness: database and shared store reachable; breaker state
  GET  /v1/actions              signed: the write actions available
  POST /v1/validate/{action}    signed: check a write, get a preview and a single-use token
  POST /v1/commit               signed, needs Idempotency-Key: perform a validated write
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import __version__, signing
from app.actions import ACTIONS
from app.config import Settings
from app.repository import PostgresRepository, Repository
from app.service import ServiceError, WriteService
from app.stores import (
    Breaker,
    MemoryBreaker,
    MemoryNonceStore,
    MemoryRateLimiter,
    NonceStore,
    RateLimiter,
    RedisBreaker,
    RedisNonceStore,
    RedisRateLimiter,
)

log = logging.getLogger("writeapi")

NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidateBody(_Body):
    params: dict[str, Any] = Field(default_factory=dict)


class CommitBody(_Body):
    token: str = Field(min_length=20, max_length=200)


@dataclass(frozen=True)
class Caller:
    key_id: str
    ip: str
    body: bytes


@dataclass
class State:
    settings: Settings
    networks: list
    repo: Repository
    nonces: NonceStore
    limiter: RateLimiter
    breaker: Breaker
    service: WriteService


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()  # the entry our own proxy added
    return request.client.host if request.client else ""


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise ServiceError(413, "body_too_large", store=False)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ServiceError(413, "body_too_large", store=False)
        chunks.append(chunk)
    return b"".join(chunks)


async def authenticate(request: Request) -> Caller:
    """IP allow-list → signature → freshness → single-use nonce → rate limit."""
    st: State = request.app.state.writeapi
    s = st.settings
    try:
        address = ipaddress.ip_address(_client_ip(request, s))
    except ValueError:
        raise ServiceError(403, "forbidden", store=False) from None
    if not any(address in net for net in st.networks):
        raise ServiceError(403, "forbidden", store=False)

    h = request.headers
    key_id = h.get(signing.HEADER_KEY_ID, "")
    timestamp = h.get(signing.HEADER_TIMESTAMP, "")
    nonce = h.get(signing.HEADER_NONCE, "")
    signature = h.get(signing.HEADER_SIGNATURE, "")
    secret = s.client_keys.get(key_id)
    if not (secret and timestamp.isdigit() and NONCE_PATTERN.match(nonce) and signature):
        raise ServiceError(401, "unauthorized", store=False)
    if abs(time.time() - int(timestamp)) > s.max_clock_skew_seconds:
        raise ServiceError(401, "stale_request", store=False)
    body = await _read_body(request, s.max_body_bytes)
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not signing.verify(secret, signature, request.method, path, timestamp, nonce, body):
        raise ServiceError(401, "unauthorized", store=False)
    if not await st.nonces.claim(f"{key_id}:{nonce}", 2 * s.max_clock_skew_seconds):
        raise ServiceError(401, "replayed_request", store=False)
    if not await st.limiter.hit(key_id, s.rate_limit_per_minute, 60):
        raise ServiceError(429, "rate_limited", store=False, headers={"Retry-After": "60"})
    return Caller(key_id=key_id, ip=str(address), body=body)


def _parse(model: type[_Body], body: bytes) -> Any:
    try:
        return model.model_validate_json(body or b"{}")
    except ValidationError as exc:
        details = [{"field": ".".join(map(str, e["loc"])) or "body", "error": e["msg"]} for e in exc.errors()]
        raise ServiceError(422, "invalid_request", details) from None


def create_app(
    settings: Settings | None = None,
    repo: Repository | None = None,
    nonces: NonceStore | None = None,
    limiter: RateLimiter | None = None,
    breaker: Breaker | None = None,
    clock=None,
) -> FastAPI:
    settings = settings or Settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis_client = None
        r = repo
        if r is None:
            if not settings.database_url:
                raise RuntimeError("DATABASE_URL is not set")
            r = await PostgresRepository.create(
                settings.database_url, settings.db_pool_min, settings.db_pool_max, settings.statement_timeout_ms
            )
        n, lim, brk = nonces, limiter, breaker
        b_args = (
            settings.breaker_failure_threshold,
            settings.breaker_window_seconds,
            settings.breaker_cooldown_seconds,
        )
        if None in (n, lim, brk):
            if settings.redis_url:
                from redis.asyncio import Redis

                redis_client = Redis.from_url(settings.redis_url)
                n = n or RedisNonceStore(redis_client)
                lim = lim or RedisRateLimiter(redis_client)
                brk = brk or RedisBreaker(redis_client, *b_args)
            else:
                log.warning("REDIS_URL not set: in-memory nonces, rate limits and breaker; run ONE instance only")
                n, lim, brk = n or MemoryNonceStore(), lim or MemoryRateLimiter(), brk or MemoryBreaker(*b_args)
        if not settings.client_keys:
            log.warning("CLIENT_KEYS is empty: every signed request will be refused")
        service = WriteService(settings, r, brk, clock) if clock else WriteService(settings, r, brk)
        app.state.writeapi = State(settings, settings.networks(), r, n, lim, brk, service)
        try:
            yield
        finally:
            await r.close()
            if redis_client is not None:
                await redis_client.aclose()

    app = FastAPI(
        title="governed-write-api",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        rid = getattr(request.state, "request_id", "")
        if exc.status in (401, 403, 413, 429, 503):
            log.warning("refused %s %s: %s rid=%s", request.method, request.url.path, exc.code, rid)
        return JSONResponse(exc.body(rid), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):  # pragma: no cover - last resort
        rid = getattr(request.state, "request_id", "")
        log.exception("unhandled error rid=%s", rid)
        return JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        st: State = request.app.state.writeapi
        db_ok = await st.repo.ping()
        try:
            store_ok = await st.nonces.ping()
            breaker_state = await st.breaker.state()
        except Exception:
            store_ok, breaker_state = False, "unknown"
        ready = db_ok and store_ok
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "database": db_ok,
                "store": store_ok,
                "breaker": breaker_state,
            },
            status_code=200 if ready else 503,
        )

    @app.get("/v1/actions")
    async def list_actions(caller: Caller = Depends(authenticate)):
        return {
            "actions": [
                {"name": a.name, "description": a.description, "params": a.params_model.model_json_schema()}
                for a in ACTIONS.values()
            ]
        }

    @app.post("/v1/validate/{action}")
    async def validate(action: str, request: Request, caller: Caller = Depends(authenticate)):
        body = _parse(ValidateBody, caller.body)
        st: State = request.app.state.writeapi
        result = await st.service.validate(caller.key_id, action, body.params, request.state.request_id)
        return JSONResponse(result)

    @app.post("/v1/commit")
    async def commit(request: Request, caller: Caller = Depends(authenticate)):
        idem_key = request.headers.get("Idempotency-Key", "")
        if not IDEMPOTENCY_PATTERN.match(idem_key):
            raise ServiceError(400, "idempotency_key_required", {"format": "8-128 of A-Z a-z 0-9 _ -"}, store=False)
        body = _parse(CommitBody, caller.body)
        st: State = request.app.state.writeapi
        status, payload, replayed = await st.service.commit(
            caller.key_id, idem_key, caller.body, body.token, request.state.request_id
        )
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return JSONResponse(payload, status_code=status, headers=headers)

    return app
