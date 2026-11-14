"""Settings, read from environment variables (12-factor). See README, "Configuration"."""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32
_HOURS = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)-([01]\d|2[0-4]):([0-5]\d)$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Connections
    database_url: str = ""
    redis_url: str = Field(default="", description="Empty = in-memory stores (single instance only)")
    db_pool_min: int = Field(default=1, ge=1, le=50)
    db_pool_max: int = Field(default=10, ge=1, le=200)
    statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)

    # Who may call
    client_keys: dict[str, str] = Field(default_factory=dict)
    allowed_networks: list[str] = Field(default_factory=lambda: ["127.0.0.1/32"])
    trust_proxy_headers: bool = False
    max_clock_skew_seconds: int = Field(default=300, ge=5, le=900)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    max_body_bytes: int = Field(default=32_768, ge=256, le=1_048_576)

    # Write governance
    validation_ttl_seconds: int = Field(default=600, ge=10, le=3_600)
    daily_commit_cap: int = Field(default=200, ge=1, le=1_000_000)
    allowed_hours: str = Field(default="00:00-24:00", description="HH:MM-HH:MM in TIMEZONE")
    timezone: str = "UTC"
    max_invoice_total: Decimal = Field(default=Decimal("1000000"), gt=0)
    max_invoice_lines: int = Field(default=50, ge=1, le=500)
    breaker_failure_threshold: int = Field(default=5, ge=1, le=1_000)
    breaker_window_seconds: int = Field(default=60, ge=1, le=3_600)
    breaker_cooldown_seconds: int = Field(default=300, ge=1, le=86_400)

    enable_docs: bool = False
    log_level: str = "INFO"

    @field_validator("client_keys")
    @classmethod
    def _strong_secrets(cls, keys: dict[str, str]) -> dict[str, str]:
        for key_id, secret in keys.items():
            if not key_id or len(key_id) > 64:
                raise ValueError("key ids must be 1-64 characters")
            if len(secret) < MIN_SECRET_LENGTH:
                raise ValueError(f"secret for '{key_id}' is shorter than {MIN_SECRET_LENGTH} characters")
            if "replace-with" in secret:
                raise ValueError(f"secret for '{key_id}' is still the placeholder from .env.example")
        return keys

    @field_validator("allowed_networks")
    @classmethod
    def _valid_networks(cls, networks: list[str]) -> list[str]:
        for net in networks:
            ipaddress.ip_network(net, strict=False)
        return networks

    @field_validator("allowed_hours")
    @classmethod
    def _valid_hours(cls, value: str) -> str:
        m = _HOURS.match(value)
        if not m or int(m.group(1)) * 60 + int(m.group(2)) > 1440 or int(m.group(3)) * 60 + int(m.group(4)) > 1440:
            raise ValueError("allowed_hours must look like 08:00-20:00")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone: {value}") from None
        return value

    def networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(n, strict=False) for n in self.allowed_networks]

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def within_hours(self, moment: dt.datetime) -> bool:
        """True if `moment` falls in ALLOWED_HOURS (local to TIMEZONE). Windows may cross midnight."""
        m = _HOURS.match(self.allowed_hours)
        start = int(m.group(1)) * 60 + int(m.group(2))
        end = int(m.group(3)) * 60 + int(m.group(4))
        local = moment.astimezone(self.tz())
        minute = local.hour * 60 + local.minute
        if start == end:
            return False
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end  # e.g. 22:00-06:00

    def start_of_day(self, moment: dt.datetime) -> dt.datetime:
        local = moment.astimezone(self.tz())
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
