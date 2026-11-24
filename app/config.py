"""Application settings read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Stores that were checked to return prices in plain HTML (see README).
DEFAULT_STORES = ("comfy", "moyo", "foxtrot", "epicentr")


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} має бути цілим числом, отримано {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    database_path: str = "price_monitor.db"
    secret_key: str = "change-me"
    enabled_stores: tuple[str, ...] = DEFAULT_STORES
    demo_mode: bool = False
    request_timeout: int = 10
    monitor_interval_minutes: int = 360
    min_drop_percent: int = 1
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        """Build the configuration from environment variables."""
        raw_stores = os.environ.get("ENABLED_STORES", "").strip()
        stores = (
            tuple(s.strip().lower() for s in raw_stores.split(",") if s.strip())
            if raw_stores
            else DEFAULT_STORES
        )
        config = cls(
            database_path=os.environ.get("DATABASE_PATH", cls.database_path),
            secret_key=os.environ.get("SECRET_KEY", cls.secret_key),
            enabled_stores=stores,
            demo_mode=os.environ.get("DEMO_MODE", "") == "1",
            request_timeout=_env_int("REQUEST_TIMEOUT", cls.request_timeout),
            monitor_interval_minutes=_env_int(
                "MONITOR_INTERVAL_MINUTES", cls.monitor_interval_minutes
            ),
            min_drop_percent=_env_int("MIN_DROP_PERCENT", cls.min_drop_percent),
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=_env_int("SMTP_PORT", cls.smtp_port),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("SMTP_FROM", ""),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Raise ConfigError if some values are out of range."""
        if self.monitor_interval_minutes < 1:
            raise ConfigError("MONITOR_INTERVAL_MINUTES має бути не меншим за 1")
        if self.request_timeout < 1:
            raise ConfigError("REQUEST_TIMEOUT має бути не меншим за 1")
        if not 0 <= self.min_drop_percent <= 100:
            raise ConfigError("MIN_DROP_PERCENT має бути від 0 до 100")
