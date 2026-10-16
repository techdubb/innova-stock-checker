"""Persisted watcher state and the pure transition function `step()`.

`step()` takes the previous state, one Detection, and the current time, and
returns the new state plus any notifications to send. It does no I/O, which
keeps alert/reminder/heartbeat/failure logic unit-testable with a fake clock.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from pathlib import Path

from config import Config
from detect import Detection, StockState

log = logging.getLogger(__name__)

PRIORITY_URGENT = "urgent"
PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"


@dataclass(frozen=True)
class Notification:
    kind: str  # restock | reminder | heartbeat | failure | recovered
    title: str
    message: str
    priority: str
    click_url: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchState:
    current: str = StockState.UNKNOWN.value
    last_known: str | None = None  # last non-UNKNOWN state
    consecutive_in_stock: int = 0
    alerted: bool = False  # restock alert sent for the current in-stock episode
    reminders_sent: int = 0
    last_alert_at: str | None = None
    unhealthy_since: str | None = None
    failure_alert_sent: bool = False
    last_heartbeat_date: str | None = None


def load_state(path: Path) -> WatchState:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return WatchState()
    except (OSError, ValueError) as exc:
        log.error("state file unreadable, starting fresh", extra={"fields": {"error": str(exc)}})
        return WatchState()
    known = {f.name for f in fields(WatchState)}
    return WatchState(**{k: v for k, v in raw.items() if k in known})


def save_state(path: Path, state: WatchState) -> None:
    """Atomic write: a crash mid-write never leaves a corrupt state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _product_label(det: Detection) -> str:
    return det.name or "Watched product"


def step(
    state: WatchState, det: Detection, now: datetime, cfg: Config
) -> tuple[WatchState, list[Notification]]:
    """Advance the state machine by one observation. `now` must be timezone-aware."""
    notes: list[Notification] = []
    s = replace(state, current=det.state.value)

    # --- stock transitions -------------------------------------------------
    if det.state is StockState.IN_STOCK:
        s = replace(s, consecutive_in_stock=s.consecutive_in_stock + 1)
        if not s.alerted:
            if s.consecutive_in_stock >= cfg.confirmations:
                notes.append(_restock_note(det, cfg))
                s = replace(
                    s, alerted=True, reminders_sent=0, last_alert_at=now.isoformat(),
                    last_known=StockState.IN_STOCK.value,
                )
        else:
            s = replace(s, last_known=StockState.IN_STOCK.value)
            last = _parse(s.last_alert_at)
            due = last is None or (now - last).total_seconds() >= cfg.reminder_interval_seconds
            if s.reminders_sent < cfg.max_reminders and due:
                n = s.reminders_sent + 1
                notes.append(_restock_note(det, cfg, reminder=n))
                s = replace(s, reminders_sent=n, last_alert_at=now.isoformat())
    elif det.state is StockState.OUT_OF_STOCK:
        # Re-arm: a later restock will alert again.
        s = replace(
            s, consecutive_in_stock=0, alerted=False, reminders_sent=0,
            last_known=StockState.OUT_OF_STOCK.value,
        )
    else:
        # UNKNOWN breaks the "consecutive" run but doesn't re-arm an alert,
        # so IN -> UNKNOWN -> IN does not double-alert.
        s = replace(s, consecutive_in_stock=0)

    # --- health / failure alert ------------------------------------------
    if det.state is StockState.UNKNOWN:
        since = _parse(s.unhealthy_since)
        if since is None:
            s = replace(s, unhealthy_since=now.isoformat())
        elif (
            not s.failure_alert_sent
            and (now - since).total_seconds() >= cfg.failure_alert_after_seconds
        ):
            minutes = int((now - since).total_seconds() // 60)
            notes.append(Notification(
                kind="failure",
                title="Restock watcher may be blocked or broken",
                message=f"No reliable stock reading for {minutes} min. Last reason: {det.reason}",
                priority=PRIORITY_HIGH,
                click_url=cfg.target_url,
                tags=("warning",),
            ))
            s = replace(s, failure_alert_sent=True)
    else:
        if s.failure_alert_sent:
            notes.append(Notification(
                kind="recovered",
                title="Restock watcher recovered",
                message=f"Reading the page again. State: {det.state.value}",
                priority=PRIORITY_LOW,
                click_url=cfg.target_url,
                tags=("white_check_mark",),
            ))
        s = replace(s, unhealthy_since=None, failure_alert_sent=False)

    # --- daily heartbeat -------------------------------------------------
    local = now.astimezone(cfg.timezone)
    today = local.date().isoformat()
    if local.time() >= cfg.heartbeat_time and s.last_heartbeat_date != today:
        notes.append(Notification(
            kind="heartbeat",
            title="Restock watcher is alive",
            message=(
                f"{_product_label(det)}: {s.current} "
                f"(last known: {s.last_known or 'none yet'}). Last reason: {det.reason}"
            ),
            priority=PRIORITY_LOW,
            click_url=cfg.target_url,
            tags=("heartbeat",),
        ))
        s = replace(s, last_heartbeat_date=today)

    return s, notes


def _restock_note(det: Detection, cfg: Config, reminder: int = 0) -> Notification:
    name = _product_label(det)
    price = f" - {det.price}" if det.price else ""
    title = f"BACK IN STOCK: {name}"
    if reminder:
        title = f"Still in stock ({reminder}/{cfg.max_reminders}): {name}"
    return Notification(
        kind="reminder" if reminder else "restock",
        title=title,
        message=f"{name}{price}\nTap to open: {cfg.target_url}",
        priority=PRIORITY_HIGH if reminder else PRIORITY_URGENT,
        click_url=cfg.target_url,
        tags=("rotating_light",) if not reminder else ("bell",),
    )
