"""The governed write flow, independent of HTTP.

validate(): parse → check rules (read-only) → store a short-lived, single-use token with the preview
commit():   idempotency claim → circuit breaker → allowed hours → [transaction: per-caller lock →
            daily cap → take token → re-check rules with row locks → preview unchanged? → write →
            mark token used → audit] → store the response under the idempotency key
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import uuid
from typing import Any

from app.actions import ACTIONS, ParamError, RuleViolation
from app.config import Settings
from app.repository import AuditEntry, QueryTimeout, Repository, Validation
from app.stores import Breaker

log = logging.getLogger("writeapi.service")


class ServiceError(Exception):
    """An outcome to return to the caller.

    `store` decides whether it's saved under the idempotency key. Final answers (rule violations,
    a bad or used token) are stored, so a retry gets the same answer. "Try later" answers (outside
    hours, daily cap, breaker open, server errors) are not, so a retry with the same key can succeed.
    """

    def __init__(
        self,
        status: int,
        code: str,
        details: Any = None,
        store: bool = True,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(code)
        self.status, self.code, self.details, self.store, self.headers = status, code, details, store, headers or {}

    def body(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "request_id": request_id}
        if self.details is not None:
            body["details"] = self.details
        return body


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class WriteService:
    def __init__(self, settings: Settings, repo: Repository, breaker: Breaker, clock=utc_now):
        self.settings, self.repo, self.breaker, self.clock = settings, repo, breaker, clock

    async def _audit(self, entry: AuditEntry) -> None:
        try:
            await self.repo.audit(entry)
        except Exception:
            log.exception("audit write failed rid=%s", entry.request_id)

    def _action(self, name: str):
        action = ACTIONS.get(name)
        if action is None:
            raise ServiceError(404, "unknown_action")
        return action

    # --- validate -------------------------------------------------------------------------------

    async def validate(self, key_id: str, action_name: str, raw_params: dict[str, Any], rid: str) -> dict[str, Any]:
        action = self._action(action_name)
        try:
            params = action.parse(raw_params)
        except ParamError as exc:
            await self._audit(AuditEntry(rid, key_id, action_name, "validate_invalid", 422))
            raise ServiceError(422, "invalid_params", exc.errors) from None

        now = self.clock()
        today = now.astimezone(self.settings.tz()).date()
        try:
            async with self.repo.read() as uow:
                preview = await action.check(uow, params, self.settings, today)
        except RuleViolation as exc:
            await self._audit(
                AuditEntry(rid, key_id, action_name, "validate_rejected", 422, detail={"rules": _rules(exc)})
            )
            raise ServiceError(422, "rule_violation", exc.violations) from None

        token = secrets.token_urlsafe(32)
        validation = Validation(
            id=str(uuid.uuid4()),
            token_hash=token_hash(token),
            key_id=key_id,
            action=action_name,
            params=params.model_dump(mode="json"),
            preview=preview,
            created_at=now,
            expires_at=now + dt.timedelta(seconds=self.settings.validation_ttl_seconds),
        )
        await self.repo.save_validation(validation)
        await self._audit(AuditEntry(rid, key_id, action_name, "validated", 200, validation.id))
        return {
            "action": action_name,
            "token": token,  # returned once; only its hash is stored
            "expires_at": validation.expires_at.isoformat(),
            "preview": preview,
            "request_id": rid,
        }

    # --- commit ---------------------------------------------------------------------------------

    async def commit(
        self, key_id: str, idem_key: str, request_body: bytes, token: str, rid: str
    ) -> tuple[int, dict[str, Any], bool]:
        """Returns (status, body, replayed)."""
        claim = await self.repo.claim_idempotency(
            key_id, idem_key, hashlib.sha256(request_body).hexdigest(), self.clock()
        )
        if claim.kind == "replay":
            await self._audit(AuditEntry(rid, key_id, "-", "replayed", claim.status_code or 0))
            return claim.status_code, claim.body, True
        if claim.kind == "in_progress":
            raise ServiceError(409, "idempotency_key_in_progress", store=False, headers={"Retry-After": "2"})
        if claim.kind == "mismatch":
            raise ServiceError(422, "idempotency_key_reused", store=False)

        try:
            body = await self._commit_new(key_id, token, rid)
        except ServiceError as err:
            if err.store:
                await self.repo.finish_idempotency(key_id, idem_key, err.status, err.body(rid))
            else:
                await self.repo.fail_idempotency(key_id, idem_key)
            raise
        except BaseException:
            await self.repo.fail_idempotency(key_id, idem_key)
            raise
        await self.repo.finish_idempotency(key_id, idem_key, 201, body)
        return 201, body, False

    async def _commit_new(self, key_id: str, token: str, rid: str) -> dict[str, Any]:
        s = self.settings
        if not await self.breaker.allow():
            await self._audit(AuditEntry(rid, key_id, "-", "breaker_open", 503))
            raise ServiceError(
                503, "circuit_open", store=False, headers={"Retry-After": str(s.breaker_cooldown_seconds)}
            )
        now = self.clock()
        if not s.within_hours(now):
            await self._audit(AuditEntry(rid, key_id, "-", "outside_hours", 403))
            raise ServiceError(403, "outside_allowed_hours", {"allowed_hours": s.allowed_hours}, store=False)

        today = now.astimezone(s.tz()).date()
        action_name, validation_id = "-", None
        try:
            async with self.repo.write(key_id) as uow:
                if await uow.commits_since(key_id, s.start_of_day(now)) >= s.daily_commit_cap:
                    raise ServiceError(429, "daily_cap_reached", {"cap": s.daily_commit_cap}, store=False)
                validation = await uow.take_validation(token_hash(token))
                if validation is None or validation.key_id != key_id:
                    raise ServiceError(404, "unknown_token")
                action_name, validation_id = validation.action, validation.id
                if validation.used_at is not None:
                    raise ServiceError(409, "token_already_used")
                if validation.expires_at <= now:
                    raise ServiceError(410, "token_expired")

                action = self._action(validation.action)
                params = action.parse(validation.params)
                try:
                    preview = await action.check(uow, params, s, today, lock=True)
                except RuleViolation as exc:
                    raise ServiceError(422, "rule_violation", exc.violations) from None
                if preview != validation.preview:
                    raise ServiceError(409, "state_changed", {"validated": validation.preview, "now": preview})

                result = await action.apply(uow, params, preview)
                await uow.mark_validation_used(validation.id, now)
                await uow.audit(AuditEntry(rid, key_id, action_name, "committed", 201, validation.id, result))
        except ServiceError as err:
            await self.breaker.record_success()  # a business answer means the system is healthy
            event = "commit_rejected" if err.store else "commit_deferred"
            await self._audit(
                AuditEntry(rid, key_id, action_name, event, err.status, validation_id, {"error": err.code})
            )
            raise
        except QueryTimeout:
            await self.breaker.record_failure()
            await self._audit(AuditEntry(rid, key_id, action_name, "timeout", 504, validation_id))
            raise ServiceError(504, "query_timeout", store=False) from None
        except Exception:
            log.exception("commit failed rid=%s", rid)
            await self.breaker.record_failure()
            await self._audit(AuditEntry(rid, key_id, action_name, "error", 500, validation_id))
            raise ServiceError(500, "internal_error", store=False) from None

        await self.breaker.record_success()
        return {"action": action_name, "result": result, "request_id": rid}


def _rules(exc: RuleViolation) -> list[str]:
    return [v["rule"] for v in exc.violations]
