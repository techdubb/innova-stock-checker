"""Environment-based configuration. No secrets live in code."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TARGET_URL = "https://proshop.innovadiscs.com/duo-destroyer-calvin-heimburg-commemorative/"

# Politeness floor from the PRD: never poll more often than every 2 minutes,
# even at the low end of the jitter range.
MIN_POLL_SECONDS = 120


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    target_url: str
    ntfy_topic: str
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: str | None = None
    poll_interval_seconds: int = 180
    jitter_percent: int = 20
    confirmations: int = 2
    heartbeat_time: time = time(9, 0)
    timezone: ZoneInfo = ZoneInfo("America/New_York")
    state_file: Path = Path("./state.json")
    log_level: str = "INFO"
    reminder_interval_seconds: int = 15 * 60
    max_reminders: int = 3
    failure_alert_after_seconds: int = 30 * 60
    max_backoff_seconds: int = 30 * 60
    request_timeout_seconds: int = 15

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def get(name: str, default: str | None = None) -> str | None:
            value = env.get(name, "").strip()
            return value or default

        def get_int(name: str, default: int, minimum: int = 0) -> int:
            raw = get(name)
            if raw is None:
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
            if value < minimum:
                raise ConfigError(f"{name} must be >= {minimum}, got {value}")
            return value

        topic = get("NTFY_TOPIC")
        if not topic:
            raise ConfigError("NTFY_TOPIC is required (pick a long, hard-to-guess name)")

        target_url = get("TARGET_URL", DEFAULT_TARGET_URL)
        if urlparse(target_url).scheme not in ("http", "https"):
            raise ConfigError(f"TARGET_URL must be an http(s) URL, got {target_url!r}")

        interval = get_int("POLL_INTERVAL_SECONDS", 180, minimum=1)
        jitter = get_int("JITTER_PERCENT", 20)
        if jitter > 50:
            raise ConfigError(f"JITTER_PERCENT must be between 0 and 50, got {jitter}")
        if interval * (100 - jitter) / 100 < MIN_POLL_SECONDS:
            raise ConfigError(
                f"POLL_INTERVAL_SECONDS={interval} with JITTER_PERCENT={jitter} could poll "
                f"faster than once per {MIN_POLL_SECONDS}s; raise the interval or lower jitter"
            )

        hb_raw = get("HEARTBEAT_TIME", "09:00")
        try:
            hh, mm = hb_raw.split(":")
            heartbeat_time = time(int(hh), int(mm))
        except ValueError:
            raise ConfigError(f"HEARTBEAT_TIME must be HH:MM, got {hb_raw!r}") from None

        tz_name = get("TIMEZONE", "America/New_York")
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"Unknown TIMEZONE {tz_name!r}") from None

        log_level = get("LOG_LEVEL", "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"Invalid LOG_LEVEL {log_level!r}")

        return cls(
            target_url=target_url,
            ntfy_topic=topic,
            ntfy_server=get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            ntfy_token=get("NTFY_TOKEN"),
            poll_interval_seconds=interval,
            jitter_percent=jitter,
            confirmations=get_int("CONFIRMATIONS", 2, minimum=1),
            heartbeat_time=heartbeat_time,
            timezone=tz,
            state_file=Path(get("STATE_FILE", "./state.json")),
            log_level=log_level,
            reminder_interval_seconds=get_int("REMINDER_INTERVAL_SECONDS", 15 * 60, minimum=60),
            max_reminders=get_int("MAX_REMINDERS", 3),
            failure_alert_after_seconds=get_int("FAILURE_ALERT_AFTER_SECONDS", 30 * 60, minimum=60),
        )
